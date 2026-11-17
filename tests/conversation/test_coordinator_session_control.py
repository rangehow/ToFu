"""Session handoff uses real Turn transactions on SQL and native authorities."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.conversation_sync.execution_identity import capture_execution_identity
from lib.conversations.coordinator_tools import execute_coordinator_tool
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit


@pytest.fixture(params=['continue', 'checkpoint_resume'])
def session_runtime(storage, monkeypatch, tmp_path, request):
    from lib import turn_lifecycle as turns
    from lib.conversations import project_coordination
    from lib.conversation_sync import runtime

    project = str(tmp_path)
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'target-model', 'projectPath': project,
                                          'thinkingEnabled': True}, 1, principal)

    def running(conversation, task_id):
        pair = turns.create_turn_pair(conversation, command_id=task_id + ':input',
            input_projection={'content': 'Continue managing the selected Session'},
            config=deepcopy(config), user_id=1, conversation_defaults={
                'allowCreate': True, 'title': conversation, 'settings': {'projectPath': project}})
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
        task = {'id': task_id, 'convId': conversation, '_turnId': pair['turn']['turnId'],
                '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
                '_principalContext': principal.to_payload(), 'config': deepcopy(config),
                'status': 'running', 'model': 'target-model', 'content': 'Preserved partial answer',
                'thinking': '', 'toolRounds': []}
        turns.record_task_event(task, {'type': 'delta', 'content': task['content']})
        return pair, task

    source, task = running('coordinator', 'coordinator-task')
    target, target_task = running('session', 'target-task')
    if request.param == 'continue':
        target_task.update(content='', thinking='Preserved reasoning')
    target_task.update(status='done', finishReason='length')
    turns.record_task_event(target_task, {'type': 'done', 'finishReason': 'length'})
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
        lambda owner, **kwargs: SimpleNamespace(read=lambda path: {
            'availability': 'available', 'conversationId': 'coordinator', 'revision': 1,
            'projectPath': project}))
    started = []

    def start(conv, cfg, request, abort_after, registered, **kwargs):
        task_id = f'resumed-{len(started)}'
        registered(task_id)
        started.append((conv, deepcopy(cfg)))
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False, start_task=start)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    target_turn = turns.get_turn('session', target['turn']['turnId'], user_id=1)
    args = {'conversation_id': 'session', 'turn_id': target_turn['turnId'],
            'attempt_id': target_turn['currentAttemptId'], 'action': request.param,
            'expected_revision': target_turn['projectionRevision'], 'idempotency_key': 'resume'}
    args['resume_anchor'] = next(option['anchor'] for option in target_turn['settlement']['resumeOptions']
                                 if option['operation'] == request.param)
    return SimpleNamespace(turns=turns, task=task, target=target, source=source,
                           args=args, started=started, commands=commands,
                           call=lambda name, args: json.loads(execute_coordinator_tool(task, name, args)))


def test_real_continue_replay_and_stale_stop_preserve_conversation(session_runtime, monkeypatch):
    s = session_runtime
    status = s.call('get_conversation_status', {'conversation_id': 'session'})
    assert any(t['turnId'] == s.args['turn_id'] for t in status['turns']), status
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert s.args['action'] in {o['operation'] for o in before['settlement']['resumeOptions']}
    from lib.storage.errors import StorageError
    create = s.commands.create_attempt
    lost = False

    def lose_first_ack(*args, **kwargs):
        nonlocal lost
        result = create(*args, **kwargs)
        if not lost:
            lost = True
            raise StorageError('database_unavailable', 'Accepted command ACK was lost')
        return result

    monkeypatch.setattr(s.commands, 'create_attempt', lose_first_ack)
    uncertain = s.call('control_conversation', s.args)
    assert uncertain['execution_started'] is None
    assert len(s.started) == 1
    first = s.call('control_conversation', s.args)
    assert 'error' not in first, first
    second = s.call('control_conversation', s.args)
    assert 'error' not in second, second
    assert first['attempt']['attemptId'] == second['attempt']['attemptId']
    assert len(s.started) == 1
    assert s.started[0][0] == 'session'
    assert s.started[0][1]['model'] == 'target-model'
    assert s.started[0][1]['thinkingEnabled'] is True
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert after['parentTurnId'] == before['parentTurnId']
    if s.args['action'] == 'continue':
        assert after['projection'].get('content', '') == before['projection']['content']
    else:
        assert after['projection'].get('content', '') == ''
        assert before['projection']['content'] in json.dumps(after['projection']['rolledBack'])
    stale = s.call('control_conversation', {**s.args, 'action': 'stop'})
    assert 'error' in stale
    assert s.turns.get_attempt(first['attempt']['attemptId'], user_id=1)['status'] in {'pending', 'running'}
    stopped = s.call('control_conversation', {**s.args, 'action': 'stop',
        'attempt_id': first['attempt']['attemptId'], 'idempotency_key': 'stop'})
    assert 'error' not in stopped, stopped
    assert s.turns.get_attempt(first['attempt']['attemptId'], user_id=1)['status'] == 'interrupted'


def test_synthetic_source_and_stale_revision_cannot_launch(session_runtime):
    s = session_runtime
    stale = s.call('control_conversation', {**s.args, 'expected_revision': 1})
    assert 'error' in stale, stale
    assert not s.started
    s.task['_principalContext'] = PrincipalContext.system(subject_id='notice', owner_user_id=1,
        scopes={'chat'}).to_payload()
    refused = s.call('control_conversation', s.args)
    assert 'error' in refused, refused
    assert not s.started


HUMAN_UI = {'kind': 'human-ui', 'ownerUserId': 1, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    return gate


def test_global_router_continues_and_stops_an_exact_attempt_in_another_project_after_a_restart(
        session_runtime, approvals, monkeypatch, tmp_path):
    """docs/ROUTER.md R9: under the owner's delegation, a Global Router request without
    an owner message of its own (a standing instruction) presses the owner's Continue
    and Stop on the exact Attempt a restart interrupted, in any project: the same Turn
    resumes with its accepted config; nothing regenerates and no assignment is created."""
    s, gate = session_runtime, approvals
    other = tmp_path / 'another-project'
    other.mkdir()
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'other-model', 'projectPath': str(other)}, 1, principal)
    pair = s.turns.create_turn_pair('elsewhere', command_id='elsewhere:input',
        input_projection={'content': 'Run the long migration'}, config=deepcopy(config), user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'elsewhere',
                               'settings': {'projectPath': str(other)}})
    s.turns.bind_task(pair['attempt']['attemptId'], 'elsewhere-task', user_id=1)
    s.turns.record_task_event({
        'id': 'elsewhere-task', 'convId': 'elsewhere', '_turnId': pair['turn']['turnId'],
        '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
        '_principalContext': principal.to_payload(), 'config': deepcopy(config), 'status': 'running',
        'model': 'other-model', 'content': '', 'thinking': 'Half way through', 'toolRounds': []},
        {'type': 'thinking', 'content': 'Half way through'})
    # The restart: boot recovery settles every pre-boot attempt but the live router's own.
    s.turns.recover_running_attempts(exclude_task_ids=['coordinator-task'])
    interrupted = s.turns.get_turn('elsewhere', pair['turn']['turnId'], user_id=1)
    assert interrupted['status'] == 'interrupted', interrupted
    option = interrupted['settlement']['resumeOptions'][0]
    assert option['operation'] in {'continue', 'checkpoint_resume'}
    # The Global Router (any project) handling a nonhuman notice: no owner message of its own.
    monkeypatch.setattr('lib.router.policy.is_router_scope', lambda scope: True)
    s.task['_principalContext'] = PrincipalContext.system(
        subject_id='notice', owner_user_id=1, scopes={'chat'}).to_payload()
    trigger = {'kind': 'standing_instruction', 'turnId': 'owner-turn', 'instructionId': 'ri-1',
               'eventId': 'restart:1', 'text': 'After a restart, continue my interrupted work.'}
    monkeypatch.setattr('lib.router.owner_delegation.resolve_trigger', lambda task, scope, args: dict(trigger))
    authority = {'reason': 'The restart interrupted this migration', 'instruction_id': 'ri-1',
                 'event_id': 'restart:1'}
    args = {'conversation_id': 'elsewhere', 'turn_id': interrupted['turnId'],
            'attempt_id': interrupted['currentAttemptId'], 'action': option['operation'],
            'expected_revision': interrupted['projectionRevision'], 'idempotency_key': 'after-restart',
            'owner_delegation': authority, **({'resume_anchor': option['anchor']} if 'anchor' in option else {})}
    without_authority = s.call('control_conversation', {k: v for k, v in args.items() if k != 'owner_delegation'})
    assert 'error' in without_authority and not s.started
    refused = s.call('control_conversation', args)
    assert refused['code'] == 'owner_delegation_not_granted' and refused['execution_started'] is False
    assert not s.started and gate.delegated_actions(1) == []
    gate.set_delegation(1, True, provenance=HUMAN_UI, grantee={
        'kind': 'global_router', 'conversationId': 'coordinator', 'bindingRevision': 1, 'tenant': None})
    regenerate = s.call('control_conversation', {**{k: v for k, v in args.items() if k != 'resume_anchor'},
                                                 'action': 'regenerate', 'idempotency_key': 'regenerate'})
    assert 'error' in regenerate and not s.started
    resumed = s.call('control_conversation', args)
    assert 'error' not in resumed, resumed
    assert resumed['turn']['turnId'] == interrupted['turnId']
    assert resumed['attempt']['attemptId'] != interrupted['currentAttemptId']
    assert [(conversation, cfg['model']) for conversation, cfg in s.started] == [('elsewhere', 'other-model')]
    # The resumed Attempt runs as the interrupted execution's own accepted identity, never as the delegation.
    from lib.conversation_sync.execution_identity import EXECUTION_IDENTITY_CONFIG_KEY
    accepted = s.turns.get_attempt(resumed['attempt']['attemptId'], user_id=1, include_execution_config=True)
    assert accepted['config'][EXECUTION_IDENTITY_CONFIG_KEY]['principal']['subject_id'] == 'owner'
    replay = s.call('control_conversation', args)
    assert replay['attempt']['attemptId'] == resumed['attempt']['attemptId'] and len(s.started) == 1
    [action] = gate.delegated_actions(1)
    assert action['id'] == resumed['owner_delegation_action_id'] and action['status'] == 'completed'
    assert action['scope'] == 'conversation_control' and action['trigger'] == trigger
    assert action['target'] == {'kind': 'conversation', 'conversationId': 'elsewhere',
                                'turnId': interrupted['turnId'],
                                'attemptId': interrupted['currentAttemptId'], 'action': option['operation']}
    stop = {'conversation_id': 'elsewhere', 'turn_id': interrupted['turnId'],
            'attempt_id': resumed['attempt']['attemptId'], 'action': 'stop', 'idempotency_key': 'stop',
            'owner_delegation': authority}
    stopped = s.call('control_conversation', stop)
    assert 'error' not in stopped, stopped
    assert s.turns.get_attempt(resumed['attempt']['attemptId'], user_id=1)['status'] == 'interrupted'
    assert sorted(item['target']['action'] for item in gate.delegated_actions(1)) == sorted(
        ['stop', option['operation']])


def _interrupted_in_another_project(s, tmp_path):
    other = tmp_path / 'helped-project'
    other.mkdir()
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'other-model', 'projectPath': str(other)}, 1, principal)
    pair = s.turns.create_turn_pair('helped', command_id='helped:input',
        input_projection={'content': 'Land the migration'}, config=deepcopy(config), user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'helped', 'settings': {'projectPath': str(other)}})
    s.turns.bind_task(pair['attempt']['attemptId'], 'helped-task', user_id=1)
    s.turns.record_task_event({
        'id': 'helped-task', 'convId': 'helped', '_turnId': pair['turn']['turnId'],
        '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
        '_principalContext': principal.to_payload(), 'config': deepcopy(config), 'status': 'running',
        'model': 'other-model', 'content': '', 'thinking': 'Half way through', 'toolRounds': []},
        {'type': 'thinking', 'content': 'Half way through'})
    s.turns.recover_running_attempts(exclude_task_ids=['coordinator-task'])
    return str(other.resolve()), s.turns.get_turn('helped', pair['turn']['turnId'], user_id=1)


def test_a_project_lead_acts_on_another_projects_session_under_the_full_delegation(
        session_runtime, approvals, monkeypatch, tmp_path):
    """docs/ROUTER.md R9 "Cross-project action": a Project Router asked to help another project
    reads and continues/stops that project's session through control_conversation(owner_delegation)
    only under the owner's full grant; the audit record names the target project and that
    project's lead receives one cross-lead report. Without the grant both are refused."""
    from lib.conversations import project_coordination
    from lib.conversations.project_messages import ProjectMailbox
    s, gate = session_runtime, approvals
    project, interrupted = _interrupted_in_another_project(s, tmp_path)
    own = s.task['config']['projectPath']
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
        lambda owner, **kwargs: SimpleNamespace(read=lambda path: {
            'availability': 'available', 'revision': 1, 'projectPath': path,
            'conversationId': 'helped-lead' if path == project else 'coordinator'}))
    s.task['_principalContext'] = PrincipalContext.system(
        subject_id='wake', owner_user_id=1, scopes={'chat'}).to_payload()
    trigger = {'kind': 'owner_delegation', 'turnId': s.task['_turnId'], 'text': 'delegated to every lead'}
    monkeypatch.setattr('lib.router.owner_delegation.resolve_trigger', lambda task, scope, args: dict(trigger))
    option = interrupted['settlement']['resumeOptions'][0]
    authority = {'reason': 'The helped project asked us to finish its migration', 'under_delegation': True}
    args = {'conversation_id': 'helped', 'turn_id': interrupted['turnId'],
            'attempt_id': interrupted['currentAttemptId'], 'action': option['operation'],
            'expected_revision': interrupted['projectionRevision'], 'idempotency_key': 'help',
            'owner_delegation': authority, **({'resume_anchor': option['anchor']} if 'anchor' in option else {})}

    status = s.call('get_conversation_status', {'conversation_id': 'helped'})
    assert status['code'] == 'owner_delegation_not_granted', status
    plain = s.call('control_conversation', {k: v for k, v in args.items() if k != 'owner_delegation'})
    assert 'another project' in plain['error'] and not s.started
    refused = s.call('control_conversation', args)
    assert refused['code'] == 'owner_delegation_not_granted' and refused['execution_started'] is False
    assert not s.started and gate.delegated_action_history(1) == []

    gate.set_delegation(1, True, provenance=HUMAN_UI, grantee={
        'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None})
    status = s.call('get_conversation_status', {'conversation_id': 'helped'})
    assert interrupted['turnId'] in {turn['turnId'] for turn in status['turns']}, status
    resumed = s.call('control_conversation', args)
    assert 'error' not in resumed, resumed
    assert [conversation for conversation, _ in s.started] == ['helped']
    assert resumed['target_lead_notified'] == 'helped-lead'
    [action] = gate.delegated_actions(1)
    assert action['requester']['kind'] == 'project_lead' and action['requester']['conversationId'] == 'coordinator'
    assert (action['scope'], action['status'], action['target']['path']) == (
        'conversation_control', 'completed', project)
    from lib.router.contract import validate
    validate(action, 'ownerDelegatedAction')
    [report] = ProjectMailbox(1, project, 'helped-lead').page('')['messages']
    assert report['sender'] == 'coordinator' and report['kind'] == 'report'
    assert report['senderProject'] == own and action['id'] in report['content']
    # A replay acts and reports once.
    again = s.call('control_conversation', args)
    assert again['attempt']['attemptId'] == resumed['attempt']['attemptId'] and len(s.started) == 1
    assert len(ProjectMailbox(1, project, 'helped-lead').page('')['messages']) == 1

    # An enumerated grant names only the Global Router: the Project Router is refused again.
    import json as _json
    document = _json.loads(open(gate._APPROVALS_FILE).read())
    for record in document['delegations'].values():
        record.update(coverage='enumerated', scopes=['conversation_control'])
    open(gate._APPROVALS_FILE, 'w').write(_json.dumps(document))
    stop = {'conversation_id': 'helped', 'turn_id': interrupted['turnId'],
            'attempt_id': resumed['attempt']['attemptId'], 'action': 'stop', 'idempotency_key': 'stop',
            'owner_delegation': authority}
    narrowed = s.call('control_conversation', stop)
    assert narrowed['code'] == 'owner_delegation_scope', narrowed
    assert s.turns.get_attempt(resumed['attempt']['attemptId'], user_id=1)['status'] in {'pending', 'running'}


@pytest.fixture
def tool_checkpoint_session(storage, monkeypatch, tmp_path):
    """A long turn that failed on a provider 402 after real tool rounds.

    Mirrors production worker-12d175fc72d5ca8423588a9a turn 411d2578 (2026-11-09
    21:37): hundreds of retained tool rounds, a content-free tail, and a
    Coordinator resuming it after the provider credit error. The Coordinator
    task is created by the same ``running`` helper the pre-existing session
    fixture uses, so the binding, principal and dispatch scope are identical.
    """
    from lib import turn_lifecycle as turns
    from lib.conversations import project_coordination
    from lib.conversation_sync import runtime
    from lib.tool_results.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD

    project = str(tmp_path)
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'target-model', 'projectPath': project,
                                          'thinkingEnabled': True}, 1, principal)

    def running(conversation, task_id):
        pair = turns.create_turn_pair(conversation, command_id=task_id + ':input',
            input_projection={'content': 'Continue managing the selected Session'},
            config=deepcopy(config), user_id=1, conversation_defaults={
                'allowCreate': True, 'title': conversation, 'settings': {'projectPath': project}})
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
        task = {'id': task_id, 'convId': conversation, '_turnId': pair['turn']['turnId'],
                '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
                '_principalContext': principal.to_payload(), 'config': deepcopy(config),
                'status': 'running', 'model': 'target-model', 'content': '',
                'thinking': '', 'toolRounds': []}
        return pair, task

    source, task = running('coordinator', 'coordinator-task')
    target, target_task = running('session', 'target-task')
    # One superseded provider-attempt artifact (transport noise, never
    # executed) precedes the durable tool receipts, exactly as the checkpoint
    # retention contract describes.
    rounds = [{
        'roundNum': 1, 'llmRound': 0, 'toolCallId': 'superseded-transport-attempt',
        'toolName': 'search_tools', 'toolArgs': {'query': 'never executed'},
        'toolContent': None, 'status': 'aborted',
        SUPERSEDED_PROVIDER_ATTEMPT_FIELD: True,
    }]
    rounds += [{
        'roundNum': position + 2, 'llmRound': position + 1,
        'toolCallId': f'durable-call-{position}', 'toolName': 'run_command',
        'toolArgs': {'command': f'pwd #{position}'},
        'toolContent': f'/workspace #{position}', 'status': 'done',
        'results': [{'title': 'run_command', 'snippet': f'/workspace #{position}'}],
    } for position in range(3)]
    target_task.update(toolRounds=rounds)
    turns.record_task_event(target_task, {'type': 'tool_result'})
    error = {'kind': 'quota', 'statusCode': 402, 'source': 'llm-stream',
             'detail': 'API HTTP 402: credit exhausted on the fallback model',
             'model': 'kimi-k3.1-pro', 'retryable': False,
             'retryPolicy': {'automatic': False, 'mode': 'manual'}}
    turns.record_task_event(target_task, {'type': 'done', 'finishReason': 'error', 'error': error})
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
        lambda owner, **kwargs: SimpleNamespace(read=lambda path: {
            'availability': 'available', 'conversationId': 'coordinator', 'revision': 1,
            'projectPath': project}))
    started = []

    def start(conv, cfg, request, abort_after, registered, **kwargs):
        task_id = f'resumed-{len(started)}'
        registered(task_id)
        started.append((conv, deepcopy(cfg)))
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False, start_task=start)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    turn = turns.get_turn('session', target['turn']['turnId'], user_id=1)
    assert turn['status'] == 'failed', turn['status']
    option = next(option for option in turn['settlement']['resumeOptions']
                  if option['operation'] == 'checkpoint_resume')
    args = {'conversation_id': 'session', 'turn_id': turn['turnId'],
            'attempt_id': turn['currentAttemptId'], 'action': 'checkpoint_resume',
            'expected_revision': turn['projectionRevision'],
            'idempotency_key': 'resume-after-402', 'resume_anchor': option['anchor']}
    return SimpleNamespace(turns=turns, task=task, args=args, option=option,
                           started=started, storage=storage, commands=commands,
                           call=lambda name, args: json.loads(execute_coordinator_tool(task, name, args)))


