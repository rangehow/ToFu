"""Model-switch visibility: the fallback event, lead audit and round evidence.

Entry: :func:`stamp_model_switch` (fallback) and :func:`stamp_switch_back`
(next round tries the primary again, :func:`_call._llm_call_with_fallback`).
Both are called at the round's start, before that round's model is chosen, and
only stamp when the last round's model differs from this round's, so the
``MODEL_FALLBACK`` event names every switch exactly once in both directions.
"""

from __future__ import annotations

from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.log import get_logger, task_log_tag

logger = get_logger(__name__)


def _lead_audit_switch(task, *, direction, source, destination, reason, round_num):
    """Every switch of a project Worker is lead-readable without transcripts.

    Leads are ``coordinator-*`` conversations; the receiver reads the stream
    with ``get_conversation`` rather than the evicted app log.
    """
    from lib.logging_pkg import lead_audit

    conv = str(task.get('convId') or '')
    if not lead_audit.is_lead_conversation(conv):
        return
    lead_audit.record('model_switch', {
        'task': str(task.get('id') or ''),
        'conv': conv,
        'turn': str((task.get('config') or {}).get('_turnId') or ''),
        'direction': direction,
        'from': source,
        'to': destination,
        'round': int(round_num) + 1,
        'reason': str(reason or '')[:200],
    })


def stamp_model_switch(task, *, source, destination, kind, reason,
                       detail_key=None, detail_args=None, round_num=0,
                       direction='fallback'):
    """Publish a switch and restamp the task's fallback reporting fields."""
    from lib.tasks_pkg.manager import _events as task_events

    if direction == 'primary':
        phase = build_phase(
            Phase.RETRYING,
            detail=f'模型 {source} 拒绝仅针对该次请求 — 本轮重试 {destination}…',
            detailKey=detail_key or 'stream.phase.refusalSwitchback',
            detailArgs=detail_args or {'model': source, 'fallback': destination},
        )
    elif detail_key:
        phase = build_phase(
            Phase.RETRYING,
            detail=(
                f'⚠️ 模型 {source} 拒绝回答 — '
                f'已自动改用 {destination} 重新生成…'
            ),
            detailKey=detail_key,
            detailArgs=detail_args or {'model': source, 'fallback': destination},
        )
    else:
        phase = build_phase(
            Phase.RETRYING,
            detail=f'⚠️ 模型 {source} 请求失败 — 已自动回退到 {destination} 继续生成…',
        )
    task_events.append_event(task, phase)
    task_events.append_event(task, build_event(
        EventType.MODEL_FALLBACK,
        fallbackModel=destination,
        fallbackFrom=source,
        fallbackKind=kind,
        fallbackReason=str(reason or '')[:300],
    ))
    task['_fallback_model'] = destination
    task['_fallback_from'] = source
    task['_fallback_reason'] = str(reason or '')[:300]
    task['_fallback_kind'] = kind
    _lead_audit_switch(task, direction=direction, source=source,
                       destination=destination, reason=reason, round_num=round_num)
    logger.info('[%s] conv=%s Model %s: %s → %s (round %d, reason: %s)',
                task_log_tag(task['id']), task.get('convId', ''),
                'return to primary' if direction == 'primary' else 'fallback',
                source, destination, round_num + 1, str(reason or '')[:200])


def _history_digest(messages):
    """Exact digest of the request's message history.

    Compaction, clearing old tool results and every injection change the
    bytes; a refusal is tied to those bytes, so a changed digest is the exact
    signal that the primary may now answer. No knob, threshold or counter.
    """
    from lib.utils import sha256_holding_gil
    import json
    try:
        blob = json.dumps(messages, sort_keys=True, default=str,
                          ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        # A circular or unencodable history has no digest; refusal tracking
        # then treats every round as unchanged history.
        logger.debug('message history digest unavailable: %s', exc)
        return ''
    return sha256_holding_gil(blob).hexdigest()


def record_refused_history(task, messages):
    """Remember the refused PREFIX: its length and that prefix's digest.

    A tool loop appends each served round's assistant message and tool results
    to the same list, so comparing the WHOLE list would always differ and
    retry the primary every round. Only the prefix that was actually sent when
    the primary refused can change the verdict: compaction or clearing rewrites
    it, and a list shorter than it means the history was replaced.
    """
    task['_refused_history_length'] = len(messages or ())
    task['_refused_history_digest'] = _history_digest(
        list(messages or ())[:task['_refused_history_length']])


def _appended_rows_are_tool_rounds(rows):
    """True for the rows ONE attempt's tool loop appends between its requests.

    The served assistant tool-call row and its tool results. A new request
    (the next user Turn) or any other injection ends the refusal's exemption.
    """
    return bool(rows) and all(
        row.get('role') == 'tool'
        or (row.get('role') == 'assistant' and row.get('tool_calls'))
        for row in rows)


def retry_primary_for_messages(task, messages):
    """True when this request must go back to the primary after a refusal.

    A content_filter refusal is a verdict on ONE request, never a demotion of
    the conversation's primary model: the NEXT request returns to it
    (docs/contracts/TURN_SETTLEMENT.md). Only a request that is exactly the
    refused history plus the same attempt's own appended tool rows stays on
    the fallback, so a refused history does not bill a probe on every tool
    round of one attempt. Everything else retries the primary: the
    conversation's next request, a compaction/clearing that rewrites the
    refused prefix, a shorter or replaced history, any other appended
    injection, or a legacy stamp with no refused prefix.
    """
    length = task.get('_refused_history_length')
    refused = task.get('_refused_history_digest') or ''
    current = list(messages or ())
    if (type(length) is int and 0 <= length < len(current) and refused
            and _history_digest(current[:length]) == refused
            and _appended_rows_are_tool_rounds(current[length:])):
        return False
    return True


def stamp_switch_back(task, round_num, messages=None):
    """Return the next request after a content_filter fallback to the primary.

    The round that refused is served on the fallback; the following request
    goes back to the primary and a repeated refusal falls back again for
    that request only. Only the refused attempt's own appended tool rounds
    skip the return probe (see :func:`retry_primary_for_messages`), and the
    return emits the same ``MODEL_FALLBACK`` event / lead-audit
    ``model_switch`` row (direction ``primary``) as the fallback.
    """
    if task.get('_fallback_kind') != 'content_filter':
        return False
    destination = str(task.get('_fallback_from') or '').strip()
    source = str(task.get('_fallback_model') or '').strip()
    # Only a round that actually ran on the fallback elects the return probe;
    # a bare ``_fallback_from`` stamp (legacy rows, an explicit switch)
    # carries no model to return from.
    if not destination or not source or destination == source:
        return False
    if not retry_primary_for_messages(task, messages):
        return False
    stamp_model_switch(
        task, source=source, destination=destination,
        kind='primary_retry', direction='primary',
        reason=f'The refusal applies to one request; round {round_num + 1} returns to the primary model.',
        detail_key='stream.phase.refusalSwitchback', round_num=round_num,
    )
    return True


def clear_switch_stamps(task):
    """A successful primary round retires the sticky fallback reporting."""
    task.pop('_fallback_model', None)
    task.pop('_fallback_from', None)
    task.pop('_fallback_reason', None)
    task.pop('_fallback_kind', None)
    task.pop('_refused_history_digest', None)
    task.pop('_refused_history_length', None)


__all__ = ['stamp_model_switch', 'stamp_switch_back', 'clear_switch_stamps',
           'record_refused_history', 'retry_primary_for_messages',
           '_history_digest']
