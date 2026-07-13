"""Layer 2 (manual) — user-triggered active context compaction (`/compact`).

Unlike the automatic L2 path (``_layer2.execute_compact_tool``), which is
*ephemeral* — it rewrites the in-flight request ``messages`` list once per
round and the raw ``conversations.messages`` in the DB is never shrunk — this
module implements the **persistent** manual compaction the user asks for with
a button: it replaces the old history in ``conversations.messages`` itself with
a single summary message, so every subsequent turn loads a small context.

It REUSES the L2 summary engine verbatim (``_generate_query_aware_summary`` +
the 9-section system prompt + objective-anchor protection + recently-accessed
files). It does NOT rewrite any summary logic.

────────────────────────────────────────────────────────────────────────────
⚠️  HARD CONSTRAINT (see docs/MANUAL_COMPACTION_DESIGN.md §4.1): boundary
    computation and application MUST stay in the SAME index space.

    ``_transform_messages`` (the api-form projection) is length/order-changing:
    one raw assistant row with N ``toolRounds`` expands into many api messages,
    endpoint sessions collapse, same-role rows merge.  So an index computed on
    the api-form list CANNOT be mapped 1:1 back to the raw
    ``conversations.messages`` list.  Slicing the raw list with an api-space
    index would split a tool round → orphan ``tool`` messages / a broken
    tool_call/result pair → HTTP 400.

    THE RULE — read-project, write-raw, compute-in-raw-space:
      * turn / boundary / anchor are defined on the RAW stored shape; the
        boundary ALWAYS lands on a raw ``user`` index and slicing is on raw.
      * per-turn token estimation is RAW-AWARE (``_raw_estimate_tokens``):
        it projects the turn slice through ``_transform_messages`` then
        ``_estimate_total_tokens`` — it does NOT call ``_estimate_msg_tokens``
        on a raw assistant row (that is api-form-aware and blind to
        ``toolRounds``, so it would massively under-count a tool-heavy turn).
      * ``_transform_messages`` is used ONLY read-only, at two points: to count
        a turn's tokens and to build the summary-input text.  Its output index
        is never used to slice the raw list.

Public surface:
  * ``compact_conversation_now`` — DB orchestrator (load → plan → archive →
    summarize → rewrite → persist).  Idle-only (caller gates on activeTaskId).
  * ``plan_manual_compaction``   — pure: computes system_end / boundary /
    anchor / old-region / reserve-region.  No DB, no LLM.
  * ``apply_manual_compaction``  — pure: rebuilds the raw message list.
  * ``_raw_turn_boundary`` / ``_raw_estimate_tokens`` — the raw-space helpers.
"""

from __future__ import annotations

import time

from lib.log import audit_log, get_logger
from lib.agent_core.store import get_conversation_store
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
)
from lib.tasks_pkg.compaction._layer2 import (
    _extract_current_query,
    _extract_recently_accessed_files,
    _generate_query_aware_summary,
    _objective_anchor_index,
)
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _usable_context,
)

logger = get_logger(__name__)

_SUMMARY_HEADER = '## 上下文已压缩（主动 /compact）'
"""Marker heading prepended to the persisted summary message body."""


def _project(raw_slice: list, config: dict | None = None) -> list:
    """Project a RAW message slice into api-form (READ-ONLY).

    Expands ``toolRounds`` into assistant(tool_calls)+tool pairs exactly as the
    live request path does, so token counts and summary input match what the
    LLM actually sees.  Always called with an EMPTY config so no system prompt
    is injected into the counted slice.  The returned list's indices are NEVER
    used to slice the raw list (see the module-level hard constraint).
    """
    from lib.tasks_pkg.conv_message_builder import _transform_messages
    try:
        return _transform_messages(list(raw_slice), {})
    except Exception as e:
        logger.warning('[ManualCompact] projection failed (%d raw msgs): %s',
                       len(raw_slice), e)
        return []


