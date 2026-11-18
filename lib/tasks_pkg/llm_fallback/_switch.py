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


def refused_history_changed(task, messages):
    """True when the refused prefix changed (or is unknown) → retry the primary.

    Appended fallback rounds leave the prefix byte-identical, so they never
    retry. Compaction/clearing rewrites it, and a shorter list is a change too.
    """
    length = task.get('_refused_history_length')
    refused = task.get('_refused_history_digest') or ''
    if type(length) is not int or length < 0 or not refused:
        # Legacy stamp without a prefix: keep the conservative retry.
        return True
    current = list(messages or ())[:length]
    if len(messages or ()) < length:
        return True
    return _history_digest(current) != refused


def stamp_switch_back(task, round_num, messages=None):
    """Probe the primary again only when the refused history actually changed.

    A content_filter refusal is tied to the conversation history, and refusals
    come in runs that persist until old tool results are cleared or the
    history is compacted. Retrying the primary on an unchanged history would
    add a refused request (and its billed prompt) to every round of exactly
    the lanes that refuse most, for no benefit. So the primary is retried
    only when the history differs from the one that was refused — a
    compaction, cleared tool results, new attachments/inbox, any real change.
    Otherwise the attempt stays on the fallback. No knobs or counters.
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
    # History-change gate on the REFUSED PREFIX only: appending this
    # attempt's own fallback rounds must not count as a history change.
    if not refused_history_changed(task, messages):
        return False
    stamp_model_switch(
        task, source=source, destination=destination,
        kind='primary_retry', direction='primary',
        reason=f'The history changed since the refusal (compaction/cleared results); retrying the primary on round {round_num + 1}.',
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
           'record_refused_history', 'refused_history_changed', '_history_digest']
