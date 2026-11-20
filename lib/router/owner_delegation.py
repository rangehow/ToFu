"""The owner's delegation to their Global Router: act as the owner, never speak as them (ROUTER.md R9).

Owner decision 2026-11-06. The owner grants (only in the Tofu UI) or revokes
one delegation that their current Global Router conversation holds. The
grant, its audit records and the restart acceptance live in the lifecycle
approval gate (``lib.lifecycle_approval``), the single decision point for
live-server lifecycle actions; this module adds what only the router knows:

* the owner trigger of an action: the owner's own message of the current
  request, an owner message steered into the reply, or an active standing
  instruction with its triggering event (resolved by the forwarding owner,
  exactly as ``route_to_project`` resolves them);
* ``restart_server``: the Settings restart path (frontend preparation first,
  then the shared ``perform_server_reexec`` lifecycle owner) behind the
  restart-protection signal (``list_running_tasks``), detached from the turn;
* ``delegated_conversation_control``: the grant branch of
  ``control_conversation`` for requests that have no owner message of their
  own (standing instructions, nonhuman turns), limited to Continue, Stop and a
  session model change (``set_model``, docs/ROUTER.md R5).

The grant never lets a lead answer for the owner, approve a prompt meant
for the owner or forward its own text as the owner's: those keep their
human-only gates (``require_human_turn``, lifecycle approvals) and no model
tool can write the grant. A full grant reaches every lead (owner decision
2026-11-18): a Project Router acts as ``project_lead`` (``restart_server``,
and ``lead_authority`` for Workers in ``lib.project_dispatch.delegation``),
and ``under_delegation`` is the trigger of an action a lead takes on its own
judgment, recorded with its reason. The one
conversation it can start is a repair conversation in a project whose
Project Router cannot run, with the owner's own words as its request
(``lib.router.project_repair``, owner decision 2026-11-12).

Entry points: router_requester, resolve_trigger, restart_server,
delegated_conversation_control, delegated_action, owner_delegation_state.
Dependencies: lib.lifecycle_approval, lib.router.forwarding,
lib.router.instructions, lib.tasks_pkg.manager.list_running_tasks,
lib.server.server_reexec.

delegated_action(task, scope, arguments, *, scope_name, target, action) -> (result, action_id)
    The one generic R9 gate for delegated scopes without a bespoke tool
    (owner_settings, project_lifecycle). Same order as
    delegated_conversation_control: grant -> owner trigger -> audit record
    (before any effect) -> action() -> settle. ``action`` is a zero-arg
    callable performing the effect through its domain owner.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from lib.log import get_logger

from .contract import CONTRACT, validate

logger = get_logger(__name__)

RESTART_REASON = 'restart'


def _refused(code: str, message: str):
    from lib.lifecycle_approval import DelegationRefused
    return DelegationRefused(code, message)


# The owner's words that delegate to every lead; the trigger of an action a lead
# takes on its own judgment (``under_delegation``) records them.
LEAD_DELEGATION_WORDS = ('多去分析一下各个负责人的轨迹，看看我们现在机制有没有什么不足之处，我的权限要全部下放给'
                         '负责人们，也就是说，负责人的能力边界要尽可能扩展开。')


def router_requester(task: dict, scope: dict) -> dict:
    """The acting lead: conversation, binding revision, tenant, Turn and Attempt.

    ``kind`` is ``global_router`` for the owner's Global Router, else
    ``project_lead``: a Project Router, identified by the coordination binding
    its scope was resolved from (owner decision 2026-11-18, docs/ROUTER.md R9).
    """
    from lib.identity import PrincipalContext
    from .policy import is_router_scope
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if principal.owner_user_id not in (None, scope['user_id']):
        raise PermissionError('The task principal belongs to another owner')
    return {'kind': 'global_router' if is_router_scope(scope) else 'project_lead',
            'conversationId': scope['conversation_id'],
            'bindingRevision': scope['binding_revision'], 'tenant': principal.tenant_id,
            'turnId': task.get('_turnId'), 'attemptId': task.get('_attemptId')}


def resolve_trigger(task: dict, scope: dict, arguments: dict) -> dict:
    """The owner's own authority for this action, with their verbatim words.

    Exactly one trigger: ``source_turn_id`` (the owner's message of the
    current request), ``source_steer_id`` (an owner message steered into the
    current reply) or ``instruction_id`` with exactly one of ``event_id`` or
    ``source_turn_id`` (an active standing instruction of this router), the
    contract's ``ownerDelegationTrigger`` rule. Refusals are
    DelegationRefused, which every caller records under the grant:
    ``owner_delegation_trigger_missing`` (no trigger named) or
    ``owner_delegation_trigger_malformed`` (the rule broken), checked before
    anything is read, and ``owner_delegation_trigger_invalid`` when the named
    authority is not genuine.
    """
    from . import forwarding
    from .instructions import RouterInstructions, authored_text
    try:
        validate(arguments, 'ownerDelegationTrigger')
    except ValueError as error:
        named = any(arguments.get(name) is not None
                    for name in CONTRACT['$defs']['ownerDelegationTrigger']['properties'])
        raise _refused('owner_delegation_trigger_malformed' if named else 'owner_delegation_trigger_missing',
                       f'Refused before anything ran: {error}') from error
    if arguments.get('under_delegation') is True:
        try:
            _live_router_attempt(task, scope)
        except (PermissionError, ValueError) as error:
            raise _refused('owner_delegation_trigger_invalid',
                           f'Only your current executing reply acts under the delegation: {error}') from error
        return {'kind': 'owner_delegation', 'turnId': task['_turnId'], 'text': LEAD_DELEGATION_WORDS}
    instruction_id = arguments.get('instruction_id')
    steer_id = arguments.get('source_steer_id')
    turn_id = arguments.get('source_turn_id')
    event_id = arguments.get('event_id')
    owner, router_id = scope['user_id'], scope['conversation_id']
    if instruction_id is None:
        try:
            if steer_id is not None:
                steered = forwarding._current_steer_source(task, scope, steer_id)
                return {'kind': 'owner_steer', 'turnId': steered['turnId'], 'steerId': steer_id,
                        'text': authored_text(steered)}
            source = forwarding._current_request_source(task, scope)
            if turn_id != source['turnId']:
                raise PermissionError('source_turn_id must be the owner\u2019s own message of your current '
                                      f'request (Turn {source["turnId"]})')
            turn = forwarding._router_turn(owner, router_id, source['turnId'])
            return {'kind': 'owner_message', 'turnId': source['turnId'], 'text': authored_text(turn)}
        except (PermissionError, ValueError) as error:
            raise _refused('owner_delegation_trigger_invalid',
                           f'No owner request authorizes this: {error}. Act only on the owner\u2019s own '
                           'message, an owner message sent while you reply, or an active standing '
                           'instruction.') from error
    from lib.turn_initiation import require_human_turn
    try:
        _live_router_attempt(task, scope)
        instruction = RouterInstructions(owner).read(instruction_id)
        if instruction['state'] != 'active':
            raise PermissionError(f'standing instruction {instruction_id} is {instruction["state"]}')
        if instruction['routerConversationId'] != router_id:
            raise PermissionError('the standing instruction belongs to an earlier Global Router')
        source_turn = forwarding._router_turn(owner, router_id, instruction['sourceTurnId'])
        require_human_turn(source_turn)
        if authored_text(source_turn) != instruction['text']:
            raise PermissionError('the standing instruction source Turn changed')
        if turn_id is not None:
            forwarding._router_turn(owner, router_id, turn_id)
    except (PermissionError, ValueError) as error:
        raise _refused('owner_delegation_trigger_invalid',
                       f'The standing instruction cannot authorize this: {error}') from error
    trigger = {'kind': 'standing_instruction', 'turnId': instruction['sourceTurnId'],
               'instructionId': instruction_id, 'text': instruction['text']}
    trigger.update({'eventId': event_id} if event_id is not None else {'triggerTurnId': turn_id})
    return trigger


def _live_router_attempt(task: dict, scope: dict) -> None:
    """The task is the executing, unstopped attempt of the current Global Router binding.

    The fences ``forwarding._current_steer_source`` applies to steered owner
    messages; owner messages get them from delegation's source resolution.
    """
    from lib.project_dispatch.repository import ProjectDispatchRepository, stopped_after
    repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    if task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set():
        raise PermissionError('Your current reply was stopped')
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner,
                               include_projection=False)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('Only your current executing reply can act for the owner')
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Your current reply was stopped')


def _restart_protection(router_conversation_id: str, interrupt: bool, *,
                        steps_refuse: bool = True) -> list[str]:
    """Apply the Settings restart guard; return the conversations a restart interrupts.

    The signal is ``list_running_tasks`` (durable jobs, local carriers and
    protected steps), excluding the Router's own conversation as Settings
    excludes the caller's. A protected step refuses once this restart holds
    precedence (only a marker it cannot wait for remains); before that,
    ``steps_refuse=False`` lets the restart wait for them instead. Other running
    conversations refuse unless the owner asked to interrupt them (the
    Settings force). An unreadable signal never proves idle.
    """
    from lib.tasks_pkg.manager import list_running_tasks
    try:
        running = list_running_tasks(exclude_conv_id=router_conversation_id)
    except Exception as error:
        raise _refused('restart_protection_unavailable',
                       'Active execution could not be verified; the server was not restarted. Retry '
                       f'when storage answers ({type(error).__name__}).') from error
    steps = [item for item in running if item.get('protectedStep')]
    if not steps_refuse:
        running = [item for item in running if not item.get('protectedStep')]
    elif steps:
        markers = ', '.join(f"{item['protectedStep']} ({item.get('marker') or item['taskId']})" for item in steps)
        raise _refused('restart_protected_step',
                       f'Restart refused: a protected step is in progress ({markers}); a restart now would '
                       'boot from a half-landed checkout. Retry after it finishes.')
    conversations = sorted({item.get('convId') or item['taskId'] for item in running})
    if conversations and not interrupt:
        raise _refused('restart_would_interrupt',
                       f'Restart refused: {len(conversations)} other conversation(s) are running and a '
                       f'restart would interrupt them ({", ".join(conversations)}). Restart anyway only if '
                       'the owner asked for it even while work runs (interrupt_running_conversations=true); '
                       'interrupted conversations resume from their checkpoint by themselves after the '
                       'restart, and the restart-complete notice lists each one\u2019s resume result.')
    return conversations


def _serving_checkout() -> str:
    """The checkout the serving instance restarts from (``server_reexec``'s restart project)."""
    return os.path.realpath(os.environ.get('TOFU_PROJECT_PATH')
                            or os.path.join(os.path.dirname(__file__), '..', '..'))


def _engine_readiness(project: str) -> str:
    """HEAD's storage engine is the deployed one or certified; else the restart is refused now.

    ``not_applicable`` without a native storage engine (SQLite, a frozen distribution).
    """
    from pathlib import Path
    from runtime_guards import load_deployment_configuration
    from serverctl_pkg import native_engine
    from serverctl_pkg.engine_certification import passing_record
    from tofu_dotenv import read_dotenv_values
    root = Path(project)
    environment = {**read_dotenv_values(root / '.env'), **os.environ}
    if load_deployment_configuration(environment).storage_backend != 'tofudb':
        return 'not_applicable'
    try:
        expected = native_engine.source_fingerprint(root)
        if expected is None:
            return 'not_applicable'
        try:
            deployed = native_engine.managed_engine_destination(root, environment)
            if deployed.is_file() and native_engine.build_identity(deployed)['source_sha256'] == expected:
                return 'deployed'
        except (OSError, native_engine.EnginePreparationError):
            pass
        record, problem = passing_record(root, expected, native_engine.storage_contract_sha256(root))
    except (OSError, native_engine.EnginePreparationError) as error:
        raise _refused('restart_readiness_unavailable',
                       f'Restart refused before anything ran: HEAD\u2019s storage engine identity could not be '
                       f'read ({error}).') from error
    if record is None:
        raise _refused('restart_engine_uncertified',
                       f'Restart refused before anything ran: HEAD\u2019s storage engine {expected[:12]} is not '
                       f'certified ({problem}), so the restart would wait hours for the engine gate or be '
                       'refused by the manager. Have it certified first (scripts/engine_certify.py, or the '
                       'background pre-certification), then ask again.')
    return 'certified'


def _supervisor_readiness(project: str) -> str:
    """The server manager has loaded this checkout's source generation, refreshed first if not.

    The refresh is the documented pre-step (re-executes only the manager, under
    its own pid; the worker keeps serving), within the delegate's own budget.
    ``unmanaged`` when no manager of this checkout answers: the restart then
    takes the in-place bridge, and a manager of another checkout is never touched.
    """
    from serverctl_pkg.application_restart import DELEGATION_TIMEOUT_SECONDS
    from supervisor_protocol import (DEFERRED_RESTART_REQUEST_SECONDS, SupervisorRefreshError, refresh_supervisor,
                                     request_supervisor_json, supervisor_base_url, supervisor_generation_matches)
    try:
        health = request_supervisor_json(supervisor_base_url(), '/health', timeout=2.0)
    except SupervisorRefreshError:
        return 'unmanaged'
    if supervisor_generation_matches(health, project):
        return 'current'
    projects = health.get('projects') if isinstance(health, dict) else None
    if health.get('ok') is not True or not isinstance(projects, list) or project not in projects:
        return 'unmanaged'
    try:
        refresh_supervisor(project, timeout=DELEGATION_TIMEOUT_SECONDS - DEFERRED_RESTART_REQUEST_SECONDS)
    except SupervisorRefreshError as error:
        raise _refused('restart_supervisor_stale',
                       'Restart refused before anything ran: the server manager runs an older source generation '
                       f'and its refresh failed ({error}); the server keeps serving.') from error
    return 'refreshed'


def _restart_readiness() -> dict:
    """The machine pre-checks of every delegated restart, before it is recorded (ROUTER.md R9)."""
    project = _serving_checkout()
    return {'engine': _engine_readiness(project), 'supervisor': _supervisor_readiness(project)}


def _lead_bindings(owner: int) -> list[tuple[str, str]]:
    """``(project path, lead conversation)`` of every available lead of the owner, the Global Router included."""
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.watchdog.binding import is_watchdog_location
    from lib.storage import get_storage_client
    from .projects import binding_page
    leads, cursor = [], None
    repository = ProjectCoordinationRepository(owner)
    while True:
        bindings, page, cursor = binding_page(owner, cursor, get_storage_client)
        for path in page:
            if is_watchdog_location(bindings.user_id, path):
                continue
            binding = repository.read(path)
            if binding.get('availability') == 'available' and binding.get('conversationId'):
                leads.append((path, binding['conversationId']))
        if cursor is None:
            return leads


def _notify_peer_leads(owner: int, sender: str, sender_project: str, task_id: str, peers: list[dict],
                       content: str, key: str) -> list[dict]:
    """One cross-lead ``report`` (R13 mailbox) to each peer; idempotent by ``key``. Returns who got it."""
    from lib.conversations.project_messages import ProjectMailbox
    reached = []
    for peer in peers:
        try:
            ProjectMailbox(owner, peer['project'], peer['conversationId']).submit(
                sender, {'content': content, 'kind': 'report', 'idempotency_key': key}, task_id,
                sender_project=sender_project)
        except Exception:  # one unreachable lead never withholds the restart or the others' reports
            logger.warning('[Router] restart report to lead %s failed', peer.get('conversationId'), exc_info=True)
            continue
        reached.append(peer)
    return reached


def _announce_restart(task: dict, scope: dict, record: dict) -> list[dict]:
    """Tell every other lead a delegated restart holds the single flight; returns the leads told."""
    from lib.conversations.project_identity import normalize_project_path
    owner, sender = scope['user_id'], scope['conversation_id']
    sender_project = normalize_project_path(scope['project_path'])
    try:
        peers = [{'project': path, 'conversationId': lead} for path, lead in _lead_bindings(owner)
                 if lead != sender]
    except Exception:
        logger.warning('[Router] the leads to tell of restart %s could not be listed', record['id'],
                       exc_info=True)
        peers = []
    content = (f'[Restart under the owner\u2019s delegation: nonhuman report, not authority] Lead {sender} '
               f'({record["requester"]["kind"]}) started restart {record["id"]}. Reason: {record["reason"]} '
               'One restart runs at a time: restart_server is refused until this one ends, and you will be '
               'told its outcome here. Your running work is interrupted only if that lead asked to interrupt '
               'running conversations; interrupted work resumes from its checkpoint by itself.')
    reached = _notify_peer_leads(owner, sender, sender_project, task['id'], peers, content,
                                 f'restart-started:{record["id"]}')
    gate_peers = [{'project': peer['project'], 'conversationId': peer['conversationId']} for peer in reached]
    from lib import lifecycle_approval as gate
    gate.note_restart_notice(record['id'], peers=gate_peers, requesterProject=sender_project)
    return gate_peers


def report_restart_outcome(notice: dict, outcome: dict) -> list[dict]:
    """Tell the leads told of a delegated restart its outcome (restart_notice delivery calls this).

    Idempotent per restart: every delivery pass repeats the same report keys.
    """
    peers = notice.get('peers') or []
    if not peers or notice.get('source') != 'owner_delegation':
        return []
    sender = notice.get('recipient') or ''
    code = f' ({outcome["code"]})' if outcome.get('code') else ''
    content = (f'[Restart outcome: nonhuman report, not authority] Restart {notice["restartId"]} by lead '
               f'{sender} ended {outcome["status"]}{code}. {outcome.get("detail") or ""} '
               'restart_server is available to every lead again.')
    return _notify_peer_leads(notice['owner'], sender, notice.get('requesterProject') or '',
                              'restart-notice:' + notice['restartId'], peers, content,
                              f'restart-outcome:{notice["restartId"]}')


def _start_detached(work) -> None:
    threading.Thread(target=work, name='tofu-router-restart', daemon=True).start()


def restart_server(task: dict, scope: dict, arguments: dict) -> dict:
    """Restart the Tofu server for the owner through the sanctioned Settings path.

    Order: grant (gate) \u2192 owner trigger \u2192 restart protection \u2192 audit record
    (gate, one transaction) \u2192 detached: frontend preparation \u2192 protection
    again \u2192 gate acceptance (grant, cooldown, stamp) \u2192 the shared lifecycle
    owner stops the old server. A failure before acceptance keeps it serving.
    """
    from lib import lifecycle_approval as gate
    arguments = validate(dict(arguments), 'restartServer')
    owner = scope['user_id']
    requester = router_requester(task, scope)
    target = {'kind': 'server'}
    reason = arguments['reason']
    action_id = gate.delegated_action_id(owner, requester, 'restart', arguments['idempotency_key'])
    prior = gate.delegated_action(owner, action_id)
    if prior is not None:
        return _restart_view(prior, replayed=True)
    gate.check_delegation(owner, requester, 'restart')
    interrupt = bool(arguments.get('interrupt_running_conversations'))
    trigger = None
    try:
        # One restart at a time across every lead, and never in an owner's blackout window.
        gate.check_restart_admission(owner)
        trigger = resolve_trigger(task, scope, arguments)
        # Landings in progress are waited for under precedence, not refused.
        _restart_protection(scope['conversation_id'], interrupt, steps_refuse=False)
        if gate.restart_cooldown_remaining() > 0:
            raise _refused('restart_cooldown', 'Another restart was accepted moments ago; nothing ran')
        readiness = _restart_readiness()
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope='restart', reason=reason, trigger=trigger,
                                      target=target, code=refusal.code, detail=str(refusal))
        raise
    try:
        # The gate decides the single flight and the blackout again in the transaction that records it.
        record, replayed = gate.begin_delegated_action(
            owner, requester, scope='restart', key=arguments['idempotency_key'], reason=reason,
            trigger=trigger, target=target)
    except gate.DelegationRefused as refusal:
        if refusal.code in ('restart_in_progress', 'restart_blackout'):
            gate.record_delegated_refusal(owner, requester, scope='restart', reason=reason, trigger=trigger,
                                          target=target, code=refusal.code, detail=str(refusal))
        raise
    view = _restart_view(record, replayed=replayed)
    if not replayed:
        gate.note_restart_notice(record['id'], readiness=readiness)
        logger.info('[Router] delegated restart %s by %s %s: readiness %s', record['id'], requester['kind'],
                    requester['conversationId'], readiness)
        view['readiness'] = readiness
        view['leads_notified'] = [peer['conversationId'] for peer in _announce_restart(task, scope, record)]
        requested_at = time.time()
        _start_detached(lambda: _restart_after_acceptance(
            owner, record['id'], requester, requested_at, interrupt))
    return view


