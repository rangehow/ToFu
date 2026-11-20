"""Scripted ROOT integration with synthetic execution and synchronous finalization ports."""
from concurrent.futures import Future
from copy import deepcopy
import json
from importlib import import_module
import threading
from types import SimpleNamespace

import pytest

from tests.goals.test_goal_invocation_integration import runtime as root_runtime
from tests.project_mod.test_coordinator_progress import Store

pytestmark = pytest.mark.unit

WAIT = 'Waiting for the synthetic measurement slot; no owner action required.'
CHOICE = 'For the synthetic report, do you prefer a table or prose?'
NARRATIVE = ('Prepare a synthetic latency report. The measurement slot is internally unavailable. '
             'Meanwhile summarize the supplied sample. Ask me only whether I prefer table or prose; '
             'continue the independent summary without waiting for that preference.')


@pytest.fixture
def confinement(tmp_path, monkeypatch):
    from lib.tasks_pkg.orchestrator import _finalize, _post_commit
    # Import consumers before replacing the package emitter, so they cannot retain a fixture closure.
    import_module('lib.tasks_pkg.orchestrator._run')
    import_module('lib.tasks_pkg.handlers.misc._human')

    project = tmp_path / 'synthetic-project'
    project.mkdir()
    state = SimpleNamespace(project=str(project), task=None, finalization=[], denied=[], events=[],
                            ingress=[], active_ingress=None)
    caller_thread = threading.get_ident()
    for name in ('_user_prompt_hooks', '_pre_hooks', '_post_hooks',
                 '_pre_compact_hooks', '_post_compact_hooks'):
        monkeypatch.setattr('lib.tasks_pkg.tool_hooks.' + name, [])

    def deny(label):
        def refused(*args, **kwargs):
            state.denied.append(label)
            pytest.fail('forbidden fixture effect: ' + label)
        return refused

    def checked(task):
        assert threading.get_ident() == caller_thread
        assert task is state.task
        assert task['config']['projectPath'] == state.project
        assert task['config']['projectPaths'] == [state.project]

    def capture(label):
        def observed(task, *args, **kwargs):
            checked(task)
            state.finalization.append(label)
        return observed

    def persist(task, **kwargs):
        checked(task)
        assert task['status'] == 'done' and not task.get('error')
        state.finalization.append('terminal-port')
        return True

    def settlement(task, project_path, *, user_id):
        checked(task)
        assert project_path == state.project and user_id == 7
        state.finalization.append('project-settlement-port')

    def commit(task, enabled, project_path, project_paths=None):
        checked(task)
        assert enabled and project_path == state.project and project_paths == [state.project]
        state.finalization.append('commit-observer-port')

    def profile(task, messages, cfg):
        checked(task)
        assert messages is task['messages'] and cfg is task['config']
        state.finalization.append('profile-observer-port')

    def local_event(task, event):
        checked(task)
        state.events.append(dict(event))
        return True

    def begin_ingress(task, *, span_id):
        checked(task)
        assert state.active_ingress is None
        state.active_ingress = span_id
        state.ingress.append(('begin', span_id))
        return span_id

    def end_ingress(task, *, token):
        checked(task)
        assert token == state.active_ingress
        state.active_ingress = None
        state.ingress.append(('end', token))
        return {}

    monkeypatch.setattr('lib.tasks_pkg.manager._stream.begin_provider_ingress', begin_ingress)
    monkeypatch.setattr('lib.tasks_pkg.manager._stream.end_provider_ingress', end_ingress)
    # Patch the finalizer's bound aliases, not just the original spawner exports.
    monkeypatch.setattr(_finalize, 'persist_task_result', persist)
    monkeypatch.setattr(_finalize, '_spawn_async_commit_round', commit)
    monkeypatch.setattr(_finalize, '_spawn_async_profile_consolidation', profile)
    monkeypatch.setattr(_post_commit, 'notify_terminal_conversation_change', capture('terminal-notice-port'))
    for target, label in (
        ('lib.tasks_pkg.event_invariants.maybe_check_task_log', 'invariants-port'),
        ('lib.tasks_pkg.compaction.api.settle_scheduled_compaction', 'compaction-port'),
        ('lib.swarm.delivery.restore_unconsumed_results', 'swarm-restore-port'),
        ('lib.swarm.turn_handoff.resume_swarm_after_turn', 'swarm-resume-port'),
        ('lib.swarm.turn_handoff.prepare_swarm_turn_end', 'swarm-end-port'),
        ('lib.agent_core.settlement.notify_project_task_settled', None),
        ('lib.tasks_pkg.manager._persist._release_heavy_task_state', 'heavy-release-port'),
        ('lib.tasks_pkg.manager._persist._release_terminal_tool_state', 'tool-release-port'),
        ('lib.tasks_pkg.orchestrator._completion_observers.observe_completion', 'completion-port'),
        ('lib.tasks_pkg.orchestrator._finalize._salvage_undelivered_steer', 'steer-salvage-port'),
    ):
        monkeypatch.setattr(target, settlement if label is None else capture(label))
    monkeypatch.setattr(_finalize, 'observe_completion', capture('completion-port'))
    store = SimpleNamespace(inject_project_messages=lambda *a, **kw: None,
                            flush_file_signals=capture('signal-flush-port'))
    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: store)
    monkeypatch.setattr('lib.presence.mark_idle', lambda *a, **kw: None)
    monkeypatch.setattr('lib.swarm.integration._rehydrate.resume_swarm_for_continue', lambda *a: None)
    monkeypatch.setattr('lib.tasks_pkg.workspace_reference_delivery.append_workspace_output_links',
                        capture('workspace-links-port'))
    monkeypatch.setattr('lib.artifacts.scan_message', lambda *a, **kw: None)
    for target in ('lib.tasks_pkg.manager.append_event',
                   'lib.tasks_pkg.executor._finalize.append_event',
                   'lib.tasks_pkg.tool_dispatch._pipeline.append_event',
                   'lib.tasks_pkg.tool_dispatch._heartbeat.append_event',
                   'lib.tasks_pkg.orchestrator._finalize.append_event',
                   'lib.tasks_pkg.orchestrator._post_loop.append_event',
                   'lib.tasks_pkg.orchestrator._round_open.append_event',
                   'lib.tasks_pkg.handlers.misc._human.append_event',
                   'lib.tasks_pkg.orchestrator._tool_loop_breaker.append_event'):
        monkeypatch.setattr(target, local_event)
    for target in (
        'lib.tasks_pkg.manager._provider_ingress_guard.begin_provider_ingress',
        'lib.tasks_pkg.commit_round._commit._spawn_async_commit_round',
        'lib.tasks_pkg.commit_round._commit._run_commit_round_async',
        'lib.tasks_pkg.commit_round._profile._spawn_async_profile_consolidation',
        'lib.tasks_pkg.commit_round._profile._run_profile_consolidation_async',
        'lib.tasks_pkg.workspace_delivery.capture_workspace_evidence',
        'lib.tasks_pkg.workspace_delivery.persist_terminal_workspace_intent',
        'lib.tasks_pkg.workspace_delivery.settle_task_workspaces',
        'lib.linear_git_checkpoint.settle_task_checkpoint',
        'lib.file_history.make_snapshot',
        'lib.tasks_pkg.manager._persist.persist_task_result',
        'lib.tasks_pkg.orchestrator._post_loop.persist_task_result',
        'lib.tasks_pkg.human_guidance.request_human_guidance',
        'lib.agent_inbox.drain',
        'lib.message_queue.enqueue_message',
        'lib.tasks_pkg.committed_steers.requeue_committed_steer',
        'lib.llm.stream_chat',
        'lib.llm.astream.async_stream_chat',
        'subprocess.Popen',
        'socket.socket.connect',
        'socket.socket.connect_ex',
        'threading.Thread.start',
    ):
        monkeypatch.setattr(target, deny(target))

    monkeypatch.setattr(import_module('lib.llm.chat'), 'chat', deny('lib.llm.chat.chat'))

    class InlinePool:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, *args, **kwargs):
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr('lib.tasks_pkg.context_composer.turn_requirements.context_provider_executor',
                        InlinePool())
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.InstrumentedThreadPoolExecutor', InlinePool)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline._start_tool_heartbeat',
                        lambda *a: (threading.Event(), None))
    state.commit = commit
    state.settlement = settlement
    state.profile = profile
    yield state
    assert state.denied == []
    assert state.active_ingress is None
    assert list(project.iterdir()) == []


