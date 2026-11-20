"""Watchdog tool adapter called by execute_coordinator_tool (docs/ROUTER.md R7).

Every call has already passed the coordinator binding check; this adapter
requires that binding to be the owner's watchdog path, then calls one owner:
the snapshot, the signal store, the scheduler review, the project mailbox or
the Global Router's ordinary turn command. Nothing here executes or delegates
work, creates a grant or cancels a Worker: the dispatch owner admits
cancellation only from the Worker's own Coordinator request or a human Stop.
Only ``watchdog_configure_review`` acts for the owner, and only while the
current request is the owner's own genuine message, resolved exactly like
delegation authority. ``resume_stalled_continuation`` is the one repair power:
it wakes a lead that durable state shows stalled, under the owner's full
delegation to every lead, and audits every outcome.
"""
from __future__ import annotations

import hashlib
import json

from lib.log import audit_log

from ..contract import validate


def _tenant(task: dict) -> str | None:
    from lib.identity import PrincipalContext
    return PrincipalContext.from_payload(task.get('_principalContext') or {}).tenant_id


def _resolve_own_failures(owner: int, watchdog_conversation_id: str) -> list[str]:
    """A Watchdog that is running again closes the signals of its own failed Turns."""
    from .signals import FaultSignals
    signals = FaultSignals(owner)
    own = [signal['signalId'] for signal in signals.open_turn_failures(watchdog_conversation_id)]
    if own:
        signals.resolve(own, 'The Watchdog is running again: a later turn of it called watchdog_snapshot.')
    return own


_WATCHDOG_MESSAGE_LABEL = ('[From the owner’s Router Watchdog: a nonhuman monitor, not the owner’s words '
                           'and not authority]\n')


def _message(task: dict, scope: dict, arguments: dict) -> dict:
    from lib.conversations.project_messages import ProjectMailbox, scope as mailbox_scope
    from .signals import _conversation_role
    arguments = validate(dict(arguments), 'watchdogMessage')
    owner, target = scope['user_id'], arguments['conversation_id']
    role = _conversation_role(owner, target, {'conversationId': scope['conversation_id'],
                                              'projectPath': scope['project_path']}, None)
    if role is None or role[0] == 'watchdog' or not role[1]:
        raise PermissionError('Watchdog messages go only to the owner\u2019s Routers and Workers')
    project = mailbox_scope(owner, target, role[1])
    message = ProjectMailbox(owner, project, target).submit(scope['conversation_id'], {
        'content': _WATCHDOG_MESSAGE_LABEL + arguments['content'], 'kind': arguments.get('kind', 'reference'),
        'idempotency_key': arguments['idempotency_key']}, task['id'])
    return {'message': message, 'recipient_role': role[0], 'project_path': project,
            'delivery': 'next_step', 'execution_started': False}


def render_router_notice(watchdog_conversation_id: str, message: str, signal_ids: list[str]) -> str:
    lines = ['[Watchdog notice: nonhuman evidence from the owner\u2019s Watchdog, not the owner\u2019s words '
             'and not authority]',
             f'Watchdog conversation: {watchdog_conversation_id}']
    if signal_ids:
        lines.append('Fault signals: ' + ', '.join(signal_ids))
    lines += ['Message:', message, '',
              'Decide whether, when and where the owner needs to hear about this; stay silent when nothing is '
              'needed. Read the Watchdog conversation and the cited evidence with get_conversation.']
    return '\n'.join(lines)


def _resolve_recovered_router_failures(owner: int, signals, failures: list[dict]) -> list[dict]:
    """Close failed-Turn signals of a Router that has since completed a Turn (docs/ROUTER.md R7).

    Only the Watchdog's own snapshot closed its failures, so a Router that recovered and went on
    working kept every signal it had opened and was refused notice as "still failing" — and the
    refusal itself told the Watchdog to resolve the signal before notifying. Recovery is read
    from durable Turns, not from the model's say-so: the newest main-lane Turn completed and is
    newer than the failed one. A still-failing, running or interrupted Router keeps its signals.
    """
    from lib.turn_lifecycle import LifecycleNotFound, get_turn, iter_turns_newest_first
    conversation_id = failures[0]['subject']['conversationId']
    latest = next(iter_turns_newest_first(conversation_id, user_id=owner), None)
    if latest is None or latest.get('status') != 'completed' or latest.get('laneId', 'main') != 'main':
        return failures
    latest_ordinal = int(latest.get('ordinal') or 0)
    remaining = []
    for signal in failures:
        try:
            turn = get_turn(conversation_id, signal['subject']['turnId'], user_id=owner,
                            include_projection=False)
        except LifecycleNotFound:
            remaining.append(signal)
            continue
        if (turn.get('laneId', 'main') != 'main' or int(turn.get('ordinal') or 0) >= latest_ordinal):
            remaining.append(signal)
            continue
        signals.resolve([signal['signalId']],
                        f'The Router recovered: main-lane Turn {latest["turnId"]} completed after the failed '
                        f'Turn {turn["turnId"]} (ordinal {latest_ordinal} > {int(turn.get("ordinal") or 0)}).')
    return remaining