def _restart_next_step(status: str) -> str:
    """What happens next for a restart in ``status``; never a promise the server does not keep."""
    if status in ('accepted', 'restarting'):
        return ('The restart runs detached from this turn: the builds run first and the old server stops '
                'only after they succeed. Tell the owner it is under way, then end your turn. When the restart '
                'ends, whether completed, refused or failed, this conversation receives one nonhuman '
                'restart-complete notice as a new turn, with the outcome, the old and new commit, the build '
                'results and the resume result of each interrupted conversation; interrupted conversations '
                'resume from their checkpoint by themselves. Report the outcome to the owner from that notice. '
                'The owner sees every step in the main coordinator\u2019s "Act for me" log. Never call '
                'restart_server again for the same request.')
    if status == 'refused':
        return ('This request was refused before anything ran: nothing restarted and no notice follows. Tell '
                'the owner why; a new restart needs a new request once the cause is fixed.')
    return (f'This restart already ended ({status}). Its one nonhuman restart-complete notice reaches this '
            'conversation as a new turn if it has not already; report the outcome to the owner from it. Never '
            'call restart_server again for the same request.')


def _restart_view(record: dict, *, replayed: bool) -> dict:
    return {'action_id': record['id'], 'status': record['status'], 'replayed': replayed,
            'execution_started': record['status'] in ('accepted', 'restarting', 'completed'),
            'code': record.get('code'), 'detail': record.get('detail'),
            'next_step': _restart_next_step(record['status'])}