def test_checkpoint_resume_after_a_provider_error_keeps_the_saved_checkpoint(
        tool_checkpoint_session, monkeypatch):
    """A dropped optional anchor field must not strand the saved checkpoint.

    Production conflict (2026-11-09): the settlement's canonical checkpoint
    anchor carries ``retainedToolRoundPositions``; a caller copying the other
    fields without it selected exactly the same durable rounds semantically,
    yet both authorities refused it with a bare ``database_conflict``. The
    Coordinator then had to append a new instruction Turn instead of resuming
    the saved long turn. A resume that changes which rounds survive is still a
    typed, recoverable conflict with nothing written.
    """
    s = tool_checkpoint_session
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    anchor = s.option['anchor']
    assert anchor['retainedToolRoundPositions']
    status = s.call('get_conversation_status', {'conversation_id': 'session'})
    assert any(turn['turnId'] == s.args['turn_id'] for turn in status['turns'])

    # The exact production call shape: every checkpoint field except the
    # redundant retained-position index list, which the storage layer derives.
    shorthand = {key: deepcopy(value) for key, value in anchor.items()
                 if key != 'retainedToolRoundPositions'}
    resumed = s.call('control_conversation', {**s.args, 'resume_anchor': shorthand})
    assert 'error' not in resumed, resumed
    assert len(s.started) == 1
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    kept = [item['toolCallId'] for item in after['projection']['toolRounds']]
    assert kept == [f'durable-call-{position}' for position in range(3)], kept
    assert after['projection']['content'] == ''
    assert after['currentAttemptId'] == resumed['attempt']['attemptId']

    # Repeating the same command replays the same Attempt; no second launch.
    replay = s.call('control_conversation', {**s.args, 'resume_anchor': shorthand})
    assert 'error' not in replay, replay
    assert replay['attempt']['attemptId'] == resumed['attempt']['attemptId']
    assert len(s.started) == 1

    # The saved checkpoint and its lineage are exactly what the accepted
    # resume left: replay did not launch a second execution or amputate work.
    preserved = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert preserved['projection'] == after['projection']
    assert preserved['currentAttemptId'] == resumed['attempt']['attemptId']
    assert len(s.started) == 1


