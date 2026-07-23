"""lib/chat_dispatch.py — chat_send business-logic pipeline (pt_04686ac6 slice 5).

**Extraction context** (board epic ``pt_04686ac6054a451e``, slice 5):

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


__all__ = ['SendIntent', 'classify_send_intent']