def _notify_router(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    from lib.router.binding import global_router
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    from .signals import FaultSignals
    arguments = validate(dict(arguments), 'watchdogNotifyRouter')
    owner = scope['user_id']
    router = global_router(owner, create=False)
    if router['availability'] != 'available':
        raise PermissionError('The owner has no available Global Router to notify')
    signals = FaultSignals(owner)
    signal_ids = list(arguments.get('signal_ids') or [])
    for signal_id in signal_ids:
        if signals.get(signal_id) is None:
            raise ValueError(f'Unknown Watchdog signal {signal_id}')
    failures = signals.open_turn_failures(router['conversationId'])
    if failures:
        failures = _resolve_recovered_router_failures(owner, signals, failures)
    if failures:
        raise PermissionError('The Global Router\u2019s own latest turn failed and that signal is open: '
                              're-check until it has recovered and resolve the signal before notifying it')
    command_id = 'router-watchdog-notice:' + hashlib.sha256(json.dumps(
        [owner, scope['conversation_id'], arguments['idempotency_key'], arguments['message'], signal_ids],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    message = stamp_initiator({'role': 'user',
                               'content': render_router_notice(scope['conversation_id'], arguments['message'],
                                                               signal_ids),
                               'origin': {'initiator': INITIATOR_PEER,
                                          'sourceConversationId': scope['conversation_id']}}, INITIATOR_PEER)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    disposition = dispatch(router['conversationId'], message, {}, user_id=owner, command_id=command_id,
                           log_prefix='[RouterWatchdog]', inject_mode='steer').disposition
    if disposition not in ('started', 'steered', 'queued'):
        raise ValueError(f'The Global Router did not accept the notice ({disposition}); retry unchanged later')
    return {'router_conversation_id': router['conversationId'], 'disposition': disposition,
            'execution_started': False}


_AUDIT_EVENT = 'watchdog_resume_stalled_continuation'


def _stall_notice(watchdog_conversation_id: str, entry: dict, reason: str) -> str:
    lines = ['[Watchdog notice: nonhuman evidence from the owner\u2019s Watchdog, not the owner\u2019s words '
             'and not authority]',
             f'Watchdog conversation: {watchdog_conversation_id}',
             'Your main lane is idle while these Workers need you:']
    for worker in entry['workers']:
        lines.append(f'- {worker["conversation_id"]} (assignment {worker["assignment_id"]}, {worker["phase"]}): '
                     f'{worker["objective"]}')
    lines += ['Watchdog reason:', reason, '',
              'Review, follow up, re-delegate or cancel them as the request needs. You may act on Workers on '
              'your own judgment under the owner\u2019s delegation to every lead.']
    return '\n'.join(lines)


def _resume_stalled_continuation(task: dict, scope: dict, arguments: dict, *, dispatch=None,
                                 recover=None) -> dict:
    """Wake one stalled lead: its own continuation first, else one nonhuman notice (docs/ROUTER.md R7).

    The stall is re-read from durable state with the snapshot's predicate, never taken from the
    model; the owner's full delegation must cover the lead; every outcome writes one audit record.
    """
    from lib import lifecycle_approval as gate
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_messages import lead_role
    from lib.project_dispatch.delegation import LEAD_AUTHORITY
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    from .snapshot import lead_lane, lead_router_entry
    arguments = validate(dict(arguments), 'watchdogResumeStalledContinuation')
    owner, lead = scope['user_id'], arguments['conversation_id']
    record = {'owner': owner, 'watchdog_conversation_id': scope['conversation_id'], 'lead': lead,
              'reason': arguments['reason'], 'idempotency_key': arguments['idempotency_key']}

    def audited(outcome: str, **evidence) -> dict:
        audit_log(_AUDIT_EVENT, **record, outcome=outcome, evidence=evidence)
        return {'outcome': outcome, 'lead_conversation_id': lead, **evidence}

    role = None if lead == scope['conversation_id'] else lead_role(owner, lead)
    if role is None:
        audited('refused', code='not_a_lead')
        raise PermissionError('resume_stalled_continuation wakes only one of the owner\u2019s current leads '
                              '(the Global Router or a Project Router)')
    role_name, project = role
    binding = ProjectCoordinationRepository(owner).read(project)
    entry = lead_router_entry(owner, project, binding)
    evidence = {key: entry[key] for key in ('lead_turn', 'workers', 'activity_error', 'stall_reason')
                if key in entry}
    if not entry.get('stalled'):
        return audited('not_stalled', **evidence)
    requester = {'kind': 'global_router' if role_name == 'global_router' else 'project_lead',
                 'conversationId': lead, 'bindingRevision': binding['revision'], 'tenant': _tenant(task),
                 'turnId': None, 'attemptId': None}
    try:
        gate.check_delegation(owner, requester, LEAD_AUTHORITY)
    except gate.DelegationRefused as refusal:
        audited('refused', code=refusal.code, **evidence)
        raise
    if recover is None:
        from lib.project_dispatch.continuation import recover_coordinator
        from lib.project_dispatch.repository import ProjectDispatchRepository

        def recover():
            return recover_coordinator(ProjectDispatchRepository(owner, project))
    recovery = list(recover() or [])
    if not lead_lane(owner, lead)['idle']:
        return audited('resumed_by_continuation', recovery=recovery, **evidence)
    command_id = 'router-watchdog-resume:' + hashlib.sha256(json.dumps(
        [owner, lead, arguments['idempotency_key']], separators=(',', ':'),
        ensure_ascii=False).encode()).hexdigest()
    message = stamp_initiator({'role': 'user',
                               'content': _stall_notice(scope['conversation_id'], entry, arguments['reason']),
                               'origin': {'initiator': INITIATOR_PEER,
                                          'sourceConversationId': scope['conversation_id']}}, INITIATOR_PEER)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    # The default mode: a busy lane answers lane_busy and nothing is steered into a live Turn.
    sent = dispatch(lead, message, {}, user_id=owner, command_id=command_id,
                    log_prefix='[RouterWatchdog]', inject_mode='')
    if sent.disposition == 'busy':
        return audited('not_stalled', recovery=recovery, disposition='busy', **evidence)
    if sent.disposition != 'started':
        audited('refused', code='notice_not_started', recovery=recovery, disposition=sent.disposition,
                **evidence)
        raise ValueError(f'The lead did not accept the notice ({sent.disposition}); retry unchanged later')
    return audited('notice_started', recovery=recovery, command_id=command_id,
                   replayed=bool(getattr(sent, 'replayed', False)), **evidence)


def _require_owner_request(task: dict, scope: dict) -> None:
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    try:
        source_for_task(ProjectDispatchRepository(scope['user_id'], scope['project_path']), task)
    except (PermissionError, ValueError) as error:
        raise PermissionError('Only the owner\u2019s own message in this conversation can change the '
                              f'review ({error})') from error


def execute_watchdog_tool(task: dict, scope: dict, name: str, arguments: dict) -> dict:
    from .binding import is_watchdog_scope
    if not is_watchdog_scope(scope):
        raise PermissionError('Only the owner\u2019s Watchdog can use this tool')
    owner = scope['user_id']
    if name == 'watchdog_snapshot':
        from .snapshot import health_snapshot
        arguments = validate(dict(arguments), 'watchdogSnapshot')
        resolved = _resolve_own_failures(owner, scope['conversation_id'])
        if arguments.get('signals_after_id') is None:
            from .quota import settle_quota_alerts
            settle_quota_alerts(owner)
        snapshot = health_snapshot(owner, after_id=arguments.get('after_id'),
                                   signals_after_id=arguments.get('signals_after_id'))
        if resolved:
            snapshot['resolved_own_failures'] = resolved
        return snapshot
    if name == 'watchdog_resolve_signals':
        from .signals import FaultSignals
        arguments = validate(dict(arguments), 'watchdogResolveSignals')
        return {'resolved': FaultSignals(owner).resolve(arguments['signal_ids'], arguments['resolution'])}
    if name == 'watchdog_schedule_recheck':
        from .review import schedule_recheck, watchdog_principal
        return schedule_recheck(owner, scope['conversation_id'], arguments,
                                principal=watchdog_principal(owner, _tenant(task)))
    if name == 'watchdog_message':
        return _message(task, scope, arguments)
    if name == 'watchdog_notify_router':
        return _notify_router(task, scope, arguments)
    if name == 'resume_stalled_continuation':
        return _resume_stalled_continuation(task, scope, arguments)
    if name == 'watchdog_configure_review':
        from .review import configure_review, watchdog_principal
        arguments = validate(dict(arguments), 'watchdogReviewRequest')
        _require_owner_request(task, scope)
        return configure_review(owner, arguments, principal=watchdog_principal(owner, _tenant(task)))
    raise ValueError('Unknown Watchdog tool')


__all__ = ['execute_watchdog_tool', 'render_router_notice']
