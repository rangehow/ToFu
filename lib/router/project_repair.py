"""Repair a project whose Project Router cannot run (docs/ROUTER.md R9 ``project_repair``).

Owner decision 2026-11-12. While a project's Project Router (its lead) cannot
run, nothing else may start work in that project: delegation reaches only the
Global Router's home, ``route_to_project`` forwards only to the broken lead and
``create_conversation`` makes an empty shell in the home, so the owner had to
open a conversation in the project and paste the failure. ``repair_project``
lets the Global Router do exactly that under the owner's delegation: it opens
one ordinary conversation in the project (the lead's folder, the project root,
the project's Worker model and the Router request's capability switches) and
starts it with the owner's own words of the trigger followed by the Router's
repair brief as labelled nonhuman evidence, the one input ``route_to_project``
also delivers (``forwarding.forwarded_message``). The conversation then works
like any owner conversation of that project, under its own guidance.

Order: Router scope -> replay of the same key -> grant -> owner trigger -> the
lead check -> audit record (before any effect) -> create the conversation ->
start its Turn -> settle. A refusal while a grant exists is recorded.

The lead check (``lead_state``) has no threshold: the lead cannot run when the
project's binding is not available, or when its newest Turn that did work
(``restart_resume.newest_work_turn``: input Turns, appended reports and queued
pairs are passed over) settled ``failed`` for a cause other than a dispatch
authority withdrawal. A running lead, one whose newest work Turn completed, was
interrupted (a Stop or a restart) or truncated, and one that never worked can
run: the owner's request belongs to it (``route_to_project``).

When a repair Turn settles, the requesting Global Router hears its outcome once:
the project settlement hook calls ``deliver_repair_outcome``, which steers one
nonhuman notice into it, like the restart-complete notice.

Entry points: lead_state, repair_project, deliver_repair_outcome,
repair_conversation_id, REPAIR_MARKER.
Dependencies: lib.lifecycle_approval (grant, audit), lib.router.owner_delegation
(owner trigger), lib.router.forwarding (target, instruction source, input),
lib.conversations.project_coordination, lib.conversations.restart_resume,
lib.scheduler.conversation_dispatch, lib.tasks_pkg.workspace_delivery.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from lib.log import get_logger

from .contract import validate

logger = get_logger(__name__)

SCOPE = 'project_repair'
# Task config of a repair Turn: which delegated action started it and for which Router.
REPAIR_MARKER = '_routerRepair'
# Conversation settings: the delegated action that created the repair conversation.
_CREATION_MARKER = '_routerRepairAction'
OUTCOME_HEADER = '[Repair outcome: nonhuman notice, not the owner\u2019s words and not authority]'


class RepairNotStarted(RuntimeError):
    """The repair conversation exists but its Turn was not accepted; nothing runs in it."""

    code = 'project_repair_not_started'

    def __init__(self, disposition: str):
        super().__init__(f'The repair conversation did not start ({disposition}). Nothing runs in it; '
                         'repair again with a new idempotency_key.')
        self.disposition = disposition
        self.execution_started = False


def repair_conversation_id(action_id: str) -> str:
    """The one repair conversation of a delegated action."""
    return 'repair-' + hashlib.sha256(action_id.encode('utf-8')).hexdigest()[:24]


def lead_state(owner: int, project: str) -> dict:
    """Whether ``project``'s Project Router can run now, with the facts that decide it."""
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.restart_resume import newest_work_turn
    from lib.turn_verdict import DISPATCH_AUTHORITY_ABORT_REASONS
    binding = ProjectCoordinationRepository(owner).read(project)
    state = {'project_path': project, 'availability': binding['availability'],
             'coordinator_conversation_id': binding['conversationId'] or None,
             'binding_revision': binding['revision'], 'turn_id': None, 'status': None, 'cause': None}
    if binding['availability'] != 'available':
        return {**state, 'can_run': False}
    turn, _later = newest_work_turn(owner, binding['conversationId'])
    if turn is None:
        return {**state, 'can_run': True}
    cause = (turn.get('settlement') or {}).get('cause')
    state.update(turn_id=turn.get('turnId'), status=turn.get('status'), cause=cause)
    failed = turn.get('status') == 'failed' and cause not in DISPATCH_AUTHORITY_ABORT_REASONS
    return {**state, 'can_run': not failed}