@pytest.fixture
def board(confinement):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress

    store, notices = Store(), []
    bindings = SimpleNamespace(read=lambda path: {
        'availability': 'available', 'conversationId': 'coordinator', 'revision': 1})
    clock = iter(range(1000, 10_000, 10))

    def factory(owner, path):
        assert owner == 7 and path == confinement.project
        return CoordinatorTaskProgress(owner, path, client=store, bindings=bindings,
            notify=lambda: notices.append('local-board-notice'), clock=lambda: next(clock))

    return SimpleNamespace(store=store, factory=factory)


@pytest.fixture
def protected_root(confinement, request):
    return request.getfixturevalue('root_runtime')


@pytest.fixture
def journey(confinement, protected_root, board, monkeypatch):
    from lib.conversations import coordinator_progress, coordinator_workers, project_coordination
    from lib.identity import PrincipalContext
    from lib.project_dispatch import repository, admission
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.storage import service as storage_service
    from tests.support.turn_reads import turn_read

    s, task = confinement, protected_root.parent
    s.task = task
    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7, tenant_id='tenant-a')
    binding = SimpleNamespace(value={'projectPath': s.project, 'conversationId': 'coordinator',
        'availability': 'available', 'revision': 1})

    def read_binding(project):
        assert project == s.project
        return deepcopy(binding.value)

    binding.read = read_binding

    class Conversations:
        snapshot = True
        rows = {name: {'user_id': 7, 'settings': {'projectPath': s.project}}
                for name in ('coordinator', 'recipient')}

        def get(self, conversation_id, *, user_id, include_messages):
            assert user_id == 7
            row = self.rows.get(conversation_id)
            return SimpleNamespace(metadata=deepcopy(row)) if row else None

    conversations = Conversations()
    task.pop('_tool_env')
    task.pop('_transientRuntime', None)
    task.pop('_inline_messages', None)
    task['_suppressEvents'] = True
    for name in ('_vault_values', '_server_config_values', '_model_routing_values'):
        monkeypatch.setattr('lib.tool_results.credential_redaction.' + name, lambda *a, **kw: [])
    for target in ('lib.tasks_pkg.context_composer.working_facts.refresh_working_facts',
                   'lib.conversations.project_messages.inject',
                   'lib.tasks_pkg.persistence_wait.checkpoint_invocation',
                   'lib.tasks_pkg.orchestrator._round_checkpoint.checkpoint_task_partial',
                   'lib.acceptance.turns.read_execution_requirements',
                   'lib.tasks_pkg.round_reads.begin_round_reads',
                   'lib.tasks_pkg.round_reads.prefetch_round_reads'):
        monkeypatch.setattr(target, lambda *a, **kw: None)
    task.update(convId='coordinator', _userId=7, _tenant_id='tenant-a', _turnId='coordinator-turn',
                _attemptId='coordinator-attempt', _attended=True,
                _principalContext=principal.to_payload())
    task['messages'] = [{'role': 'user', 'content': NARRATIVE}]
    task['config'].update(projectPath=s.project, projectPaths=[s.project], codeExecEnabled=True,
                          autoApply=True, _turnOwnerUserId=7, tools={'resultEnvelope': 'legacy'})
    task['_principalContext']['scopes'] = []
    turns = {'human-turn': {'conversationId': 'coordinator', 'turnId': 'human-turn', 'actor': 'human',
        'projectionRevision': 3, 'projection': {'role': 'user', 'content': NARRATIVE}},
        'coordinator-turn': {'conversationId': 'coordinator', 'turnId': 'coordinator-turn',
        'actor': 'assistant', 'parentTurnId': 'human-turn', 'currentAttemptId': 'coordinator-attempt',
        'projectionRevision': 3, 'projection': {}}}
    attempts = {'coordinator-attempt': {'conversationId': 'coordinator',
        'turnId': 'coordinator-turn', 'attemptId': 'coordinator-attempt',
        'taskId': task['id'], 'status': 'running', 'config': deepcopy(task['config'])}}
    repo = repository.ProjectDispatchRepository(7, s.project, client=board.store, storage_version=1,
        conversations=conversations, turn_get=lambda conv, turn, **kw: turn_read(turns.get(turn), **kw),
        binding_repository=binding, clock=lambda: 1000)
    repo.attempt_get = lambda attempt, **kw: deepcopy(attempts.get(attempt))
    repo.workspace_get = lambda *a, **kw: None
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', repo.turn_get)
    monkeypatch.setattr(repository, 'ProjectDispatchRepository', lambda owner, path: repo)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchRepository', lambda owner, path: repo)
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: binding)
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository', lambda: conversations)
    monkeypatch.setattr('lib.conversations.project_messages.ConversationRepository', lambda: conversations)
    monkeypatch.setattr('lib.conversations.session_model._conversation_settings', lambda *a: {})
    monkeypatch.setattr(admission, 'repository_for', lambda binding: repo)
    monkeypatch.setattr(coordinator_progress, 'CoordinatorTaskProgress',
                        lambda owner, path, **kw: board.factory(owner, path))
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', lambda *a, **kw: None)
    from lib.conversations import coordinator_births
    from lib.project_dispatch import request_budget
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    monkeypatch.setattr(request_budget, 'spawn', lambda body, name: body())
    created = []

    class Memory:
        def query(self, operation, payload):
            return board.store.query(operation, payload)

        def command(self, operation, payload, command_id):
            if operation.startswith('record.'):
                return board.store.command(operation, payload, command_id)
            assert operation == 'conversation.create', operation
            assert payload['user_id'] == 7
            conversations.rows[payload['conv_id']] = {
                'user_id': 7, 'settings': deepcopy(payload['settings'])}
            created.append(payload['conv_id'])

    monkeypatch.setattr(storage_service, '_runtime', SimpleNamespace(client=lambda **kw: Memory()))

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            assert owner == 7 and kwargs['principal'].kind == 'system'
            assert request['inputActor'] == 'virtual_user'
            config = trusted.capture(request['config'], owner, kwargs['principal'], conv)
            turn, attempt = 'turn-' + conv, 'attempt-' + conv
            trusted.bind_execution(turn, attempt)
            turns[turn] = {'conversationId': conv, 'turnId': turn, 'actor': 'assistant',
                          'projectionRevision': 1, 'projection': {'content': 'sample alpha: 12 ms'}}
            attempts[attempt] = {'conversationId': conv, 'turnId': turn, 'attemptId': attempt,
                'taskId': 'task-' + conv,
                'status': 'completed' if request['inputTurn']['content'].startswith('Summarize') else 'running',
                'config': config}
            return SimpleNamespace(value={})

    service = ProjectDispatchService(repo, commands=Commands(),
        turn_get=repo.turn_get, attempt_get=repo.attempt_get)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda repo: service)
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: service)
    capture_worker_policy(task, task['config'], [{'function': {'name': 'read_files'}}],
                          'fixture-model', worker_model='fixture-model')
    questions = []
    monkeypatch.setattr('lib.router.relay.relay_project_question', lambda *a: None)
    monkeypatch.setattr('lib.router.proactive.questions.record_question',
                        lambda *a, **kw: questions.append((a, kw)))
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda *a, **kw: [])
    monkeypatch.setattr('lib.channels.tools.visible_tools', lambda *a, **kw: [])
    return SimpleNamespace(task=task, repo=repo, turns=turns, attempts=attempts, safety=s,
        board=board, requests=[], questions=questions, created=created, events=s.events)


