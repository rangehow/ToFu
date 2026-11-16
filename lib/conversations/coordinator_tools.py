"""Coordinator adapters over existing conversation and project-dispatch owners.

Creation is empty/create-only. Delegation derives a bounded grant from the current
accepted human request; dispatch and observation reuse ProjectDispatchService.
Global Router tools (docs/ROUTER.md) delegate to lib.router.tools after the same
binding check. This module never synthesizes human principals or starts a model loop.
"""
from __future__ import annotations

import hashlib
import json

from lib.log import get_logger

logger = get_logger(__name__)


def _bound_scope(task: dict) -> dict:
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task)
    if not scope:
        raise PermissionError('Only the bound project Coordinator can use this tool')
    return scope


def _scope(task: dict) -> dict:
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    scope = _bound_scope(task)
    binding = ProjectCoordinationRepository(scope['user_id']).read(scope['project_path'])
    if (binding['availability'] != 'available'
            or binding['conversationId'] != scope['conversation_id']
            or binding['revision'] != scope['binding_revision']):
        raise PermissionError('Coordinator binding changed; start a new authorized turn')
    return scope


def worker_identity(scope: dict, key: str) -> tuple[str, str]:
    """``(conversation ID, creation digest)`` of the conversation created for ``key``."""
    digest = hashlib.sha256(json.dumps(
        [scope['user_id'], scope['project_path'], scope['conversation_id'],
         scope['binding_revision'], key],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return 'worker-' + digest[:24], digest


def coordinator_folder(scope: dict):
    """The sidebar folder of the Coordinator conversation, which its Workers join."""
    from lib.conversations.repository import ConversationRepository
    source = ConversationRepository().get(scope['conversation_id'], user_id=scope['user_id'],
                                          include_messages=False)
    source = getattr(source, 'metadata', source) or {}
    return (source.get('settings') or {}).get('folderId')


_READ_FOLDER = object()


def _create(scope: dict, arguments: dict, *, folder=_READ_FOLDER) -> dict:
    """Create the empty conversation of ``arguments``' key, or adopt the one it created.

    ``folder`` is the Coordinator's folder when the caller already knows it.
    The header is created outright: storage refuses an existing ID and replays
    this command's original receipt, so only that refusal reads the existing
    conversation, which only the same creation request may adopt.
    """
    from lib.conversations.repository import ConversationRepository
    from lib.storage import get_storage_client
    from lib.storage.errors import StorageError
    title = arguments.get('title')
    key = arguments.get('idempotency_key')
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError('title must contain 1–160 characters')
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError('idempotency_key must contain 1–200 characters')
    conversation_id, digest = worker_identity(scope, key)
    request_digest = hashlib.sha256(json.dumps(
        [digest, title.strip()], separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    settings = {'projectPath': scope['project_path'],
                '_coordinatorSource': scope['conversation_id'], '_coordinatorCreationKey': digest,
                '_coordinatorCreationRequest': request_digest}
    if folder is _READ_FOLDER:
        folder = coordinator_folder(scope)
    initial_settings = {**settings, **({'folderId': folder} if folder else {})}
    try:
        get_storage_client(write=True).command('conversation.create', {
            'conv_id': conversation_id, 'user_id': scope['user_id'],
            'title': title.strip(), 'settings': initial_settings,
        }, 'coordinator-worker-' + digest)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
        existing = ConversationRepository().get(conversation_id, user_id=scope['user_id'],
                                                include_messages=False)
        existing = getattr(existing, 'metadata', existing)
        actual_settings = (existing or {}).get('settings') or {}
        # Title and ordinary settings remain editable. Only the original request
        # marker and owner/project/binding identity decide idempotent replay; losing
        # a marker or reparenting never silently adopts an unrelated conversation.
        if existing is None or any(actual_settings.get(name) != value for name, value in settings.items()):
            raise ValueError('Conversation creation key conflicts with an existing conversation') from error
    return {'conversation_id': conversation_id, 'created_only': True,
            'execution_started': False, 'project_path': scope['project_path'],
            'next_step': ('For work within the current authorized request, use delegate_project_work; '
                          'it creates and starts its own Worker without a separate per-worker '
                          'authorization step. This empty conversation grants no execution rights. '
                          'Use dispatch_project_assignment only for an already-authorized assignment.')}


def _work_receipt(view: dict, decision: str, next_step: str) -> dict:
    """What a review or cancel changed: identity, decision, revisions, delivery and next step.

    The brief, granted tools and Worker transcript stay with get_project_assignment
    and get_conversation; echoing that view cost thousands of tokens per call.
    """
    execution = view.get('execution') or {}
    # conversation_id names the Worker conversation, whose title is the task's
    # name for every assignment in it, so a reader of the receipt can title it.
    return {'assignment_id': view['assignmentId'], 'conversation_id': view['request']['recipientId'],
            'decision': decision, 'grant_revision': view['grantRevision'], 'revoked': view['revoked'],
            'result_revision': (view.get('review') or {}).get('resultRevision', execution.get('resultRevision')),
            'execution_status': execution.get('status'),
            'checkpoint_sha': (view.get('publication') or {}).get('checkpointSha'),
            'delivery': view.get('delivery'), 'canonical_delivery': view['canonicalDelivery'],
            'next_step': next_step}


def _audited_worker_control(task, scope, name, arguments, act):
    """review/cancel act on any Worker of this binding; each call is one audit record."""
    from lib.project_dispatch.delegation import audited_coordinator_control, coordinator_control_record
    record = coordinator_control_record(task, scope, name, assignment_id=arguments.get('assignment_id'),
                                        decision=arguments.get('decision'),
                                        checkpoint_sha=arguments.get('checkpoint_sha'))
    return audited_coordinator_control(record, act)


def _review_receipt(view: dict) -> dict:
    decision = view['review']['decision']
    if decision != 'accept':
        step = ('Repair with delegate_project_work and rework_of {assignmentId, grantRevision, '
                'resultRevision} copied from this receipt. The review is immutable.')
    elif view.get('publication'):
        step = ('Publication is asynchronous: poll this assignment_id with wait_s, which waits until its '
                'delivery is canonical or held, and confirm canonical_delivery before reporting completion.')
    else:
        step = ('Recorded as Coordinator judgment, not human acceptance. No code is published without '
                'checkpoint_sha (the exact delivery.checkpointSha). To publish this accept, repeat it with the '
                'same note, the current result_revision and checkpoint_sha.')
    receipt = _work_receipt(view, decision, step)
    # Counts only: the checks and per-check verdicts stay with get_project_assignment.
    counts = view['review'].get('acceptanceCounts')
    if counts is not None:
        receipt['acceptance_counts'] = counts
        if decision == 'accept' and counts['unknown']:
            receipt['next_step'] += (f" {counts['unknown']} acceptance check(s) are unknown: report them "
                                     'to the human as unverified, never as met.')
    return receipt


def execute_coordinator_tool(task: dict, name: str, arguments: dict, *, runtime_context=None) -> str:
    """Handle explicit create/dispatch/read; all results preserve owner scope.

    Reads, replay-safe delegation and the write tools re-read through dispatch
    contention (``reread_on_contention``): a lost CAS or a torn snapshot never
    reaches the model. Review/cancel/finish/decision are idempotent commands:
    replaying the same one is a no-op, a different one is still immutable, so
    re-running the whole tool call after contention never double-applies it.
    Each repeat re-validates live authority and takes the publication lock
    fresh, so a contended call never holds the repo lock while it waits.
    """
    from lib.project_dispatch.admission import reread_on_contention
    from lib.storage.errors import StorageError
    from . import coordinator_births
    progress = None
    try:
        # A delegation's birth validates the binding with every other stored
        # fact on its own thread; the call itself reads no storage.
        scope = _bound_scope(task) if name == 'delegate_project_work' else _scope(task)
        if name in {'get_conversation_status', 'control_conversation'}:
            from .session_control import execute_session_tool
            return execute_session_tool(task, scope, name, arguments)
        if name == 'create_conversation':
            result = _create(scope, arguments)
        elif name in {'delegate_project_work', 'poll', 'await_project_workers'}:
            from .coordinator_workers import await_workers, delegate, poll_workers
            if name == 'delegate_project_work':
                result = delegate(task, scope, arguments)
            elif name == 'poll':
                result = poll_workers(task, scope, arguments, runtime_context=runtime_context)
            else:
                result = await_workers(
                    task, scope, arguments, runtime_context=runtime_context)
        elif name == 'review_project_work':
            from lib.project_dispatch.repository import ProjectDispatchRepository
            from lib.project_dispatch.service import ProjectDispatchService
            result = _review_receipt(_audited_worker_control(task, scope, name, arguments, lambda: reread_on_contention(
                task, lambda: ProjectDispatchService(
                    ProjectDispatchRepository(scope['user_id'], scope['project_path'])
                ).review_for_coordinator(task, scope, arguments),
                label='coordinator review')))
        elif name == 'get_project_workers':
            from .coordinator_workers import get_workers
            result = reread_on_contention(task, lambda: get_workers(task, scope, arguments),
                                          label='coordinator worker page', effect_free=True)
        elif name in {'finish_project_request', 'cancel_project_work', 'request_user_decision'}:
            from lib.project_dispatch.repository import ProjectDispatchRepository
            if name != 'request_user_decision':
                # Cancel and finish act on durable Workers: let births in flight land first.
                coordinator_births.wait(
                    task, [arguments.get('assignment_id')] if name == 'cancel_project_work' else None,
                    deadline=None, interrupted=lambda: bool(task.get('aborted')))

            def command():
                repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
                if name == 'finish_project_request':
                    from lib.project_dispatch.continuation import finish_request
                    return finish_request(repository, task, arguments)
                if name == 'request_user_decision':
                    from lib.project_dispatch.continuation import request_decision
                    return request_decision(repository, task, arguments)
                from lib.project_dispatch.service import ProjectDispatchService
                return _work_receipt(
                    ProjectDispatchService(repository).cancel_for_coordinator(task, scope, arguments),
                    'cancelled', 'Grant withdrawn and live Worker attempts stopped; results and receipts '
                    'are preserved. Cancel other unwanted work before finish_project_request.')

            result = reread_on_contention(task, command, label='coordinator ' + name) \
                if name != 'cancel_project_work' else _audited_worker_control(
                    task, scope, name, arguments,
                    lambda: reread_on_contention(task, command, label='coordinator ' + name))
        elif name == 'report_to_user':
            from lib.conversations.coordinator_reports import CoordinatorReports
            result = CoordinatorReports(scope['user_id'], scope['project_path']).publish({
                'owner': scope['user_id'], 'projectPath': scope['project_path'],
                'coordinatorId': scope['conversation_id'], 'bindingRevision': scope['binding_revision'],
            }, arguments)
        elif name == 'update_task_progress':
            from lib.conversations.coordinator_progress import progress_view, publish_task_board
            from lib.tools.todo import render_todo_snapshot
            progress_scope = {
                'owner': scope['user_id'], 'projectPath': scope['project_path'],
                'coordinatorId': scope['conversation_id'], 'bindingRevision': scope['binding_revision'],
            }
            published, plan = publish_task_board(task, progress_scope, arguments)
            # ``plan`` is the board as the checklist this task now holds; it is
            # also the receipt that proves that checklist visible to the model.
            result = {'revision': published['board']['revision'], 'changed': published['changed'],
                      'task_count': len(published['board']['tasks']), 'plan': render_todo_snapshot(plan),
                      'next_step': 'Shown to the human above this chat and kept as your checklist. Send the '
                                   'complete board again whenever a task starts, advances, finishes or is dropped.'}
            progress = progress_view(progress_scope, published['board'], plan)
        elif name in {'dispatch_project_assignment', 'get_project_assignment'}:
            from lib.identity import PrincipalContext
            from lib.project_dispatch.repository import ProjectDispatchRepository
            from lib.project_dispatch.service import ProjectDispatchService
            repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
            service = ProjectDispatchService(repository)
            assignment_id = str(arguments.get('assignment_id') or '')
            principal = PrincipalContext.from_payload(task['_principalContext'])

            def owned_admission():
                assignment = repository.read(assignment_id)
                if (assignment['request']['coordinatorId'] != scope['conversation_id']
                        or assignment['grant']['tenant'] != principal.tenant_id):
                    raise PermissionError('Assignment belongs to another Coordinator or tenant')
                return repository.admission(assignment_id)
            if name == 'dispatch_project_assignment':
                binding = reread_on_contention(task, owned_admission, label='coordinator assignment read',
                                               effect_free=True)
                revision = arguments.get('expected_revision')
                if type(revision) is not int or revision != binding['revision']:
                    raise ValueError('expected_revision must match the current human grant revision')
                result = service.dispatch_authorized(binding)
            else:
                def view():
                    owned_admission()
                    return service.view_for_coordinator(assignment_id, scope=scope)
                result = reread_on_contention(task, view, label='coordinator assignment view', effect_free=True)
        elif name == 'list_project_leads':
            from lib.router.projects import project_leads
            result = project_leads(scope['user_id'], scope['conversation_id'], arguments)
        elif name in _router_only_tools():
            from lib.router.tools import execute_router_tool
            result = execute_router_tool(task, scope, name, arguments)
        elif name in _watchdog_only_tools():
            from lib.router.watchdog.tools import execute_watchdog_tool
            result = execute_watchdog_tool(task, scope, name, arguments)
        else:
            raise ValueError('Unknown Coordinator tool')
        text = json.dumps(result, ensure_ascii=False)
        if progress is None:
            return text
        from lib.conversations.coordinator_progress import ProgressReceipt
        return ProgressReceipt(text, progress)
    except coordinator_births.BirthFailed as failure:
        return failed_result(failure.payload)
    except (PermissionError, ValueError, KeyError, StorageError) as error:
        return failed_result(refusal_payload(name, error))


def start_streamed_call(task: dict, name: str, arguments: dict) -> None:
    """Start a declared stream-start call whose arguments are complete (StreamStartPort.start).

    Only delegate_project_work is declared. A refusal here is left to the
    ordinary dispatch of the same call, which returns it to the model.
    """
    if name != 'delegate_project_work':
        return
    try:
        from .coordinator_workers import delegate
        delegate(task, _bound_scope(task), arguments, early=True)
    except Exception as error:
        logger.info('[Coordinator] %s not started while streaming (task=%s): %s',
                    name, task.get('id'), error)


def refusal_payload(name: str, error: BaseException) -> dict:
    """What a refused call reports: its cause, effect and recovery.

    A typed refusal states its own effect and recovery; generic errors stay uncertain.
    """
    typed = {'code': error.code} if isinstance(getattr(error, 'code', None), str) else {}
    if typed and isinstance(getattr(error, 'details', None), dict):
        typed['details'] = error.details
    return {'error': str(error) or type(error).__name__, **typed,
            'execution_started': getattr(error, 'execution_started',
                None if name in {'dispatch_project_assignment', 'delegate_project_work', 'review_project_work',
                                 'route_to_project'} else False),
            'recovery': getattr(error, 'recovery', None) or (
                'Read get_project_assignment before retrying an uncertain dispatch.'
                if name == 'dispatch_project_assignment' else
                'Retry delegate_project_work with unchanged arguments and the same idempotency_key; it reuses any accepted worker.'
                if name == 'delegate_project_work' else
                'Retry route_to_project with unchanged arguments; it never forwards twice.'
                if name == 'route_to_project' else '')}


def failed_result(value: dict) -> str:
    """A refused or failed call's JSON, carrying the failed execution verdict.

    The tool pipeline never infers failure from payload JSON
    (``tool_result_failure_verdict``): without this verdict the round settles
    ``done``, so the transcript and the tool status show a refusal as a
    completed call.
    """
    from lib.tools.result_value import TrustedResultText, capture_result
    text = json.dumps(value, ensure_ascii=False)
    return TrustedResultText(text, canonical=capture_result(text, execution_status='error'))


def _router_only_tools() -> frozenset:
    from lib.router.policy import ROUTER_ONLY_TOOLS
    return ROUTER_ONLY_TOOLS


def _watchdog_only_tools() -> frozenset:
    from lib.router.watchdog.policy import WATCHDOG_ONLY_TOOLS
    return WATCHDOG_ONLY_TOOLS