def _raw_estimate_tokens(raw_slice: list, config: dict | None = None) -> int:
    """Raw-aware token estimate for a slice of RAW conversation messages.

    Projects the slice to api-form (expanding ``toolRounds``) then sums the
    per-message estimate.  This is the ONLY correct way to size a raw turn:
    ``_estimate_msg_tokens`` on a raw assistant row reads ``content``/
    ``tool_calls`` and is blind to ``toolRounds``, so it under-counts a
    tool-heavy turn by the entire tool-args + tool-output payload.
    """
    return _estimate_total_tokens(_project(raw_slice, config))


def _system_end(raw_messages: list) -> int:
    """Index just past the leading ``system`` block in the RAW list.

    Raw ``conversations.messages`` normally carry no system row (the system
    prompt is injected by ``_transform_messages``), but we skip a leading
    system block defensively so the anchor/boundary never fall inside it.
    """
    end = 0
    for m in raw_messages:
        if isinstance(m, dict) and m.get('role') == 'system':
            end += 1
        else:
            break
    return end


def _raw_turn_boundary(
    raw_messages: list,
    *,
    config: dict | None = None,
    task: dict | None = None,
    max_turns: int = _MAX_PRESERVE_TURNS,
    budget_tokens: int | None = None,
) -> int:
    """Preservation boundary in RAW index space (mirror of _find_turn_boundary).

    A *turn* = ``[user_msg, ...all subsequent non-user raw messages]``.  Turns
    are atomic; the boundary ALWAYS falls on a raw ``user`` index so a tool
    round (a raw assistant row with its ``toolRounds``) is never split.

    Policy (identical to the L2 boundary, but RAW-AWARE token sizing):
      • HARD INVARIANT — the current (most-recent) turn is always preserved.
      • BEST-EFFORT    — older turns added newest→oldest while
        ``preserved + turn_tokens <= budget_tokens`` AND count ``<= max_turns``.
      • REFUSE         — no ``user`` row → returns ``len(raw_messages)`` so the
        caller short-circuits (nothing to compact).
    """
    config = config or {}
    user_idx = [i for i, m in enumerate(raw_messages)
                if isinstance(m, dict) and m.get('role') == 'user']
    if not user_idx:
        return len(raw_messages)

    if budget_tokens is None:
        usable = _usable_context(_get_context_limit(task))
        budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))

    turn_starts = user_idx
    turn_ends = user_idx[1:] + [len(raw_messages)]

    cur_start, cur_end = turn_starts[-1], turn_ends[-1]
    boundary = cur_start
    preserved = _raw_estimate_tokens(raw_messages[cur_start:cur_end], config)
    count = 1

    for k in range(len(turn_starts) - 2, -1, -1):
        if count >= max_turns:
            break
        start, end = turn_starts[k], turn_ends[k]
        tt = _raw_estimate_tokens(raw_messages[start:end], config)
        if preserved + tt > budget_tokens:
            break
        boundary = start
        preserved += tt
        count += 1

    return boundary


def plan_manual_compaction(
    raw_messages: list,
    *,
    config: dict | None = None,
    task: dict | None = None,
    max_turns: int | None = None,
) -> dict | None:
    """Compute the manual-compaction plan in RAW index space (pure, no DB/LLM).

    Returns a plan dict, or ``None`` when there is nothing to compact (history
    too short — the boundary would preserve everything after the system block).

    Plan keys:
      ``system_raw``  — leading system block (verbatim, kept at front)
      ``anchor_msg``  — first real user message IF it fell in the old region
                        (pulled out to survive verbatim), else ``None``
      ``old_raw``     — raw messages to be summarized (anchor excluded)
      ``reserve_raw`` — raw messages preserved verbatim (starts on a user row)
      ``boundary``    — raw index of the preservation boundary
    """
    config = config or {}
    resolved_max = _MAX_PRESERVE_TURNS if max_turns is None else max(1, int(max_turns))

    system_end = _system_end(raw_messages)
    boundary = _raw_turn_boundary(
        raw_messages, config=config, task=task, max_turns=resolved_max)

    # Nothing to compact: the boundary preserves everything past the system
    # block (single live turn, or an empty conversation).
    if boundary <= system_end:
        return None

    anchor_idx = _objective_anchor_index(raw_messages)
    anchor_msg = None
    old_raw = list(raw_messages[system_end:boundary])
    if anchor_idx is not None and system_end <= anchor_idx < boundary:
        anchor_msg = raw_messages[anchor_idx]
        old_raw = [m for k, m in enumerate(raw_messages[system_end:boundary],
                                           start=system_end)
                   if k != anchor_idx]

    if not old_raw:
        # Only the anchor was in the old region — summarizing nothing is a
        # no-op; treat as nothing-to-compact so we don't archive/rewrite.
        return None

    return {
        'system_end': system_end,
        'boundary': boundary,
        'anchor_msg': anchor_msg,
        'old_raw': old_raw,
        'reserve_raw': list(raw_messages[boundary:]),
        'system_raw': list(raw_messages[:system_end]),
    }


