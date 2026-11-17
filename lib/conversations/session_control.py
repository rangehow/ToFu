"""Owner-scoped session observation and control for bound Coordinators.

Entry points: inspect_session, control_session and execute_session_tool. Binding
admission is shared with coordinator_tools; source authority uses project
delegation, or for the Global Router the owner's delegation
(lib.router.owner_delegation, docs/ROUTER.md R9). Queries and commands reuse
conversation_sync/turn_lifecycle, never an executor, route, database or
synthetic human message. A follow_up gives an ended Worker session its next
instruction as a new Turn through the project dispatch service, never a regenerate.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import time


def _text(arguments, name):
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')
    return value


def _target(scope, arguments, *, write=False, reach=None):
    """The owned conversation named by ``arguments`` and its metadata.

    A Project Coordinator's target stays in its project, unless ``reach`` (the
    owner's full delegation, docs/ROUTER.md R9 "Cross-project action") admits
    another project's conversation: it is called with that project and raises
    when the delegation does not cover it.
    """
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.repository import ConversationRepository
    from lib.router.policy import is_router_scope
    from lib.router.watchdog.binding import is_watchdog_scope

    if is_watchdog_scope(scope):
        raise PermissionError('The Watchdog cannot control sessions; notify their Coordinator')
    conversation_id = _text(arguments, 'conversation_id')
    snapshot = ConversationRepository().get(
        conversation_id, user_id=scope['user_id'], include_messages=False)
    if snapshot is None:
        raise PermissionError('Conversation not found in this owner scope')
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') or {}
    project = normalize_project_path(settings.get('projectPath') or '')
    if not is_router_scope(scope) and project != normalize_project_path(scope['project_path']):
        if reach is None:
            raise PermissionError('Conversation belongs to another project; use its Coordinator or the '
                                  'Global Router, or act under the owner\u2019s delegation (owner_delegation)')
        reach(project)
    if write and settings.get('readOnly'):
        raise PermissionError('Read-only conversations cannot be controlled')
    return conversation_id, metadata


def _turn_view(turn):
    return {key: deepcopy(turn[key]) for key in (
        'turnId', 'conversationId', 'laneId', 'actor', 'kind', 'status',
        'currentAttemptId', 'projectionRevision', 'createdAt', 'updatedAt', 'settlement',
    ) if key in turn}


def _attempt_view(attempt):
    return {key: deepcopy(attempt[key]) for key in (
        'attemptId', 'conversationId', 'turnId', 'taskId', 'status', 'operation',
        'createdAt', 'startedAt', 'settledAt', 'error', 'queueBinding',
    ) if key in attempt}


_STATUS_NEXT_STEP = ('Use the selected Turn currentAttemptId and projectionRevision for control_conversation. '
                     'Resume operations come from settlement.resumeOptions. Read content with get_conversation.')


def _window_cursor(arguments):
    lane_id = arguments.get('lane_id', 'main')
    if not isinstance(lane_id, str) or not 1 <= len(lane_id) <= 128:
        raise ValueError('lane_id must be a Turn laneId')
    before = arguments.get('before_ordinal')
    if before is not None and (type(before) is not int or before < 0):
        raise ValueError('before_ordinal must be a window.next_before_ordinal value')
    return lane_id, before


def _status_window(conversation_id, owner, arguments, status, fits):
    """Admit one lane's Turns newest-first while the status still fits the model.

    Pages come from ``turn.sync.page``: each is one bounded read, and a page
    refused at the authority's response bound is read again smaller. The
    newest Turn is always shown; the window stops before the first Turn that
    would make the result spill into an artifact.
    """
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService

    lane_id, before = _window_cursor(arguments)
    service = ConversationSyncService(SidecarConversationSyncRepository())
    shown, head = [], 0  # newest first: (ordinal, turn view, attempt view)

    def render(rows, older):
        status['turns'] = [turn for _, turn, _ in reversed(rows)]
        status['attempts'] = [attempt for _, _, attempt in reversed(rows) if attempt is not None]
        status['window']['next_before_ordinal'] = rows[-1][0] if older and rows else None
        return status

    while True:
        page = service.turn_page(conversation_id, owner, lane_id=lane_id,
                                 expected_sync_sequence=head, before_ordinal=before)
        head = page['syncSeq']
        status['window'] = {'lane_id': lane_id, 'total_turns': page['totalTurns'], 'next_before_ordinal': None}
        attempts = {attempt['attemptId']: attempt for attempt in page['attempts']}
        for turn in reversed(page['turns']):
            attempt = attempts.get(turn.get('currentAttemptId'))
            row = (turn['ordinal'], _turn_view(turn), _attempt_view(attempt) if attempt else None)
            if shown and not fits(json.dumps(render([*shown, row], True), ensure_ascii=False)):
                return render(shown, True)
            shown.append(row)
        if not page['hasMore'] or not page['turns']:
            return render(shown, False)
        before = page['nextBeforeOrdinal']


def _delegated_reach(task, scope):
    """Reading another project's session needs the owner's full delegation (conversation_control)."""
    def reach(_project):
        from lib import lifecycle_approval as gate
        from lib.router.owner_delegation import router_requester
        gate.check_delegation(scope['user_id'], router_requester(task, scope), 'conversation_control')
    return reach


def inspect_session(scope, arguments, *, model='', task=None):
    """Return canonical execution facts; no transcript or private executor config.

    No read spans the whole conversation, whose history can exceed the
    authority's response bound. A named Turn is read by identity with its
    current Attempt. Otherwise a lane (default main) is read newest-first and
    its newest Turns are shown while the result reaches the model inline;
    ``window.next_before_ordinal`` continues with older Turns.
    """
    from lib.message_queue import get_queue
    from lib.tasks_pkg.compaction.api import fits_tool_result_budget
    from lib.turn_lifecycle import get_attempt, get_turn

    conversation_id, metadata = _target(
        scope, arguments, reach=None if task is None else _delegated_reach(task, scope))
    owner = scope['user_id']
    status = {'conversation_id': conversation_id, 'title': metadata.get('title', ''),
              'read_only': bool((metadata.get('settings') or {}).get('readOnly')),
              'turns': [], 'attempts': [], 'queue_items': get_queue(conversation_id, user_id=owner)}
    if arguments.get('turn_id'):
        turn = get_turn(conversation_id, _text(arguments, 'turn_id'), user_id=owner, include_projection=False)
        status['turns'] = [_turn_view(turn)]
        if turn.get('currentAttemptId'):
            status['attempts'] = [_attempt_view(get_attempt(turn['currentAttemptId'], user_id=owner))]
        return {**status, 'next_step': _STATUS_NEXT_STEP}
    status['next_step'] = (_STATUS_NEXT_STEP + ' For older Turns of this lane, call again with '
                           'before_ordinal=window.next_before_ordinal (and its lane_id).')
    return _status_window(conversation_id, owner, arguments, status,
                          lambda text: fits_tool_result_budget(text, model=model))


def control_session(task, scope, arguments, *, acting_principal=None):
    """Resume the exact accepted execution, or stop its exact current Attempt.

    Every action, accepted or refused, is recorded in the audit log
    (``coordinator_session_control``).
    """
    from lib.project_dispatch.delegation import audited_coordinator_control, coordinator_control_record
    record = coordinator_control_record(
        task, scope, arguments.get('action'), delegated=acting_principal is not None,
        conversation_id=arguments.get('conversation_id'), turn_id=arguments.get('turn_id'),
        attempt_id=arguments.get('attempt_id'))
    return audited_coordinator_control(
        record, lambda: _control_session(task, scope, arguments, acting_principal=acting_principal),
        lambda result: {'command_id': result.get('command_id') if isinstance(result, Mapping) else None})


def _control_session(task, scope, arguments, *, acting_principal=None):
    """``control_session`` without its audit record.

    ``acting_principal`` replaces the current request's source principal only
    for the owner's delegation (``lib.router.owner_delegation``): it stands in
    for the owner's message and never becomes the target's execution identity.
    """
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.project_dispatch.admission import repository_for, turn_dispatch
    from lib.project_dispatch.delegation import source_for_task, worker_control
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.turn_lifecycle import get_attempt, get_turn

    action = _text(arguments, 'action')
    if action == 'set_model':
        from .session_model import set_session_model
        if acting_principal is not None:
            return set_session_model(task, scope, acting_principal, arguments, delegated=True)
        return set_session_model(task, scope, source_for_task(ProjectDispatchRepository(
            scope['user_id'], scope['project_path']), task, delegated=True)[0], arguments)
    if action not in {'continue', 'checkpoint_resume', 'regenerate', 'stop', 'follow_up'}:
        raise ValueError('action must be continue, checkpoint_resume, regenerate, stop, follow_up '
                         'or set_model')
    if arguments.get('attachments') and action != 'follow_up':
        raise ValueError('attachments go with a follow_up instruction only')
    # Under the owner's delegation the grant was checked and recorded before this call.
    conversation_id, _ = _target(scope, arguments, write=True,
                                 reach=None if acting_principal is None else lambda _project: None)
    turn_id, attempt_id = _text(arguments, 'turn_id'), _text(arguments, 'attempt_id')
    key = _text(arguments, 'idempotency_key')
    expected = arguments.get('expected_revision')
    if action != 'stop' and (type(expected) is not int or expected < 1):
        raise ValueError('expected_revision must be the observed Turn projectionRevision')
    principal = acting_principal or source_for_task(ProjectDispatchRepository(
        scope['user_id'], scope['project_path']), task, delegated=True)[0]
    owner = scope['user_id']
    turn = get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
    attempt = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
    if attempt['conversationId'] != conversation_id or attempt['turnId'] != turn_id:
        raise PermissionError('Attempt does not belong to the selected conversation and Turn')
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        raise PermissionError('The accepted execution config is unavailable')
    if (config.get('_turnPrincipalTenantId') or None) != principal.tenant_id:
        raise PermissionError('Conversation execution belongs to another tenant')

    # Pin the action and observed execution, not a mutable "latest" pointer.
    # Re-reading config from this original Attempt also makes lost-ACK replay
    # byte-stable after the successor has become the current Attempt.
    digest = hashlib.sha256(json.dumps([
        owner, scope['conversation_id'], scope['binding_revision'], key,
        conversation_id, turn_id, attempt_id, action, expected, arguments.get('resume_anchor'),
    ] + ([_instruction(arguments), arguments.get('attachments') or []] if action == 'follow_up' else []),
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    command_id = 'session-control:' + digest
    if action == 'follow_up':
        return _follow_up(task, scope, arguments, turn=turn, config=config, command_id=command_id)
    if turn.get('currentAttemptId') != attempt_id:
        if action == 'stop':
            raise ValueError('The current Attempt changed; read get_conversation_status before acting')
        current = get_attempt(turn['currentAttemptId'], user_id=owner)
        if current.get('commandId') != command_id:
            raise ValueError('The current Attempt changed; read get_conversation_status before acting')

    trusted = turn_dispatch(conversation_id, turn, config, user_id=owner)
    if trusted is not None:
        if acting_principal is None:
            # Any Turn of this Coordinator, human-steered or an automatic
            # continuation, controls the sessions of the Workers its binding
            # dispatched in any request; another Coordinator's stay refused.
            worker_control(repository_for(trusted.binding), task, trusted.checked(), session=True)
        elif principal.kind != 'user':
            worker_control(repository_for(trusted.binding), task, trusted.checked())

    if action == 'stop':
        # A repeated Stop of a settled Attempt must not plant a fresh
        # conversation-wide withdrawal against work started after that Stop.
        result = (conversation_turn_commands.abort_attempt(attempt_id, owner).value
                  if attempt['status'] in {'pending', 'running'} else
                  {'attemptId': attempt_id, 'status': attempt['status'], 'alreadyTerminal': True})
    else:
        if trusted is not None:
            target_principal = trusted.principal
            public_config = {}
        else:
            target_principal, _ = restore_execution_identity({**config, '_turnOwnerUserId': owner})
            public_config = {name: deepcopy(value) for name, value in config.items() if not name.startswith('_')}
        body = {'commandId': command_id, 'operation': action,
                'expectedProjectionRevision': expected, 'config': public_config}
        if arguments.get('resume_anchor') is not None:
            # Anchors are exact values returned by canonical resumeOptions;
            # the command owner validates them, just as for a browser click.
            body['resumeAnchor'] = arguments['resume_anchor']
        result = conversation_turn_commands.create_attempt(
            conversation_id, turn_id, owner, body, request_started_at=time.time(),
            principal=target_principal, trusted_dispatch=trusted).value
    return {'conversation_id': conversation_id, 'action': action, 'command_id': command_id,
            'turn': _turn_view(result['turn']) if result.get('turn') else None,
            'attempt': _attempt_view(result['attempt']) if result.get('attempt') else None,
            'idempotent_replay': bool(result.get('idempotentReplay')),
            'receipt': {key: value for key, value in result.items()
                        if key in {'aborted', 'attemptId', 'status', 'alreadyTerminal', 'cancelled',
                                   'dispatchWithdrawal', 'queueId', 'deletedTurnIds', 'conversationRevision'}},
            'next_step': 'Read get_conversation_status to observe execution; acceptance is not completion.'}


def _instruction(arguments):
    from lib.project_dispatch.repository import CONTRACT
    instruction = _text(arguments, 'instruction')
    if len(instruction) > CONTRACT['$defs']['request']['properties']['brief']['maxLength']:
        raise ValueError('instruction exceeds the project dispatch brief limit')
    return instruction


def _follow_up(task, scope, arguments, *, turn, config, command_id):
    """Give an ended Worker session its Coordinator's next instruction as a new Turn.

    The ended Turn is never regenerated. An unreviewed result whose delivery can
    still reopen continues its own assignment, grant, account and checkout
    (``ProjectDispatchService.follow_up``). A review or delivery is final for its
    assignment, so such a result continues as a new assignment of the current
    request in the same conversation.
    """
    from lib.project_dispatch.admission import repository_for, turn_dispatch
    from lib.project_dispatch.delegation import worker_control
    from lib.project_dispatch.service import FollowUpRefused, ProjectDispatchService
    if arguments.get('resume_anchor') is not None:
        raise ValueError('follow_up takes an instruction, not a resume_anchor')
    trusted = turn_dispatch(turn['conversationId'], turn, config, user_id=scope['user_id'])
    if trusted is None:
        raise FollowUpRefused('follow_up continues a Worker session a Coordinator dispatched; '
                              'any other session takes its owner\u2019s own messages')
    repository = repository_for(trusted.binding)
    row = repository.read(trusted.binding['assignmentId'])
    # Coordinator context for its own Worker only, never another binding's or the Router's.
    worker_control(repository, task, row, session=True)
    if row['grant']['revoked']:
        raise FollowUpRefused('This Worker was cancelled; delegate new work instead of continuing it')
    service = ProjectDispatchService(repository)
    receipt = {'conversation_id': turn['conversationId'], 'action': 'follow_up', 'command_id': command_id,
               'next_step': 'Poll this assignment_id with wait_s, then review its new result. '
                            'Acceptance is not completion.'}
    if not service.continues_in_place(row):
        return {**receipt, **_successor(task, scope, arguments, row, turn, service)}
    from .lead_attachments import admit
    result = service.follow_up(row['id'], turn_id=turn['turnId'], attempt_id=arguments['attempt_id'],
                               expected_revision=arguments['expected_revision'],
                               instruction=_instruction(arguments), command_id=command_id,
                               attachments=admit(task, scope['user_id'], arguments.get('attachments')))
    if result.get('aborted'):
        raise FollowUpRefused('The owner stopped this conversation just now; the follow-up did not start')
    return {**receipt, 'assignment_id': row['id'],
            'turn': _turn_view(result['turn']) if result.get('turn') else None,
            'attempt': _attempt_view(result['attempt']) if result.get('attempt') else None,
            'idempotent_replay': bool(result.get('idempotentReplay'))}


def _successor(task, scope, arguments, row, turn, service):
    """A final result continues as a new assignment dispatched into its own conversation.

    One delegation per observed result and request: its identity is the pinned
    association, and its request derives only from that and the instruction, so a
    retry replays it and a different instruction for the same result is refused.
    """
    from lib.project_dispatch.delegation import successor_brief
    from lib.project_dispatch.publication import delivery_final, delivery_view
    from lib.project_dispatch.repository import CommandReused
    from lib.project_dispatch.service import FollowUpRefused
    from .coordinator_workers import delegate
    execution = row['execution'] or {}
    if execution.get('turnId') != turn['turnId'] or execution.get('attemptId') != arguments['attempt_id']:
        raise FollowUpRefused('This Worker session moved on since you read it: its assignment continues '
                              f"from Turn {execution.get('turnId')}. Pin its latest settled Turn.")
    if turn.get('projectionRevision') != arguments['expected_revision']:
        raise FollowUpRefused('The Worker result changed since you read it; read get_conversation_status '
                              'and pin its current projectionRevision.')
    attempt = service.attempt_get(execution['attemptId'], user_id=scope['user_id'])
    workspace_get = service.workspace_get or service.repository.get_workspace
    held, unlanded = None, None
    rework = (row.get('review') or {}).get('decision') == 'rework'
    if row.get('publication') and not delivery_final(row, attempt, workspace_get):
        # A conflict hold is final for this checkout too: only a merge on the current
        # branch resolves it, which is exactly what the successor's checkout is. A
        # rework that superseded the accept stopped its publication the same way.
        held = (delivery_view(row, attempt, workspace_get) or {}).get('conflict')
        unlanded = row['publication']['checkpointSha'] if held or rework else None
        if not unlanded:
            raise FollowUpRefused('Its accepted result has not been delivered yet, so a new checkout would '
                                  'miss it. Poll its assignment_id until the delivery is canonical (or held '
                                  'by a conflict, which a follow-up resolves), then follow up.')
    result_key = hashlib.sha256(json.dumps([row['id'], execution['attemptId']]).encode()).hexdigest()
    try:
        worker = delegate(task, scope, {
            'title': 'Follow-up of assignment ' + row['id'],
            'brief': successor_brief(row, _instruction(arguments), held_checkpoint=unlanded),
            # A rework's repair links to the result it reworks (the dependency graph's reworkOf).
            **({'rework_of': {'assignmentId': row['id'], 'grantRevision': row['grant']['revision'],
                              'resultRevision': row['review']['resultRevision']}} if rework else {}),
            'idempotency_key': 'follow-up:' + result_key,
            **({'attachments': arguments['attachments']} if arguments.get('attachments') else {}),
        }, create=lambda scope, arguments: {'conversation_id': turn['conversationId']})
    except CommandReused as reused:
        raise FollowUpRefused(f'This result already continues in assignment {reused.assignment_id} with '
                              'another instruction; follow up on that session\u2019s latest Turn.') from reused
    return {'assignment_id': worker['assignment_id'], 'continues_assignment': row['id'],
            'dispatch': worker['dispatch'], 'status': worker['status'], 'task_id': worker['task_id'],
            **({'resolves_held_conflict': {'checkpoint_sha': row['publication']['checkpointSha'],
                                           'paths': held['paths']}} if held else {})}


def execute_session_tool(task, scope, name, arguments):
    """Keep typed conflicts and uncertain mutation outcomes visible to the model."""
    from lib.conversation_sync.service import ConversationSyncNotFound, ConversationTurnPageStale
    from lib.error_envelope import BadRequest
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import LaterTurnsRefused, LifecycleConflict, LifecycleNotFound

    try:
        if name == 'get_conversation_status':
            result = inspect_session(scope, arguments, model=task.get('model', ''), task=task)
        elif arguments.get('owner_delegation') is not None:
            from lib.router.owner_delegation import delegated_conversation_control
            result = delegated_conversation_control(task, scope, arguments, control=control_session)
        else:
            result = control_session(task, scope, arguments)
    except (PermissionError, ValueError, KeyError, LifecycleNotFound, ConversationSyncNotFound,
            ConversationTurnPageStale, LifecycleConflict, BadRequest, StorageError) as error:
        from .coordinator_tools import failed_result
        result = {'error': str(error), 'code': getattr(error, 'code', 'session_control_refused'),
                  'execution_started': getattr(error, 'execution_started',
                                               None if name == 'control_conversation' else False),
                  'recovery': getattr(error, 'recovery', None) or (
                      'Read get_conversation_status. After an uncertain command ACK, retry unchanged '
                      'arguments with the same idempotency_key; never select a new Attempt silently.')}
        if isinstance(error, LifecycleConflict) and error.turn:
            result['turn'] = _turn_view(error.turn)
        if isinstance(error, LaterTurnsRefused):
            result['later_turns'] = error.later_turns
            result['recovery'] = (
                'This Turn is not the latest of its lane: regenerating it would discard the later '
                'Turns listed here, and only the owner can approve that in their own UI. A resume '
                'operation the Turn offers (continue, checkpoint_resume) completes it in place and '
                'keeps every later Turn; otherwise act on the latest Turn (follow_up for a Worker '
                'you dispatched), or tell the owner which Turn needs their decision.')
        return failed_result(result)
    return json.dumps(result, ensure_ascii=False)