def _prepare(action_id: str) -> tuple[str, dict]:
    """The Settings preparation, observed for the restart-complete notice's build results."""
    from lib.server import server_reexec
    from serverctl_pkg.native_engine import observe_preparation
    phases: list[str] = []

    def observe(progress):
        phase = progress.get('phase') if isinstance(progress, dict) else None
        if isinstance(phase, str) and phase not in phases:
            phases.append(phase)
    started = time.monotonic()
    with observe_preparation(observe):
        error = server_reexec.prepare_server_reexec_frontend()
    return error, {'ok': not error, 'phases': phases, 'seconds': round(time.monotonic() - started, 1),
                   'detail': (error or '')[:2000] or None}


def _restart_after_acceptance(owner: int, action_id: str, requester: dict, requested_at: float,
                              interrupt: bool) -> None:
    from lib import boot_identity
    from lib import lifecycle_approval as gate
    from lib.server import server_reexec
    from lib.server.restart_notice import deliver_restart_notice
    from lib.runtime.protected_steps import restart_precedence
    old_code = boot_identity.code_fingerprint()
    try:
        # New landings wait for this restart; running ones finish first.
        with restart_precedence(f'Global Router restart {action_id}') as precedence:
            error, preparation = _prepare(action_id)
            if error:
                gate.note_restart_notice(action_id, preparation=preparation, oldCode=old_code)
                gate.settle_delegated_action(owner, action_id, status='failed',
                                             code='restart_preparation_failed', detail=error)
                return
            interrupted = _restart_protection(requester['conversationId'], interrupt)
            accepted = gate.accept_delegated_restart(owner, action_id, requester, interrupted=interrupted,
                                                     old_code=old_code, preparation=preparation)
            if accepted.get('status') != 'restarting':
                return
            outcome = server_reexec.perform_server_reexec(RESTART_REASON, frontend_prevalidated=True,
                                                          requested_at=requested_at)
            failure = server_reexec.await_managed_restart_failure(outcome, requested_at)
            if outcome.handed_over and not failure:
                precedence.retain_until_exit()
            else:
                gate.settle_delegated_action(owner, action_id, status='failed',
                                             code='restart_bridge_refused', detail=failure)
    except gate.DelegationRefused as refusal:
        gate.note_restart_notice(action_id, oldCode=old_code)
        gate.settle_delegated_action(owner, action_id, status='failed', code=refusal.code,
                                     detail=str(refusal))
    except Exception as error:  # a detached thread has no caller to report to
        logger.exception('[Router] delegated restart %s failed', action_id)
        gate.settle_delegated_action(owner, action_id, status='failed', code='restart_failed',
                                     detail=f'{type(error).__name__}: {error}')
    finally:
        # Every refusal above left the old server serving: tell the requester now.
        # A handed-over restart has no outcome yet, so this delivers nothing.
        deliver_restart_notice(action_id)