def test_checkpoint_resume_rejects_a_changed_anchor_at_the_anchor_fence(
        tool_checkpoint_session):
    """A changed or malformed anchor is a typed conflict that writes nothing.

    The negative cases run against the SETTLED turn, never the resumed one:
    the storage live-attempt fence would otherwise fire first and these
    assertions would pass without reaching the anchor comparison at all
    (found by the 2026-11-10 freeze audit). The saved checkpoint must survive
    every refusal byte-identically, and remain resumable afterwards.
    """
    s = tool_checkpoint_session
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert before['status'] == 'failed'
    anchor = s.option['anchor']
    shorthand = {key: deepcopy(value) for key, value in anchor.items()
                 if key != 'retainedToolRoundPositions'}
    bad_anchors = {
        'changed-boundary': {**shorthand, 'keptToolRounds': anchor['keptToolRounds'] - 1},
        'explicit-positions-widened': {**shorthand,
                                       'retainedToolRoundPositions': [0, 1, 2, 3]},
        'empty-positions': {**shorthand, 'retainedToolRoundPositions': []},
        'extra-field': {**shorthand, 'unexpected': True},
        'empty-anchor': {},
        'wrong-type': {'type': 'turn_start'},
    }
    from lib.turn_lifecycle import LifecycleConflict, create_attempt
    for name, bad_anchor in bad_anchors.items():
        with pytest.raises(LifecycleConflict) as anchor_conflict:
            create_attempt('session', s.args['turn_id'], command_id=f'different-selection-{name}',
                           operation='checkpoint_resume',
                           expected_projection_revision=before['projectionRevision'],
                           resume_anchor=bad_anchor, user_id=1)
        assert anchor_conflict.value.code == 'database_conflict', (name, anchor_conflict.value)
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert after['projection'] == before['projection']
    assert after['projectionRevision'] == before['projectionRevision']
    assert after['currentAttemptId'] == before['currentAttemptId']
    assert after['settlement'] == before['settlement']
    assert not s.started
    # The refusals leave the same checkpoint resumable through the shorthand.
    resumed = s.call('control_conversation', {**s.args, 'resume_anchor': shorthand})
    assert 'error' not in resumed, resumed
    assert len(s.started) == 1