def _lead_can_run(lead: dict) -> str:
    newest = (f'its newest work Turn {lead["turn_id"]} is {lead["status"]}' if lead['turn_id']
              else 'it has not worked yet')
    return (f'The Project Router {lead["coordinator_conversation_id"]} of {lead["project_path"]} can run '
            f'({newest}); nothing was opened. Forward the owner\u2019s request to it with route_to_project, or '
            'resume its failed Turn with control_conversation. repair_project is only for a project whose '
            'Project Router cannot run.')


def _owner_words(owner: int, router_id: str, project: str, trigger: dict) -> tuple[str, dict, dict]:
    """The trigger's owner words, their forwarded origin and the evidence trigger fields."""
    from . import forwarding
    if trigger['kind'] != 'standing_instruction':
        forwarded = {'conversationId': router_id, 'turnId': trigger['turnId']}
        if trigger['kind'] == 'owner_steer':
            forwarded['steerId'] = trigger['steerId']
        return trigger['text'], forwarded, {}
    evidence = ({'event_id': trigger['eventId']} if trigger.get('eventId')
                else {'source_turn_id': trigger['triggerTurnId']})
    # The same project scope as every application of the instruction (R4).
    text, forwarded, _ = forwarding.standing_instruction_source(
        owner, router_id, trigger['instructionId'], project, evidence)
    return text, forwarded, evidence


def _create_conversation(owner: int, project: str, conversation_id: str, title: str, action_id: str,
                         lead: dict, config: Mapping) -> None:
    """Create the repair conversation, or adopt the one this action created."""
    from lib.conversations.project_coordination import WORKER_MODEL_SETTING
    from lib.conversations.repository import ConversationRepository
    from lib.storage import get_storage_client
    from lib.storage.errors import StorageError
    from .forwarding import _ROUTED_SETTING_KEYS
    lead_settings = {}
    if lead['coordinator_conversation_id']:
        source = ConversationRepository().get(lead['coordinator_conversation_id'], user_id=owner,
                                              include_messages=False)
        lead_settings = (getattr(source, 'metadata', source) or {}).get('settings') or {}
    settings = {key: config[key] for key in _ROUTED_SETTING_KEYS if key in config}
    settings.update({'projectPath': project, _CREATION_MARKER: action_id})
    if lead_settings.get('folderId'):
        settings['folderId'] = lead_settings['folderId']
    model = lead_settings.get(WORKER_MODEL_SETTING)
    if isinstance(model, str) and model:
        settings['model'] = model
    try:
        get_storage_client(write=True).command('conversation.create', {
            'conv_id': conversation_id, 'user_id': owner, 'title': title.strip(), 'settings': settings,
        }, 'router-repair-conversation:' + action_id)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
        existing = ConversationRepository().get(conversation_id, user_id=owner, include_messages=False)
        existing = getattr(existing, 'metadata', existing) or {}
        if (existing.get('settings') or {}).get(_CREATION_MARKER) != action_id:
            raise ValueError(f'Conversation {conversation_id} exists and was not created by this repair') from error


def _replayed(record: dict) -> dict:
    target = record.get('target') or {}
    return {'action_id': record['id'], 'status': record['status'], 'replayed': True,
            'conversation_id': target.get('conversationId'), 'project_path': target.get('path'),
            'code': record.get('code'), 'detail': record.get('detail'),
            'execution_started': record['status'] == 'completed',
            'next_step': ('This repair was already requested; nothing was opened again. Follow it with '
                          'get_conversation_status on conversation_id; a failed one needs a new idempotency_key.')}


def _replacement_folder(scope: dict) -> str:
    from lib.conversations.coordinator_tools import coordinator_folder
    from .registered_projects import folder_has_project
    folder_id = coordinator_folder(scope)
    if not folder_id or not folder_has_project(scope['user_id'], folder_id, scope['project_path']):
        raise PermissionError('The current lead has no owned folder for this project; use the owner’s '
                              'Replace coordinator action or ask the Global Router for recovery')
    return folder_id