def _run(r, monkeypatch, *, extra_question=False):
    import lib.tasks_pkg.manager._stream as stream
    import lib.tasks_pkg.orchestrator._run as runner
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools

    assert coordinator_scope(r.task) == {'user_id': 7, 'project_path': r.safety.project,
        'conversation_id': 'coordinator', 'binding_revision': 1}
    r.task['config'].update(memoryEnabled=True, mcpEnabled=False, plugins=[])
    catalog, available = assemble_round_tools(r.task['config'], r.task, {
        'tid': r.task['id'], 'project_enabled': True, 'project_path': r.safety.project,
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': 'fixture-model'})
    assert available
    assert {'update_task_progress', 'delegate_project_work', 'poll'} <= {
        tool['function']['name'] for tool in catalog}
    r.task['_executable_tool_catalog'] = catalog
    monkeypatch.setattr(runner, 'assemble_round_tools', lambda *a: (catalog, True))
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._ports.build_request_body',
        lambda model, messages, **kw: {'model': model,
            'messages': json.loads(json.dumps(messages)), 'tools': deepcopy(kw['tools'])})
    rounds = [
        [('update_task_progress', {'summary': 'Prepare synthetic latency report', 'tasks': [
            {'title': 'Measure latency', 'status': 'pending', 'progress': WAIT,
             'blocker': {'kind': 'next_action', 'action': 'Measure the synthetic latency sample'}},
            {'title': 'Summarize sample', 'status': 'in_progress'}]})],
        [('delegate_project_work', {'title': 'Measure latency',
            'brief': 'Wait for the synthetic measurement slot. No external effects.',
            'idempotency_key': 'measurement'})],
        [('ask_human', {'question': CHOICE, 'response_type': 'free_text', 'blocking': False})],
        [('delegate_project_work', {'title': 'Summarize sample',
            'brief': 'Summarize the supplied synthetic alpha sample, 12 ms. No external effects.',
            'idempotency_key': 'summary'})],
        [('get_project_assignment', {'assignment_id': 'summary'})],
        [('update_task_progress', {'changes': [{'title': 'Summarize sample',
            'status': 'completed', 'progress': 'Supplied sample alpha: 12 ms.'}]})],
        [('update_task_progress', {'read': {}})],
        [('poll', {'ids': ['measurement'], 'wait_s': 0})],
        [('poll', {'ids': ['measurement'], 'wait_s': 0})],
    ]
    if extra_question:
        rounds.insert(2, [('ask_human', {'question': 'Should I wait for the measurement slot?',
                                       'response_type': 'free_text', 'blocking': False})])

    def provider(body, **kwargs):
        index = len(r.requests)
        assert index < len(rounds), 'root did not park after the unchanged Worker wait'
        r.requests.append(deepcopy(body))
        assert kwargs['owner_user_id'] == 7
        selected = deepcopy(rounds[index])
        assignments = {row['request']['brief'].split()[0]: row['id'] for row in r.repo.list()}
        for name, args in selected:
            if name == 'get_project_assignment':
                args['assignment_id'] = assignments['Summarize']
            if name == 'poll':
                args['ids'] = [assignments['Wait']]
        calls = [{'id': f'round-{index}-{position}', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}
            for position, (name, args) in enumerate(selected)]
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '', 'tool_calls': calls}, 'tool_calls',
            {'prompt_tokens': 40, 'completion_tokens': 3, 'total_tokens': 43})

    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    execute_invocation(r.task, policy=ROOT_INVOCATION_POLICY)
    assert len(r.requests) == len(rounds)
    assert [row['toolName'] for row in r.task['toolRounds']] == [name for batch in rounds for name, _ in batch]
    assert len(r.task['toolRounds']) == len(r.requests)
    assert len(r.safety.ingress) == 2 * len(r.requests)
    for opened, closed in zip(r.safety.ingress[::2], r.safety.ingress[1::2], strict=True):
        assert opened[0] == 'begin' and closed == ('end', opened[1])
    assert r.safety.finalization == [
        'steer-salvage-port', 'swarm-end-port', 'workspace-links-port', 'completion-port', 'terminal-port',
        'invariants-port', 'compaction-port', 'terminal-notice-port', 'swarm-restore-port',
        'swarm-resume-port', 'signal-flush-port', 'project-settlement-port',
        'commit-observer-port', 'heavy-release-port', 'profile-observer-port', 'tool-release-port']
    assert r.safety.denied == []