def delegated_conversation_control(task: dict, scope: dict, arguments: dict, *, control):
    """Audited delegated session control: Continue, checkpoint resume, Stop, hold/resume or set_model.

    Only set_model may omit the explicit authority object: the tool adapter
    selects this path when the current owner request does not supply authority
    for the target. The existing full grant and live-attempt trigger still apply.

    The grant, the owner trigger and the audit record precede ``control``, the
    shared session command (``session_control.control_session``). It runs as
    the owner's delegation, which stands in for an owner message of the
    current request; the target keeps its own accepted execution identity.
    Its outcome settles the record. Regenerate needs the owner's own request.

    Every lead may call it (a Project Router only under the full grant, which
    ``check_delegation`` enforces). It reaches a session of another project
    (docs/ROUTER.md R9 "Cross-project action"): the audit record names the
    target project (``target.path``) and that project's lead is told.
    """
    from lib import lifecycle_approval as gate
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.project_messages import scope as conversation_project
    from lib.identity import PrincipalContext
    action = arguments.get('action')
    explicit = arguments.get('owner_delegation')
    standing = action == 'set_model' and explicit is None
    authority = validate(
        {'reason': arguments.get('reason'), 'under_delegation': True} if standing else dict(explicit),
        'ownerDelegationAuthority')
    for name in ('conversation_id', 'turn_id', 'attempt_id', 'idempotency_key'):
        if not isinstance(arguments.get(name), str) or not arguments[name].strip():
            raise ValueError(f'{name} is required')
    if action not in ('continue', 'checkpoint_resume', 'stop', 'hold', 'resume', 'set_model'):
        raise ValueError('Under the owner\u2019s delegation, only continue, checkpoint_resume, stop, hold, '
                         'resume and set_model are available; regenerate needs the owner\u2019s own request, and '
                         'follow_up belongs to the Worker\u2019s own Project Coordinator')
    owner = scope['user_id']
    requester = router_requester(task, scope)
    target = {'kind': 'conversation', 'conversationId': arguments['conversation_id'],
              'turnId': arguments['turn_id'], 'attemptId': arguments['attempt_id'], 'action': action}
    # The Global Router acts on every project as its own reach (R9); a Project Router's
    # session of another project is a cross-project action: recorded with its project,
    # and that project's lead is told.
    cross_project = False
    if requester['kind'] == 'project_lead':
        target_project = conversation_project(owner, arguments['conversation_id'])
        cross_project = target_project != normalize_project_path(scope['project_path'])
        if cross_project:
            target['path'] = target_project
    if action == 'set_model':
        # Checked before the gate records anything: every record stays an ownerDelegatedAction.
        from lib.conversations.session_model import validate_request
        target['model'] = validate_request(arguments)[1]
    grant = gate.check_delegation(owner, requester, 'conversation_control')
    try:
        if standing and grant['coverage'] != 'full':
            raise _refused('owner_delegation_scope', 'A marker-free model change by a lead needs the owner’s '
                           'full delegation. A narrower grant requires an explicit owner trigger.')
        trigger = resolve_trigger(task, scope, authority)
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope='conversation_control',
                                      reason=authority['reason'], trigger=None, target=target,
                                      code=refusal.code, detail=str(refusal))
        raise
    record, _ = gate.begin_delegated_action(
        owner, requester, scope='conversation_control', key=arguments['idempotency_key'],
        reason=authority['reason'], trigger=trigger, target=target)
    acting = PrincipalContext.user(subject_id='owner-delegation:' + record['id'], owner_user_id=owner,
                                   tenant_id=requester['tenant'])
    command = {name: value for name, value in arguments.items() if name != 'owner_delegation'}
    try:
        result = control(task, scope, command, acting_principal=acting)
    except Exception as error:
        # A replay settles only what no earlier call settled.
        if record['status'] == 'accepted':
            code = getattr(error, 'code', None)
            gate.settle_delegated_action(owner, record['id'], status='failed',
                                         code=code if isinstance(code, str) else type(error).__name__,
                                         detail=str(error))
        raise
    if record['status'] != 'completed':
        gate.settle_delegated_action(owner, record['id'], status='completed')
    result = {**result, 'owner_delegation_action_id': record['id']}
    if cross_project:
        result['target_lead_notified'] = _notify_target_lead(
            task, scope, target_project, record, target, authority['reason'])
    return result


