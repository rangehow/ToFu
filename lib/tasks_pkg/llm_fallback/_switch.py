"""Model-switch visibility: the fallback event, lead audit and round evidence.

Entry: :func:`stamp_model_switch` (fallback) and :func:`stamp_switch_back`
(next round tries the primary again, :func:`_call._llm_call_with_fallback`).
Selection and preparation are read-only. The irreversible lease dispatch
fence commits only local policy. Completed return-switch evidence publishes
after the primary attempt; local publication failures cannot retry a provider.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def _publish_model_switch(task, *, source, destination, kind, reason,
                          detail_key=None, detail_args=None, round_num=0,
                          direction='fallback'):
    """Publish fallible switch evidence without committing local policy."""
    try:
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
        _lead_audit_switch(task, direction=direction, source=source,
                           destination=destination, reason=reason, round_num=round_num)
        logger.info('[%s] conv=%s Model %s: %s → %s (round %d, reason: %s)',
                    task_log_tag(task['id']), task.get('convId', ''),
                    'return to primary' if direction == 'primary' else 'fallback',
                    source, destination, round_num + 1, str(reason or '')[:200])
        return {
            '_fallback_model': destination,
            '_fallback_from': source,
            '_fallback_reason': str(reason or '')[:300],
            '_fallback_kind': kind,
        }
    except Exception as error:
        from lib.llm import AbortedError
        from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
        if isinstance(error, (AbortedError, EventDeliveryError, EventDeliveryRevoked)):
            raise
        raise EventDeliveryError('Model-switch evidence publication failed') from error


def stamp_model_switch(task, *, source, destination, kind, reason,
                       detail_key=None, detail_args=None, round_num=0,
                       direction='fallback'):
    """Publish a switch and restamp the task's fallback reporting fields."""
    task.update(_publish_model_switch(
        task, source=source, destination=destination, kind=kind, reason=reason,
        detail_key=detail_key, detail_args=detail_args, round_num=round_num,
        direction=direction))


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
    except Exception:
        return ''
    return sha256_holding_gil(blob).hexdigest()


def record_refused_history(task, messages):
    """Remember the refused PREFIX: its length and that prefix's digest.

    A tool loop appends each served round's assistant message and tool results
    to the same list, so comparing the WHOLE list would always differ and
    retry the primary every round. Only the prefix that was actually sent when
    the primary refused can change the verdict: compaction or clearing rewrites
    it, and a list shorter than it means the history was replaced.

    ``_refusal_served_length`` is set when the fallback actually ANSWERS, and
    names the history length at that moment. Only the immediately following
    tool round repeats those exact bytes; every later request returns to the
    primary. The old rule latched the fallback for the whole attempt whenever
    the refused prefix stayed unchanged (the worker-25bb6da1 incident).
    """
    task['_refused_history_length'] = len(messages or ())
    task['_refused_history_digest'] = _history_digest(
        list(messages or ())[:task['_refused_history_length']])
    # The ONE request that repeats the refused payload plus the fallback's
    # just-served answer skips the return probe. ``_refusal_skip_left`` is
    # consumed by that request and NOT re-armed by a repeated refusal on the
    # return probe: the probe's own fallback answer is a new answer, but the
    # primary must still be attempted again on the next request rather than
    # latching the fallback for the whole attempt.
    task.setdefault('_refusal_skip_left', 1)


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
    (docs/contracts/TURN_SETTLEMENT.md), including the tool rounds of the
    refused attempt itself. Only the request that repeats the refused history
    plus the fallback's own just-served answer skips that return — probing the
    primary on those exact bytes would refuse the same payload the fallback
    already answered. Any later request returns to the primary: subsequent
    tool rounds, the conversation's next request, a compaction/clearing that
    rewrites the refused prefix, a shorter or replaced history, any other
    appended injection, or a legacy stamp. A repeated refusal on the return
    probe does not re-arm the consumed exemption: every subsequent request
    must attempt the primary again.
    """
    length = task.get('_refused_history_length')
    refused = task.get('_refused_history_digest') or ''
    current = list(messages or ())
    if (type(length) is int and 0 <= length < len(current) and refused
            and _history_digest(current[:length]) == refused
            and _appended_rows_are_tool_rounds(current[length:])
            and task.get('_refusal_skip_left')):
        return False
    return True


def prepare_switch_back(task, round_num, messages=None):
    """Prepare evidence for a possible primary request without publishing it.

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
    return dict(
        source=source, destination=destination,
        kind='primary_retry', direction='primary',
        reason=f'The refusal applies to one request; round {round_num + 1} returns to the primary model.',
        detail_key='stream.phase.refusalSwitchback', round_num=round_num,
    )


def stamp_switch_back(task, round_num, messages=None):
    prepared = prepare_switch_back(task, round_num, messages)
    if not prepared:
        return False
    task.update(_publish_model_switch(task, **prepared))
    task['_refusal_skip_left'] = 0
    return True


@dataclass(frozen=True, slots=True, eq=False)
class RequestModelDecision:
    """One dispatch's model selection, passed explicitly from build to call.

    Each instance identifies one request, even when round numbers repeat. It
    stays caller-owned, never cached on the task or reused for a later round.
    """

    model: str
    exempt_history_digest: str = ''


def decide_request_model(task, model, round_num, messages, decision=None):
    """Choose without mutation; changed payload bytes invalidate an exemption."""
    if decision is not None and (
        not decision.exempt_history_digest
        or decision.exempt_history_digest == _history_digest(messages)
    ):
        return decision
    if not (task.get('config') or {}).get('disableModelFallback'):
        primary = str(task.get('_fallback_from') or '').strip()
        fallback = str(task.get('_fallback_model') or '').strip()
        if (primary and fallback and primary != fallback
                and task.get('_fallback_kind') == 'content_filter'):
            if retry_primary_for_messages(task, messages):
                model = primary
            else:
                return RequestModelDecision(fallback, _history_digest(messages))
    return RequestModelDecision(model)


def prepare_request_model(task, decision, round_num, messages):
    """Prepare return-switch evidence; denied requests never publish it."""
    if not decision.exempt_history_digest and not (
            task.get('config') or {}).get('disableModelFallback'):
        return prepare_switch_back(task, round_num, messages)
    return None


def commit_request_model(task, decision, prepared_switch):
    """Commit only infallible local policy after the lease dispatch fence."""
    if prepared_switch:
        task.update({
            '_fallback_model': prepared_switch['destination'],
            '_fallback_from': prepared_switch['source'],
            '_fallback_reason': str(prepared_switch['reason'] or '')[:300],
            '_fallback_kind': prepared_switch['kind'],
        })
    if decision.exempt_history_digest or prepared_switch:
        task['_refusal_skip_left'] = 0


def publish_request_model(task, prepared_switch):
    """A completed switch reports an actual physical attempt, never a plan."""
    if prepared_switch:
        _publish_model_switch(task, **prepared_switch)


def clear_switch_stamps(task):
    """A successful primary round retires the sticky fallback reporting."""
    task.pop('_fallback_model', None)
    task.pop('_fallback_from', None)
    task.pop('_fallback_reason', None)
    task.pop('_fallback_kind', None)
    task.pop('_refused_history_digest', None)
    task.pop('_refused_history_length', None)
    task.pop('_refusal_skip_left', None)


__all__ = ['stamp_model_switch', 'stamp_switch_back', 'clear_switch_stamps',
           'record_refused_history', 'retry_primary_for_messages',
           '_history_digest', 'RequestModelDecision', 'decide_request_model']