def _assert_sole_question(r):
    assert [row['guidanceQuestion'] for row in r.task['toolRounds']
            if row['toolName'] == 'ask_human'] == [CHOICE], 'unnecessary owner question'


def _assert_journey(r):
    questions = [row for row in r.task['toolRounds'] if row['toolName'] == 'ask_human']
    assert all(row['guidanceBlocking'] is False for row in questions)
    assert all('guidanceResponse' not in row for row in questions)
    assert '_ownerGuidanceAnswers' not in r.task
    assert not any(event['type'] == 'human_guidance_request' for event in r.events)
    assert all(row['status'] == 'done' for row in r.task['toolRounds'])
    assert r.task.get('_toolLoopCleanFinish', {}).get('reason') == 'coordinator_idle_wait'
    parked, = [event for event in r.events if event['type'] == 'round_end'
               and event.get('reason') == 'coordinator_idle_wait']
    assert parked['roundNum'] == len(r.requests) - 1
    assert r.task.get('error') is None
    board = r.board.factory(7, r.safety.project).read()['board']
    assert board['tasks'][0]['progress'] == WAIT
    assert board['tasks'][1]['status'] == 'completed'
    assert len(r.created) == len(r.repo.list()) == 2
    assert not r.repo._load()[0].get('coordinatorSettlements')
    assert len(r.questions) == len(questions)
    assert [entry[0][1]['question'] for entry in r.questions] == [row['guidanceQuestion'] for row in questions]
    assert all(entry[0][1]['blocking'] is False for entry in r.questions)
    assert all(row.get('executionEffect', {}).get('state') == 'returned' for row in r.task['toolRounds'])
    for row, following in zip(r.task['toolRounds'][:-1], r.requests[1:], strict=True):
        reply, = [message for message in following['messages']
                  if message.get('tool_call_id') == row['toolCallId']]
        assert reply['role'] == 'tool' and reply['content'] == str(row['toolContent'])
    results = [message for message in r.requests[-1]['messages'] if message['role'] == 'tool']
    assert any('without waiting' in message['content'] for message in results)
    assert any('accepted' in message['content'] for message in results)
    viewed = next(json.loads(row['toolContent']) for row in r.task['toolRounds']
                  if row['toolName'] == 'get_project_assignment')
    assert viewed['execution']['status'] == 'completed'
    assert viewed['execution']['result']['content'] == 'sample alpha: 12 ms'
    assert viewed['review'] is None and viewed['canonicalDelivery'] is False
    waits = [json.loads(row['toolContent']) for row in r.task['toolRounds'][-2:]]
    assert [waited['wait'] for waited in waits] == ['progress', 'timeout']
    assert waits[0]['workers'][0]['status'] == 'completed'
    assert waits[0]['workers'][0]['result'] == 'sample alpha: 12 ms'
    measurement = next(row for row in r.repo.list() if row['request']['brief'].startswith('Wait'))
    assert r.attempts[measurement['execution']['attemptId']]['status'] == 'running'
    assert waits[1]['workers'] == []
    assert any(WAIT in message['content'] and 'completed' in message['content'] for message in results)