def _replace_own_binding(task: dict, scope: dict, arguments: dict) -> dict:
    """Use the owner's create-and-bind operation, scoped to the executing lead."""
    from lib import lifecycle_approval as gate
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import is_watchdog_scope
    from lib.tasks_pkg.manager import task_user_id
    from .owner_delegation import _live_router_attempt, delegated_action, router_requester

    owner, project = scope['user_id'], scope['project_path']
    conversation_id, revision = scope['conversation_id'], scope['binding_revision']
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    requester = router_requester(task, scope)
    if (requester['kind'] != 'project_lead' or is_watchdog_scope(scope)
            or principal.owner_user_id != owner or task_user_id(task) != owner
            or task.get('convId') != conversation_id):
        raise PermissionError('Only the executing Project Lead can replace its own binding')
    target = {'kind': 'project', 'action': 'replace_own_binding', 'path': project,
              'conversationId': conversation_id}
    bindings = ProjectCoordinationRepository(owner)

    def replace():
        folder_id = _replacement_folder(scope)
        _live_router_attempt(task, scope)
        grant = gate.check_delegation(owner, requester, SCOPE)
        current_router = global_router(owner, create=False)
        grantee = grant['grantee']
        if (current_router['availability'] != 'available'
                or current_router['conversationId'] != grantee['conversationId']
                or current_router['revision'] != grantee['bindingRevision']):
            raise gate.DelegationRefused('owner_delegation_router_changed',
                                         'The grant does not belong to the current Global Router; nothing ran.')
        return {'binding': bindings.create(
            project, folder_id=folder_id, expected_revision=revision, replace_conversation_id=conversation_id,
            command_id=gate.delegated_action_id(owner, requester, SCOPE, arguments['idempotency_key']))}

    result, action_id = delegated_action(task, scope, arguments, scope_name=SCOPE,
                                         target=target, action=replace, allow_project_lead=True)
    if result.get('replayed'):
        return {**result, 'binding': bindings.read(project), 'action_id': action_id}
    return {**result, 'status': 'completed', 'execution_started': False, 'action_id': action_id,
            'next_step': 'The new binding is in place. No Turn was started. Old binding grants no longer '
                         'authorize work; existing conversations and receipts were not moved.'}