def test_checkpoint_resume_survives_a_lost_ack_and_a_stale_revision(
        tool_checkpoint_session, monkeypatch):
    """An uncertain ACK replays one Attempt; a stale revision is fenced.

    The ACK of an accepted resume is lost once: retrying the identical command
    returns the SAME Attempt without launching a second execution, and every
    durable tool result survives. A later command at the pre-resume revision
    is refused by the projection CAS, which is the fence a genuinely
    concurrent projection writer trips. Neither path may rewrite the saved
    checkpoint or launch the Attempt twice.
    """
    s = tool_checkpoint_session
    from lib.storage.errors import StorageError
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    create = s.commands.create_attempt
    lost = False

    def lose_first_ack(*args, **kwargs):
        nonlocal lost
        result = create(*args, **kwargs)
        if not lost:
            lost = True
            raise StorageError('database_unavailable', 'Accepted command ACK was lost')
        return result

    monkeypatch.setattr(s.commands, 'create_attempt', lose_first_ack)
    uncertain = s.call('control_conversation', s.args)
    assert uncertain['execution_started'] is None
    assert len(s.started) == 1
    first = s.call('control_conversation', s.args)
    assert 'error' not in first, first
    second = s.call('control_conversation', s.args)
    assert second['attempt']['attemptId'] == first['attempt']['attemptId']
    assert len(s.started) == 1
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert [item['toolCallId'] for item in after['projection']['toolRounds']] == [
        item['toolCallId'] for item in before['projection']['toolRounds']
        if not item.get('_providerAttemptDiscarded')]

    # A different command at the now-stale revision is refused without
    # mutation. The session layer pins the source Attempt, so a replayed
    # command identity is refused at that fence; the storage revision fence
    # below is exercised directly against the same stale revision.
    stale = s.call('control_conversation', {**s.args, 'idempotency_key': 'stale-revision'})
    assert 'error' in stale, stale
    assert len(s.started) == 1
    from lib.turn_lifecycle import LifecycleConflict, create_attempt
    with pytest.raises(LifecycleConflict) as stale_revision:
        create_attempt('session', s.args['turn_id'], command_id='stale-revision-direct',
                       operation='checkpoint_resume',
                       expected_projection_revision=s.args['expected_revision'],
                       resume_anchor=s.option['anchor'], user_id=1)
    assert stale_revision.value.code == 'stale_projection', stale_revision.value
    assert len(s.started) == 1
    assert s.turns.get_turn('session', s.args['turn_id'],
                            user_id=1)['currentAttemptId'] == first['attempt']['attemptId']