def _build_summary_message(
    summary_text: str,
    *,
    archive_id: int | None,
    tokens_after: int,
) -> dict:
    """Build the C1 summary message: a plain assistant row marked as a
    compaction boundary so the frontend renders it as a fold card and headless
    consumers see it as ordinary assistant text."""
    marker = {'archiveId': archive_id, 'trigger': 'manual',
              'ts': int(time.time())}
    return {
        'role': 'assistant',
        'content': f'{_SUMMARY_HEADER}\n\n{summary_text}',
        '_isCompactionSummary': True,
        '_compactionArchiveId': archive_id,
        # gauge scheme B: no real usage on this synthetic message, so the
        # frontend falls back to this estimate until the next real turn.
        '_estimatedPromptTokens': int(tokens_after or 0),
        '_compactions': [marker],
        'timestamp': int(time.time() * 1000),
    }


def apply_manual_compaction(
    raw_messages: list,
    plan: dict,
    summary_text: str,
    *,
    archive_id: int | None = None,
    tokens_after: int = 0,
) -> list:
    """Rebuild the RAW message list from a plan + summary (pure).

    Sequence: ``[system...] + [anchor user?] + [summary assistant] +
    [reserve...]``.  Because the boundary lands on a user index, ``reserve``
    starts with a ``user`` row, so the summary assistant is always followed by
    a user row → no assistant-assistant adjacency for
    ``_merge_consecutive_same_role`` to collapse (design test 7b).
    """
    anchor_block = [plan['anchor_msg']] if plan.get('anchor_msg') is not None else []
    summary_msg = _build_summary_message(
        summary_text, archive_id=archive_id, tokens_after=tokens_after)
    return (list(plan['system_raw'])
            + anchor_block
            + [summary_msg]
            + list(plan['reserve_raw']))