def _notify_target_lead(task: dict, scope: dict, project: str, record: dict, target: dict,
                        reason: str) -> str | None:
    """Tell the lead of ``project`` what another lead did there under the delegation.

    One cross-lead report (docs/ROUTER.md R13) per audit record, idempotent by
    its id. Returns the lead conversation it reached, or None when the project
    has no available lead (the audit record remains the evidence).
    """
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.project_messages import ProjectMailbox
    owner = scope['user_id']
    binding = ProjectCoordinationRepository(owner).read(project)
    lead = binding.get('conversationId')
    if binding.get('availability') != 'available' or not lead or lead == scope['conversation_id']:
        return None
    content = (f'Under the owner\u2019s delegation, lead {scope["conversation_id"]} ran {target["action"]} on '
               f'conversation {target["conversationId"]} (Turn {target["turnId"]}, Attempt '
               f'{target["attemptId"]}) in your project. Reason: {reason} Audit record: {record["id"]}. '
               'Read get_conversation_status before acting on that session yourself.')
    ProjectMailbox(owner, project, lead).submit(
        scope['conversation_id'], {'content': content, 'kind': 'report',
                                   'idempotency_key': 'owner-delegation:' + record['id']},
        task['id'], sender_project=normalize_project_path(scope['project_path']))
    return lead


