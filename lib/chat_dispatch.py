"""lib/chat_dispatch.py — chat_send + chat_stream business-logic sinks (pt_04686ac6 slices 5-7).

**Extraction context** (board epic ``pt_04686ac6054a451e``):

  * Slice 5 — ``classify_send_intent``: chat_send's ~175-line queue-
    classification + steer + abort-on-send pipeline.
  * Slice 6 — ``build_cold_replay_response``: chat_stream's ~170-line
    cold-path (task not in memory) resume-serviceability + snapshot
    generation for both the persisted-events replay and the DB-only
    synthetic state+done paths.
  * Slice 7 — ``plan_warm_resume`` + ``build_fresh_state_snapshot``:
    chat_stream's warm-path Last-Event-ID parse, ``_warm_resume_serviceable``
    verdict + resume-snapshot builder AND the fresh-connection full-state
    snapshot builder. Two pure(-ish) helpers so the chat_stream generate()
    closure shrinks to a thin cursor+yield driver.

``routes/chat.py::chat_send`` used to inline ~175 lines of business
logic between "user_msg was built with auto-translate" and "no active
task, dispatch immediately": abort-during-translate check, abort-on-send
race (frontend-reported recently-stopped task), running-task
classification, autopilot-followup supersede, inject-mode steer path,
and the queue path with cross-device pending-row mirror.

That block is EXACTLY the "queue classification + autopilot-followup
detection + abort-on-send race" bundle the epic description called out
as the fat handler's business logic. Extracted here so:

  * ``chat_send`` becomes a ~30-line facade around the parse → classify
    → dispatch pipeline
  * The classification lives in an importable, unit-testable pure(-ish)
    function that takes explicit arguments and returns a small
    ``SendIntent`` value object
  * Future slices can add ``dispatch_regen`` / ``dispatch_continue`` /
    ``dispatch_branch`` next to it under the same module

**Contract**:

  ``classify_send_intent(...) -> SendIntent | None``

    Returns ``None`` when the caller MUST fall through to the
    "immediate start" path (append user_msg, persist, start task).

    Returns a ``SendIntent`` when the caller must SHORT-CIRCUIT and
    return the intent's ``response`` dict as the API response. The
    intent's ``kind`` is one of:
      * ``'aborted'`` — abort landed during auto-translate; drop the
        message entirely (do NOT persist, enqueue, or dispatch).
      * ``'steered'`` — steer-injected into the currently-running turn.
      * ``'queued'`` — enqueued for dispatch after the running turn ends.

Side effects (preserved byte-for-byte from the pre-slice inline code):

  * Marks any ``abortTaskId``-referenced task as aborted
    (`_abort_reason='superseded_by_send'`) BEFORE the running-task scan.
  * Supersedes invisible autopilot follow-up runs when the ONLY running
    tasks are followups AND autopilot is armed (aborts them, disarms
    autopilot, falls through so has_running_task=False).
  * On 'steer' with a drainable inbox: enqueues into agent_inbox at
    priority=next/mode=user-steer, persists title-only for new convs,
    emits notify_conv_changed(rev=None).
  * On 'queue' path: enqueues into message_queue with pre-built
    user_msg (so dispatch_next_queued can append without re-translate),
    persists title-only for new convs, attempts the cross-device
    pending-row mirror (best-effort), emits notify_conv_changed with
    the real rev.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass
class SendIntent:
    """Result of classify_send_intent: what chat_send should return NOW.

    Attributes:
        kind: One of 'aborted', 'steered', 'queued' — the branch the
            classifier picked. Callers use this only for logging /
            metrics; the response body is authoritative.
        response: The JSON body the caller should hand back verbatim.
    """
    kind: str
    response: dict[str, Any]


def classify_send_intent(
    db: Any,
    conv_id: str,
    config: dict[str, Any],
    payload: dict[str, Any],
    data: dict[str, Any],
    messages: list,
    is_new: bool,
    title: str,
    user_msg: dict[str, Any],
    settings_patch: Any,
    text: str,
    send_started_at: float,
) -> SendIntent | None:
    """Business-logic pipeline of chat_send. See module docstring.

    Returns None to signal "proceed with immediate start"; returns a
    SendIntent to short-circuit chat_send with its response body.
    """
    # Late imports (routes/chat.py used lazy imports throughout — preserve
    # that style so the extraction cannot introduce a new import-time
    # circular-dependency edge with lib.tasks_pkg / lib.message_queue).
    from lib.tasks_pkg import tasks, tasks_lock
    from lib.chat import (
        append_pending_user_msg as _append_pending_user_msg,
        persist_conv_messages as _persist_conv_messages,
    )
    from routes.chat_state import _was_aborted_after
    # notify_conv_changed lives in routes.common; wrap in a try-broken
    # local so the extraction remains testable without a Flask app ctx.
    try:
        from routes.common import _notify_conv_changed
    except Exception as _e:
        logger.debug('[chat_dispatch] routes.common._notify_conv_changed '
                     'unavailable: %s (test path)', _e)
        _notify_conv_changed = lambda *a, **kw: None  # noqa: E731

    # 2a. If the user clicked Stop while we were inside the auto-
    #     translate call, drop this message entirely — do NOT persist,
    #     enqueue, or dispatch. This prevents the 'translation finishes
    #     after abort → enqueue → fires after regen completes' double-
    #     send bug.
    if _was_aborted_after(conv_id, send_started_at):
        logger.info('[Send] conv=%s ⚠️ Aborted during translate — dropping message '
                    '(translated=%s)',
                    conv_id[:8], bool(user_msg.get('originalContent')))
        return SendIntent(kind='aborted', response={
            'aborted': True,
            'convId': conv_id,
        })

    # ★ 3a. If the frontend reports a recently-aborted task, mark it
    #   as aborted NOW — this handles the race where the user clicks
    #   Stop and immediately sends a new message, and the fire-and-
    #   forget abort fetch hasn't arrived yet.
    abort_task_id = data.get('abortTaskId')
    if abort_task_id:
        with tasks_lock:
            abort_target = tasks.get(abort_task_id)
            if (abort_target
                    and not abort_target.get('aborted')
                    and abort_target.get('convId') == conv_id):
                abort_target['aborted'] = True
                abort_target['_abort_timestamp'] = time.time()
                abort_target['_abort_reason'] = 'superseded_by_send'
                logger.info('[Send] conv=%s ⚠️ Abort-on-send: task %s marked aborted '
                            '(frontend reported recently stopped task)',
                            conv_id[:8], abort_task_id[:8])

    # ★ 3b. Check if a task is already running for this conversation.
    #   If so, enqueue instead of starting — the backend dispatches
    #   automatically when the current task finishes.
    running_tasks = []
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('aborted')):
                running_tasks.append(t)

    from lib.message_queue import has_autopilot_marker

    def _is_autopilot_followup(t):
        return bool(t.get('_autopilotParent') or t.get('_vu_subtask')
                    or t.get('_autopilot_kick'))

    has_running_task = bool(running_tasks)
    only_autopilot_followups = (
        has_running_task
        and all(_is_autopilot_followup(t) for t in running_tasks))

    if (has_running_task and only_autopilot_followups
            and has_autopilot_marker(conv_id)):
        # Supersede: abort the invisible autopilot follow-up(s) for real
        # (backend stop, so the zombie is reclaimed), disarm autopilot, and
        # fall through to start the human message immediately.
        for t in running_tasks:
            t['aborted'] = True
            t['_abort_timestamp'] = time.time()
            t['_abort_reason'] = 'superseded_by_user_send'
        logger.info('[Send] conv=%s ⚡ superseding %d in-flight autopilot '
                    'follow-up turn(s) for a real user send',
                    conv_id[:8], len(running_tasks))
        try:
            from lib.tasks_pkg.autopilot import disarm_autopilot
            disarm_autopilot(conv_id)
        except Exception as e:
            logger.warning('[Send] conv=%s disarm_autopilot on supersede '
                           'failed (non-fatal): %s', conv_id[:8], e)
        has_running_task = False

    # ★ Inject-mode: 'queue' (default) or 'steer'.
    # injectMode is a PER-SEND decision from the post-send dialog
    # (_promptInjectMode), carried at the top level of the request body — it
    # is NOT a persisted conversation setting. Read `data` FIRST: reading
    # `config` first would be shadowed by resolve_conv_config's 'queue'
    # default (truthy), so a 'steer' choice could never win.
    _inject_mode = (data.get('injectMode') or '').strip().lower()
    if has_running_task and _inject_mode == 'steer':
        from lib.agent_inbox import _tombstones as _inbox_tombstones
        from lib.agent_inbox import _lock as _inbox_lock
        from lib.agent_inbox import enqueue as _inbox_enqueue
        # The inbox key is conversation-scoped (swarm_key_for → convId).
        _steer_key = conv_id
        with _inbox_lock:
            _drainable = _steer_key not in _inbox_tombstones
        if _drainable:
            # value = the wire text the model sees; _user_msg carries the
            # pre-built/translated dict so the finalize salvage can re-queue
            # it verbatim on an abort (exactly-once, never re-translated).
            _steer_text = user_msg.get('content', '') or text
            _inbox_enqueue(
                _steer_key, _steer_text,
                priority='next', mode='user-steer',
                extra={'_user_msg': user_msg, 'config': config})
            logger.info('[Send] conv=%s ➡ STEER (injected into running turn) '
                        'text=%d chars', conv_id[:8], len(_steer_text))
            if is_new:
                _persist_conv_messages(db, conv_id, messages, title, settings_patch)
            _notify_conv_changed(conv_id, rev=None)
            return SendIntent(kind='steered', response={
                'steered': True,
                'convId': conv_id,
                'title': title,
                'userMessage': user_msg,
                'isNew': is_new,
                'msgCount': len(messages),  # excludes the steer msg
            })
        # Not drainable → fall through to the durable-queue path below so
        # the steer is delivered as a fresh turn instead of being dropped.
        logger.info('[Send] conv=%s steer requested but inbox slot not '
                    'drainable (task finalizing) — falling back to queue',
                    conv_id[:8])

    if has_running_task:
        from lib.message_queue import enqueue_message, get_queue_depth
        # ★ Enqueue for later dispatch. The durable queue is the source of
        #   truth for WHEN this turn runs. Store the pre-built user_msg so
        #   dispatch_next_queued can append it without re-translating.
        queue_payload = dict(payload)
        queue_payload['_user_msg'] = user_msg
        queue_result = enqueue_message(conv_id, queue_payload, config)
        logger.info('[Send] conv=%s ➡ QUEUED (active task running) queueId=%s position=%d',
                    conv_id[:8], queue_result['queueId'][:8], queue_result['position'])

        # Persist title update for new conversations (but NOT the user message)
        if is_new:
            _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # ★ Cross-device visibility (Fix 2a): land the queued user message
        #   in the conversation body NOW as a display-only _pendingQueued
        #   row + push the REAL rev, so another device sees it immediately
        #   instead of only after the current turn replies. Two guards keep
        #   this safe: (1) ONLY the FIRST queued turn (depth==1 after this
        #   enqueue) may pre-persist — a 2nd pending row would misorder
        #   against the eventual replies; (2) the helper itself declines
        #   unless the DB tail is the running turn's assistant slot (so the
        #   row lands correctly ordered). On decline we fall back to the
        #   original queue-only behaviour (rev=None sidebar nudge). The
        #   later dispatch_next_queued reconciles this row in place by
        #   timestamp (never a duplicate).
        _pending_rev = None
        try:
            _running_amids = {t.get('_assistantMsgId') for t in running_tasks
                              if t.get('_assistantMsgId')}
            if get_queue_depth(conv_id) == 1:
                _appended, _pending_rev = _append_pending_user_msg(
                    db, conv_id, user_msg, valid_assistant_ids=_running_amids)
                if _appended:
                    logger.info('[Send] conv=%s queued user msg mirrored as '
                                'pending row (rev=%s) — cross-device visible',
                                conv_id[:8], _pending_rev)
        except Exception as e:
            logger.warning('[Send] conv=%s pending-row mirror failed (non-fatal, '
                           'queue-only fallback): %s', conv_id[:8], e)
            _pending_rev = None

        _notify_conv_changed(conv_id, rev=_pending_rev)

        return SendIntent(kind='queued', response={
            'queued': True,
            'queueId': queue_result['queueId'],
            'position': queue_result['position'],
            'convId': conv_id,
            'title': title,
            'userMessage': user_msg,
            'isNew': is_new,
            'msgCount': len(messages),  # excludes the queued user msg
        })

    # No active task, no steer, no abort → caller falls through to
    # immediate-start (append user_msg + persist + start task).
    return None


async def build_cold_replay_response(task_id: str, last_event_id_header: str):
    """chat_stream cold-path handler (pt_04686ac6 slice 6).

    Called by ``routes/chat.py::chat_stream`` when the task is not in
    the in-memory ``tasks`` dict (crashed / cleaned up / restarted /
    reconnecting past cleanup_old_tasks). Two sub-paths:

    1. **Persisted event replay** — if the client sends a valid
       ``Last-Event-ID`` header AND ``lib.tasks_pkg.event_log`` has
       persisted events since that cursor, replay every event from the
       log. On no persisted 'done' event, synthesize a state+done pair
       from the ``task_results`` row folded with ``event_fold``.

    2. **DB snapshot** — if no valid Last-Event-ID (or the log had
       nothing since it) but ``task_results`` still has the row,
       emit a single ``state`` event (content + thinking, folded via
       event_fold; toolRounds either from the DB column or rebuilt from
       the conversation) followed by a ``done`` event with all the
       expected metadata keys (finishReason / usage / preset / model /
       provider_id / thinkingDepth / apiRounds / modifiedFiles /
       modifiedFileList / fallbackModel/from/reason/kind).

    3. **Not found** — no persisted events AND no ``task_results`` row
       → return ``api_not_found('Task not found')``.

    Args:
        task_id: The task id from the URL path.
        last_event_id_header: The raw ``Last-Event-ID`` HTTP header
            value (empty string when the client didn't send one).

    Returns:
        A Flask/Quart response ready to hand back from chat_stream, OR
        ``None`` in the degenerate case that a caller should fall through
        (kept as a sentinel; not currently used — the three sub-paths
        above always produce a real response).
    """
    import asyncio
    import json

    from lib.agent_core.events import EventType, build_event
    from lib.api_response import api_not_found, sse_response
    from lib.database import DOMAIN_CHAT, get_db

    # Late imports (matches routes/chat.py's late-import style; keeps
    # this module import-lightweight for tests).
    from routes.chat import _dumps_yielding, _loads_yielding
    from lib.chat.persistence import extract_db_meta as _extract_db_meta

    # ── Persisted event replay path ──
    _replay_cursor_hdr = (last_event_id_header or '').strip()
    if _replay_cursor_hdr:
        try:
            _replay_cursor = int(_replay_cursor_hdr)
        except (ValueError, TypeError) as _e_audit:
            logger.debug('[chat_dispatch] cold-replay caught %s: %s',
                         type(_e_audit).__name__, _e_audit)
            _replay_cursor = None
        if _replay_cursor is not None and _replay_cursor >= 0:
            from lib.tasks_pkg.event_log import read_events as _read_events
            _persisted = await asyncio.to_thread(
                _read_events, task_id, since_event_id=_replay_cursor)
            if _persisted:
                logger.info('[Chat] Stream %s cold replay from event_log: '
                            '%d event(s) since id=%d',
                            task_id[:8], len(_persisted), _replay_cursor)

                def gen_persisted():
                    # SSE preamble: 4 large-comment lines force any
                    # buffering proxy to flush headers immediately.
                    for _ in range(4):
                        yield ':' + ' ' * 2048 + '\n\n'
                    for ev in _persisted:
                        eid = ev['event_id']
                        payload = ev['payload']
                        yield f'id: {eid}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                        if isinstance(payload, dict) and payload.get('type') == 'done':
                            return
                    # No persisted 'done' — synthesize state+done from
                    # task_results.  We MUST emit a 'state' event before
                    # 'done' here: a client whose Last-Event-ID points
                    # past the end of the persisted log (e.g. TTL prune
                    # ran, or the client's last cursor was very recent)
                    # would otherwise see only metadata and lose all
                    # text. Mirrors the warm-fallback shape further down.
                    try:
                        db_local = get_db(DOMAIN_CHAT)
                        row_local = db_local.execute(
                            'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata '
                            'FROM task_results WHERE task_id=?',
                            (task_id,)
                        ).fetchone()
                        if row_local:
                            # ★ Close the 5s cold-replay window: fold
                            #   the lossless per-delta task_events log
                            #   instead of trusting the (up to 5s stale)
                            #   task_results checkpoint. The fold
                            #   reconstructs the EXACT text the client
                            #   saw; on an empty/failed log it returns
                            #   the checkpoint pair unchanged.
                            from lib.tasks_pkg.event_fold import fold_cold_state_text
                            _fold_c, _fold_t = fold_cold_state_text(
                                task_id, row_local['content'] or '',
                                row_local['thinking'] or '')
                            state_local = build_event(
                                EventType.STATE,
                                content=_fold_c,
                                thinking=_fold_t,
                                status=row_local['status'],
                            )
                            if row_local['tool_rounds']:
                                try:
                                    state_local['toolRounds'] = _loads_yielding(row_local['tool_rounds'])
                                except (json.JSONDecodeError, ValueError, TypeError) as _e:
                                    logger.debug('[Chat] cold-replay tool_rounds parse failed: %s', _e)
                            else:
                                from lib.tasks_pkg import load_tool_rounds_from_conversation
                                _tr = load_tool_rounds_from_conversation(row_local['conv_id'])
                                if _tr:
                                    state_local['toolRounds'] = _tr
                            if row_local['error']:
                                from lib.error_envelope import from_json as _err_from_json
                                state_local['error'] = _err_from_json(row_local['error'])
                            yield f'data: {_dumps_yielding(state_local)}\n\n'
                        done_evt_local = build_event(EventType.DONE)
                        if row_local:
                            if row_local['metadata']:
                                try:
                                    m = json.loads(row_local['metadata'])
                                    # Field list MUST mirror
                                    # _extract_task_meta / _extract_db_meta
                                    # / chat_poll's DB-path loop.  See
                                    # _extract_task_meta docstring for why.
                                    for k in ('finishReason', 'usage', 'preset', 'toolSummary',
                                              'model', 'provider_id', 'thinkingDepth',
                                              'apiRounds', 'modifiedFiles', 'modifiedFileList',
                                              'fallbackModel', 'fallbackFrom',
                                              'fallbackReason', 'fallbackKind'):
                                        if m.get(k):
                                            done_evt_local[k] = m[k]
                                except (json.JSONDecodeError, TypeError) as _e_audit:
                                    logger.debug('[chat_dispatch] gen_persisted caught %s: %s',
                                                 type(_e_audit).__name__, _e_audit)
                            if row_local['error']:
                                from lib.error_envelope import from_json as _err_from_json
                                done_evt_local['error'] = _err_from_json(row_local['error'])
                        yield f'data: {_dumps_yielding(done_evt_local)}\n\n'
                    except Exception as _e:
                        logger.debug('[Chat] cold-replay synthetic done failed: %s', _e)

                return sse_response(gen_persisted())

    # ── DB snapshot path (no persisted events / no valid cursor) ──
    db = get_db(DOMAIN_CHAT)
    row = await asyncio.to_thread(
        lambda: db.execute(
            'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata '
            'FROM task_results WHERE task_id=?',
            (task_id,)
        ).fetchone())
    if row:
        # ★ Close the 5s cold-replay window (see gen_persisted above):
        #   fold the lossless per-delta task_events log; falls back to
        #   the checkpoint pair on an empty/failed log.
        from lib.tasks_pkg.event_fold import fold_cold_state_text
        _fold_c, _fold_t = fold_cold_state_text(
            task_id, row['content'] or '', row['thinking'] or '')
        state = build_event(
            EventType.STATE, content=_fold_c,
            thinking=_fold_t, status=row['status'],
        )
        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            state['error'] = _err_from_json(row['error'])
        if row['tool_rounds']:
            try:
                state['toolRounds'] = await asyncio.to_thread(_loads_yielding, row['tool_rounds'])
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds for task %s: %s',
                               task_id, e, exc_info=True)
        else:
            from lib.tasks_pkg import load_tool_rounds_from_conversation
            _tr = await asyncio.to_thread(load_tool_rounds_from_conversation, row['conv_id'])
            if _tr:
                state['toolRounds'] = _tr
        meta = _extract_db_meta(row)
        # Field lists MUST stay aligned with _extract_task_meta and the
        # chat_poll DB-path loop. See _extract_task_meta docstring.
        for key in ('finishReason', 'usage', 'preset', 'model',
                    'provider_id', 'thinkingDepth',
                    'apiRounds', 'modifiedFiles', 'modifiedFileList'):
            if meta.get(key):
                state[key] = meta[key]
        done_evt = build_event(EventType.DONE)
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                    'model', 'provider_id', 'thinkingDepth',
                    'apiRounds', 'modifiedFiles', 'modifiedFileList'):
            if meta.get(key):
                done_evt[key] = meta[key]
        if meta.get('fallbackModel'):
            done_evt['fallbackModel'] = meta['fallbackModel']
            done_evt['fallbackFrom'] = meta.get('fallbackFrom', '')
            if meta.get('fallbackReason'):
                done_evt['fallbackReason'] = meta['fallbackReason']
            if meta.get('fallbackKind'):
                done_evt['fallbackKind'] = meta['fallbackKind']
        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            done_evt['error'] = _err_from_json(row['error'])

        logger.info('[Chat] Stream %s served from DB — status=%s content=%dchars '
                    'finishReason=%s model=%s error=%s',
                    task_id[:8], row['status'], len(row['content'] or ''),
                    meta.get('finishReason', '?'), meta.get('model', '?'),
                    row['error'] or 'none')

        def gen_done():
            for _ in range(4):
                yield ':' + ' ' * 2048 + '\n\n'
            yield f'data: {_dumps_yielding(state)}\n\n'
            yield f'data: {_dumps_yielding(done_evt)}\n\n'

        return sse_response(gen_done())

    # ── Not found ──
    logger.warning('[Chat] Task %s not found (stream)', task_id)
    return api_not_found('Task not found')


@dataclass
class WarmResumePlan:
    """Result of ``plan_warm_resume``: enough state to drive the warm
    resume yields (pt_04686ac6 slice 7).

    Attributes:
        resume_from: The event-log index the caller resumes streaming
            from (per the SSE spec, ``cursor + 1`` — Last-Event-ID is the
            id of the last RECEIVED event).
        replay_events: Copy of ``task['events'][resume_from:]`` taken
            under ``events_lock``; the caller yields these back to the
            client as ``id:``-tagged replay frames.
        resume_state: The leading ``state`` event the caller emits BEFORE
            the delta replay (see the ★ comment about the frontend
            keep-longer guard adopting a full snapshot on a fresh
            placeholder). Carries ``content`` / ``thinking`` /
            ``status`` / ``toolRounds`` + optional ``createdAt``,
            ``error`` copied from the task under the lock.
        serviceable: Always True on a returned plan — kept as an explicit
            attribute so a False-y check on the plan itself never fires
            by mistake (a plan with 0 replay events is still valid — it
            triggers the terminal ``late_done`` synth in the caller).
    """
    resume_from: int
    replay_events: list
    resume_state: dict[str, Any]
    serviceable: bool = True


def plan_warm_resume(
    task: dict[str, Any],
    last_event_id_hdr: str,
    task_id_short: str,
) -> WarmResumePlan | None:
    """chat_stream warm-path resume planner (pt_04686ac6 slice 7).

    Called by ``routes/chat.py::chat_stream`` inside its generate()
    closure. Handles the two-part decision the pre-slice inline code
    made:

      1. Parse ``Last-Event-ID`` to an int cursor. Empty / non-numeric
         → cursor=None → no valid resume → return None (caller falls
         through to ``build_fresh_state_snapshot``).
      2. Check ``_warm_resume_serviceable(cursor, len(task['events']))``
         under ``events_lock``. When True: slice ``task['events']`` from
         cursor+1, capture the resume state, return a ``WarmResumePlan``.
         When False (cursor ahead of buffer / trimmed): return None so
         the caller emits a full-state resync snapshot.

    Args:
        task: The in-memory task dict (must have ``events``,
            ``events_lock``, ``content``, ``thinking``, ``status``,
            ``error``, ``toolRounds``).
        last_event_id_hdr: Raw ``Last-Event-ID`` HTTP header value
            (empty string when absent).
        task_id_short: An 8-char task id for log lines (matches the
            pre-slice inline ``task_id[:8]`` style).

    Returns:
        A ``WarmResumePlan`` when the cursor is in-buffer; ``None``
        when the caller should build a fresh snapshot instead.
    """
    _cursor_hdr = (last_event_id_hdr or '').strip()
    if not _cursor_hdr:
        return None
    try:
        _cursor = int(_cursor_hdr)
    except (ValueError, TypeError):
        logger.debug('[Chat] SSE stream %s ignoring invalid Last-Event-ID: %s',
                     task_id_short, _cursor_hdr)
        return None
    logger.info('[Chat] SSE stream %s reconnecting with Last-Event-ID=%d',
                task_id_short, _cursor)

    # Late import to keep chat_dispatch import-lightweight for tests
    # (routes.chat_helpers re-exports _warm_resume_serviceable, which
    # is a 4-line pure function so the extra module load is cheap).
    from routes.chat_helpers import _warm_resume_serviceable
    from lib.agent_core.events import EventType, build_event

    with task['events_lock']:
        if not _warm_resume_serviceable(_cursor, len(task['events'])):
            if _cursor >= 0:
                logger.info('[Chat] SSE stream %s Last-Event-ID=%d is ahead of '
                            'buffer (len=%d) — full-snapshot resync',
                            task_id_short, _cursor, len(task['events']))
            return None
        _resume_from = _cursor + 1
        _replay = task['events'][_resume_from:]
        # ★ Build the resume state under the SAME lock that sliced the
        #   replay list, so the state and the deltas are internally
        #   consistent (a mid-lock append to task['content'] on the
        #   producer side otherwise races).  Mirrors the pre-slice
        #   inline block's second ``with task['events_lock']:`` — kept
        #   as ONE lock hold for slice 7 since the two blocks were
        #   trivially adjacent and the extra release+reacquire was
        #   pure overhead (no other awaits between them).
        _state = build_event(
            EventType.STATE, content=task['content'],
            thinking=task['thinking'], status=task['status'],
        )
        _created = task.get('created_at')
        if _created:
            _state['createdAt'] = int(_created * 1000)
        if task['error']:
            _state['error'] = task['error']
        _state['toolRounds'] = task['toolRounds']

    return WarmResumePlan(
        resume_from=_resume_from,
        replay_events=_replay,
        resume_state=_state,
        serviceable=True,
    )


def build_fresh_state_snapshot(task: dict[str, Any]):
    """chat_stream fresh-connection snapshot builder (pt_04686ac6 slice 7).

    Called by ``routes/chat.py::chat_stream`` when ``plan_warm_resume``
    returned None (no cursor / cursor ahead of buffer). Builds the full
    state event + meta dict + cursor exactly as the pre-slice inline
    block did.

    Runs the entire snapshot compose under ``events_lock`` so a
    concurrent producer append cannot split content across the state
    read and the cursor snapshot.

    Args:
        task: The in-memory task dict.

    Returns:
        (state, meta, cursor):
          * state: the ``state`` event dict to encode + yield
            (via ``asyncio.to_thread(_dumps_yielding, state)`` — the
            caller does the offload since this fn stays synchronous).
          * meta: the raw ``_extract_task_meta(task)`` dict; caller
            reuses it if the task is already terminal to synthesize
            the trailing ``done`` event.
          * cursor: ``len(task['events'])`` — where the live-stream
            loop should start reading from.
    """
    from lib.agent_core.events import EventType, build_event
    from lib.chat.persistence import extract_task_meta as _extract_task_meta

    with task['events_lock']:
        state = build_event(
            EventType.STATE, content=task['content'],
            thinking=task['thinking'], status=task['status'],
        )
        _created = task.get('created_at')
        if _created:
            state['createdAt'] = int(_created * 1000)
        if task['error']:
            state['error'] = task['error']
        if task['toolRounds']:
            state['toolRounds'] = task['toolRounds']
        meta = _extract_task_meta(task)
        for key in ('finishReason', 'usage', 'model', 'thinkingDepth'):
            if meta.get(key):
                state[key] = meta[key]
        if task.get('preset'):
            state['preset'] = task['preset']
        if task.get('_memoryPrefetch'):
            state['memoryPrefetch'] = task['_memoryPrefetch']
        if task.get('_preferencesApplied'):
            state['preferencesApplied'] = task['_preferencesApplied']
        if task.get('_relatedConversations'):
            state['relatedConversations'] = task['_relatedConversations']
        if task.get('_preferencesLearned'):
            state['preferencesLearned'] = task['_preferencesLearned']
        # inbox-inject sidecars (swarm/peer/user-steer) — survive an
        # SSE-broken resume so the in-timeline inject chips repaint.
        if task.get('_inboxInjects'):
            state['inboxInjects'] = task['_inboxInjects']
        if task.get('_peerInjects'):
            state['peerInjects'] = task['_peerInjects']
        if task.get('_userSteerInjects'):
            state['userSteerInjects'] = task['_userSteerInjects']
        if task.get('endpoint_mode'):
            state['endpointMode'] = True
            state['endpointPhase'] = task.get('_endpoint_phase', 'planning')
            state['endpointIteration'] = task.get('_endpoint_iteration', 0)
            ep_turns = task.get('_endpoint_turns')
            if ep_turns:
                state['endpointTurns'] = ep_turns
            if task.get('_endpoint_stop_reason'):
                state['endpointStopReason'] = task['_endpoint_stop_reason']
        cursor = len(task['events'])

    return state, meta, cursor


__all__ = [
    'SendIntent', 'classify_send_intent', 'build_cold_replay_response',
    'WarmResumePlan', 'plan_warm_resume', 'build_fresh_state_snapshot',
]