def compact_conversation_now(
    conv_id: str,
    *,
    config: dict | None = None,
    task: dict | None = None,
    keep_recent_turns: int | None = None,
) -> dict:
    """User-triggered persistent compaction of a conversation.

    Reuses the L2 summary engine but writes the result back to
    ``conversations.messages`` so subsequent turns start from a small context.
    Idle-only — the caller (REST route) MUST refuse when a task is running.

    Returns a dict: ``{ok, ...}`` on success or ``{ok: False, error}``.
    Error codes: ``not_found`` / ``nothing_to_compact`` / ``summary_failed`` /
    ``stale`` (a concurrent writer won the CAS).
    """
    config = config or {}
    log_id = conv_id[:8] if conv_id else '?'
    store = get_conversation_store()

    loaded = store.load_conversation_messages(conv_id)
    if loaded is None:
        logger.warning('[ManualCompact] conv=%s not found', log_id)
        return {'ok': False, 'error': 'not_found'}
    raw_messages, updated_at = loaded

    plan = plan_manual_compaction(
        raw_messages, config=config, task=task, max_turns=keep_recent_turns)
    if plan is None:
        logger.info('[ManualCompact] conv=%s nothing to compact (%d msgs)',
                    log_id, len(raw_messages))
        return {'ok': False, 'error': 'nothing_to_compact'}

    tokens_before = _raw_estimate_tokens(raw_messages, config)
    msgs_before = len(raw_messages)

    # ── ARCHIVE the full pre-compaction snapshot BEFORE any rewrite ──
    # (design test 3). trigger='manual'; no SSE event (no live task to carry it).
    archive_id = _archive_transcript(
        conv_id, raw_messages,
        trigger='manual', task=task, round_num=0,
        tokens_before=int(tokens_before), msgs_before=int(msgs_before),
        reason='manual /compact', emit_event=False)

    # ── SUMMARIZE the old region (reuse the L2 engine) ──
    old_api = _project(plan['old_raw'], config)
    current_query = _extract_current_query(_project(plan['reserve_raw'], config))
    summary_text = _generate_query_aware_summary(
        old_api, current_query, '[ManualCompact]', conv_id=conv_id, task=task)
    if not summary_text:
        logger.warning('[ManualCompact] conv=%s summary generation failed — '
                       'leaving conversation intact', log_id)
        return {'ok': False, 'error': 'summary_failed', 'archiveId': archive_id}

    recent_files = _extract_recently_accessed_files(_project(raw_messages, config))
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        summary_text += (
            '\n\n### Recently Accessed Files\n'
            'Use read_files to review current state if needed:\n'
            f'{file_list}')

    # ── REBUILD (raw shape) + measure the after-size (raw-aware) ──
    new_messages = apply_manual_compaction(
        raw_messages, plan, summary_text, archive_id=archive_id, tokens_after=0)
    tokens_after = _raw_estimate_tokens(new_messages, config)
    msgs_after = len(new_messages)
    reduction_pct = round((1 - tokens_after / max(1, tokens_before)) * 100, 1)
    # Stamp the full stats onto the summary message + its compaction marker so
    # the frontend card is a PURE render of a backend-downloaded fact (never a
    # client-side inference). See docs/MANUAL_COMPACTION_DESIGN.md §5.3.
    for m in new_messages:
        if isinstance(m, dict) and m.get('_isCompactionSummary'):
            m['_estimatedPromptTokens'] = int(tokens_after)
            for mk in m.get('_compactions', []):
                mk.update({'tokensBefore': int(tokens_before),
                           'tokensAfter': int(tokens_after),
                           'msgsBefore': int(msgs_before),
                           'msgsAfter': int(msgs_after),
                           'reductionPct': reduction_pct})

    # ── PERSIST (CAS on updated_at — a concurrent writer aborts us) ──
    affected = store.cas_update_conversation_messages(
        conv_id, new_messages, updated_at)
    if not affected:
        logger.warning('[ManualCompact] conv=%s CAS lost — concurrent write, '
                       'aborting (archive %s kept, harmless)', log_id, archive_id)
        return {'ok': False, 'error': 'stale', 'archiveId': archive_id}

    if archive_id is not None:
        try:
            store.update_archive_summary(
                archive_id, summary_text, int(tokens_after), int(msgs_after))
        except Exception as e:
            logger.debug('[ManualCompact] archive row update failed: %s', e)

    audit_log('manual_compaction', conv_id=conv_id, archive_id=archive_id,
              tokens_before=int(tokens_before), tokens_after=int(tokens_after),
              msgs_before=int(msgs_before), msgs_after=int(msgs_after))
    logger.info('[ManualCompact] conv=%s DONE  tokens: %d → %d (%.0f%%)  '
                'msgs: %d → %d  archive=%s',
                log_id, tokens_before, tokens_after, reduction_pct,
                msgs_before, msgs_after, archive_id)

    return {
        'ok': True,
        'archiveId': archive_id,
        'tokensBefore': int(tokens_before),
        'tokensAfter': int(tokens_after),
        'msgsBefore': int(msgs_before),
        'msgsAfter': int(msgs_after),
        'reductionPct': reduction_pct,
        'summaryPreview': summary_text[:500],
    }