def test_scripted_coordinator_keeps_internal_wait_off_owner_question_and_progresses(journey, monkeypatch):
    _run(journey, monkeypatch)
    _assert_journey(journey)
    _assert_sole_question(journey)


def test_extra_question_control_is_detected_on_the_same_root_path(journey, monkeypatch):
    _run(journey, monkeypatch, extra_question=True)
    _assert_journey(journey)
    with pytest.raises(AssertionError, match='unnecessary owner question'):
        _assert_sole_question(journey)


def test_finalization_ports_refuse_unowned_paths_and_original_observers(journey):
    from lib.tasks_pkg.commit_round import _commit
    from lib.tasks_pkg.orchestrator import _finalize, _post_commit

    r = journey
    r.task['status'] = 'done'
    _post_commit.finish_committed_task(r.task, messages=r.task['messages'], cfg=r.task['config'],
        project_enabled=True, project_path=r.safety.project,
        commit_round=_finalize._spawn_async_commit_round,
        consolidate_profile=_finalize._spawn_async_profile_consolidation)
    expected = ['invariants-port', 'compaction-port', 'terminal-notice-port', 'swarm-restore-port',
        'swarm-resume-port', 'signal-flush-port', 'project-settlement-port',
        'commit-observer-port', 'heavy-release-port', 'profile-observer-port', 'tool-release-port']
    assert r.safety.finalization == expected
    with pytest.raises(AssertionError):
        r.safety.commit(r.task, True, str(r.safety.project) + '-unowned', [r.safety.project])
    with pytest.raises(AssertionError):
        r.safety.settlement(r.task, str(r.safety.project) + '-unowned', user_id=7)
    with pytest.raises(pytest.fail.Exception, match='forbidden fixture effect'):
        _commit._spawn_async_commit_round(r.task, True, r.safety.project)
    assert r.safety.denied == ['lib.tasks_pkg.commit_round._commit._spawn_async_commit_round']
    r.safety.denied.clear()
    assert r.safety.finalization == expected