def test_checkpoint_resume_refusal_leaves_the_saved_turn_byte_identical(
        tool_checkpoint_session, monkeypatch):
    """A refused resume writes nothing, and the checkpoint stays resumable.

    The conflict is injected at the lifecycle entry point, BEFORE storage is
    called, so this covers the refusal's no-write guarantee and the Turn's
    continued resumability — not mid-transaction rollback. Native
    mid-transaction atomicity is covered at the storage boundary by
    turn_tests (conflicts precede all mutations and has_business_mutation is
    false) and by the existing attempt_live_tail/conflict specs.
    """
    s = tool_checkpoint_session
    from lib.storage.errors import StorageError
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    original = s.turns.create_attempt
    injected = False

    def fail_once(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise StorageError('database_conflict', 'Injected concurrent writer conflict')
        return original(*args, **kwargs)

    monkeypatch.setattr(s.turns, 'create_attempt', fail_once)
    import lib.conversation_sync.command_service as command_service
    monkeypatch.setattr(command_service, 'create_attempt', fail_once)
    refused = s.call('control_conversation', s.args)
    assert refused['code'] == 'database_conflict', refused
    assert not s.started
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert after['projection'] == before['projection']
    assert after['projectionRevision'] == before['projectionRevision']
    assert after['currentAttemptId'] == before['currentAttemptId']
    assert after['settlement'] == before['settlement']

    resumed = s.call('control_conversation', s.args)
    assert 'error' not in resumed, resumed
    assert len(s.started) == 1