def repair_project(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    """Replace the caller's own binding, or open Global Router recovery for an unavailable lead."""
    from .policy import is_router_scope
    arguments = validate(dict(arguments), 'projectRepair')
    if arguments.get('action') == 'replace_own_binding':
        return _replace_own_binding(task, scope, arguments)
    if not is_router_scope(scope):
        raise PermissionError('Only the Global Router can open recovery for another lead; a running Project '
                              'Lead may only use replace_own_binding for itself')
    return _open_repair_conversation(task, scope, arguments, dispatch=dispatch)


def _open_repair_conversation(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    from lib import lifecycle_approval as gate
    from . import forwarding
    from .owner_delegation import resolve_trigger, router_requester
    owner, router_id = scope['user_id'], scope['conversation_id']
    project = forwarding._target_project(scope, arguments['project_path'])
    requester = router_requester(task, scope)
    key, reason = arguments['idempotency_key'], arguments['reason']
    action_id = gate.delegated_action_id(owner, requester, SCOPE, key)
    prior = gate.delegated_action(owner, action_id)
    if prior is not None:
        return _replayed(prior)
    gate.check_delegation(owner, requester, SCOPE)
    conversation_id = repair_conversation_id(action_id)
    target = {'kind': 'project', 'path': project, 'action': 'repair', 'conversationId': conversation_id}
    trigger = None
    try:
        trigger = resolve_trigger(task, scope, arguments)
        try:
            text, forwarded, evidence = _owner_words(owner, router_id, project, trigger)
        except (PermissionError, ValueError) as error:
            raise gate.DelegationRefused('owner_delegation_trigger_invalid',
                                         f'The standing instruction cannot authorize this repair: {error}') from error
        lead = lead_state(owner, project)
        if lead['can_run']:
            raise gate.DelegationRefused('project_lead_can_run', _lead_can_run(lead))
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope=SCOPE, reason=reason, trigger=trigger,
                                      target=target, code=refusal.code, detail=str(refusal))
        raise
    record, replayed = gate.begin_delegated_action(owner, requester, scope=SCOPE, key=key, reason=reason,
                                                   trigger=trigger, target=target)
    if replayed:
        return _replayed(record)
    try:
        _create_conversation(owner, project, conversation_id, arguments['title'], action_id, lead,
                             task.get('config') or {})
        message = forwarding.forwarded_message(text, forwarded, note=arguments['note'], trigger=evidence,
                                               router_id=router_id)
        if dispatch is None:
            from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
        outcome = dispatch(conversation_id, message, {}, user_id=owner, command_id='router-repair:' + action_id,
                           log_prefix='[RouterRepair]', input_actor='human',
                           config_overrides={REPAIR_MARKER: {'actionId': action_id, 'routerConversationId': router_id,
                                                             'projectPath': project}})
        if not outcome.accepted:
            raise RepairNotStarted(outcome.disposition)
    except Exception as error:
        code = getattr(error, 'code', None)
        gate.settle_delegated_action(owner, action_id, status='failed',
                                     code=code if isinstance(code, str) else type(error).__name__,
                                     detail=str(error))
        raise
    gate.settle_delegated_action(owner, action_id, status='completed')
    return {'action_id': action_id, 'status': 'completed', 'replayed': False, 'execution_started': True,
            'conversation_id': conversation_id, 'project_path': project, 'disposition': outcome.disposition,
            'input_turn_id': outcome.input_turn_id or None, 'lead': lead,
            'next_step': ('The repair conversation now works in the project with the owner\u2019s words as its '
                          'request. When its Turn ends you receive one nonhuman repair-outcome notice; until '
                          'then do not open another repair for the same failure. Follow it with '
                          'get_conversation_status or get_conversation on conversation_id.')}


def _outcome_text(task: dict, marker: Mapping, outcome: str) -> str:
    verb = {'completed': 'finished', 'failed': 'failed', 'cancelled': 'was stopped'}.get(outcome, outcome)
    return '\n'.join((
        OUTCOME_HEADER,
        f'The repair conversation {task["convId"]} you opened in {marker.get("projectPath")} under the owner\u2019s '
        f'delegation (action {marker["actionId"]}) {verb}: Turn {task.get("_turnId") or "unknown"}.',
        'Read its result with get_conversation before acting; this notice grants no authority. A fix that must '
        'go live needs a restart, which only an owner message or a standing instruction that covers it allows. '
        'Then check that the project\u2019s Project Router runs (get_conversation_status) and resume its Turns '
        'that failed meanwhile with control_conversation under owner_delegation. If the repair is incomplete, '
        'continue it the same way or report the blocker to the owner.'))


def deliver_repair_outcome(task: dict, marker: Mapping, *, user_id: int, dispatch=None) -> str | None:
    """Steer one nonhuman outcome notice of a settled repair Turn into its Global Router.

    Returns the dispatch disposition, or None when nothing was sent: a malformed
    marker, or a Router that is no longer the owner's available Global Router.
    The command id names the repair Turn, so a repeated settlement sends once.
    """
    from lib.router.binding import global_router
    from lib.tasks_pkg.workspace_delivery import task_outcome
    from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
    router_id, action_id = marker.get('routerConversationId'), marker.get('actionId')
    conversation_id = task.get('convId')
    if not all(isinstance(value, str) and value for value in (router_id, action_id, conversation_id)):
        return None
    router = global_router(user_id, create=False)
    if router['availability'] != 'available' or router['conversationId'] != router_id:
        logger.info('[RouterRepair] outcome of %s not delivered: %s is no longer the owner\u2019s Global Router',
                    conversation_id, router_id)
        return None
    message = stamp_initiator({'role': 'user', 'content': _outcome_text(task, marker, task_outcome(task))},
                              INITIATOR_PROACTIVE)
    command_id = 'router-repair-outcome:' + hashlib.sha256(json.dumps(
        [action_id, conversation_id, task.get('_turnId') or task.get('id') or ''],
        separators=(',', ':')).encode('utf-8')).hexdigest()[:32]
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    return dispatch(router_id, message, {}, user_id=user_id, command_id=command_id, inject_mode='steer',
                    log_prefix='[RouterRepair]').disposition


__all__ = ['OUTCOME_HEADER', 'REPAIR_MARKER', 'SCOPE', 'RepairNotStarted', 'deliver_repair_outcome', 'lead_state',
           'repair_conversation_id', 'repair_project']