def delegated_action(task: dict, scope: dict, arguments: dict, *, scope_name: str,
                     target: dict, action, allow_project_lead: bool = False) -> tuple[dict, str]:
    """One delegated effect through the R9 gate, for scopes without a bespoke tool.

    The owner instruction is authoritative (never the agent's claim). The grant,
    the owner trigger and one audit record are resolved and persisted BEFORE
    ``action`` runs; its outcome is settled. ``action`` is a zero-arg callable
    that performs the effect through its domain owner (Settings backend, channel
    service, project folders), never a second authority. Returns the effect's
    result and the audit action id. A DelegationRefused raised by the effect is
    settled ``failed`` and re-raised; any other exception is settled ``failed``
    with its type name. Callers opting into Project Lead authority must supply
    their own live, operation-specific scope fence. The gate still requires full coverage.
    """
    from lib import lifecycle_approval as gate
    from .policy import is_router_scope
    if not allow_project_lead and not is_router_scope(scope):
        raise PermissionError('Only the owner\u2019s Global Router can act under the owner\u2019s delegation')
    owner = scope['user_id']
    requester = router_requester(task, scope)
    reason = arguments.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('reason is required: why the owner asked for this')
    key = arguments.get('idempotency_key') or f'{scope_name}:{secrets.token_hex(16)}'
    gate.check_delegation(owner, requester, scope_name)
    trigger = None
    try:
        trigger = resolve_trigger(task, scope, arguments)
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope=scope_name, reason=reason,
                                      trigger=None, target=target, code=refusal.code, detail=str(refusal))
        raise
    record, replayed = gate.begin_delegated_action(
        owner, requester, scope=scope_name, key=key, reason=reason, trigger=trigger, target=target)
    action_id = record['id']
    if replayed:
        # Never applied twice. A replay of a record still ``accepted`` names the
        # interruption so the caller reads the effect's state back instead of guessing.
        return {'status': record['status'], 'code': record.get('code'),
                'detail': record.get('detail'), 'replayed': True,
                'interrupted': record['status'] == 'accepted',
                'execution_started': False}, action_id
    try:
        result = action()
    except gate.DelegationRefused as refusal:
        gate.settle_delegated_action(owner, action_id, status='failed', code=refusal.code,
                                     detail=str(refusal))
        raise
    except Exception as error:
        code = getattr(error, 'code', None)
        gate.settle_delegated_action(owner, action_id, status='failed',
                                     code=code if isinstance(code, str) else type(error).__name__,
                                     detail=str(error))
        raise
    gate.settle_delegated_action(owner, action_id, status='completed')
    return (result if isinstance(result, dict) else {'result': result}), action_id


def owner_delegation_state(owner: int) -> dict:
    """The owner's grant, whether it covers the current Global Router, and its audit records."""
    from lib import lifecycle_approval as gate
    from lib.router.binding import global_router
    grant = gate.delegation(owner)
    router = global_router(owner, create=False)
    grantee = grant.get('grantee') or {}
    active = (grant['state'] == 'granted' and router['availability'] == 'available'
              and grantee.get('conversationId') == router['conversationId']
              and grantee.get('bindingRevision') == router['revision'])
    return validate({'grant': grant, 'active': active,
                     'router': {key: router[key] for key in ('conversationId', 'revision', 'availability')},
                     'actions': gate.delegated_actions(owner)}, 'ownerDelegationState')


__all__ = ['RESTART_REASON', 'delegated_action', 'delegated_conversation_control', 'owner_delegation_state',
           'report_restart_outcome', 'resolve_trigger', 'restart_server', 'router_requester']
