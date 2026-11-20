"""One accepted Coordinator request delegates via actual native Turn admission.

No paid provider: substitute transport only, retain native authorization, budgets,
ordinary tool resolution, isolated Git workspaces and report/unread persistence.
"""
from pathlib import Path
import threading
import uuid

import pytest

from tests.storage.test_native_project_dispatch import native_dispatch_authority as native_dispatch_authority, _git

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)]


@pytest.mark.parametrize('continue_after_restart', [False, True])
def test_native_delegate_execute_await_report_and_restart(native_dispatch_authority, tmp_path, monkeypatch, continue_after_restart):
    from lib import turn_lifecycle as turns, integration_control as ic
    from lib.project_mod import modifications, delivery
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations import coordinator_workers
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.repository import ConversationRepository
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.agent_core.execution_budget import model_budget_admission
    import json

    factory, owner, engine = native_dispatch_authority
    root = tmp_path / 'canonical'; root.mkdir()
    _git(root, 'init', '-b', 'coordinator-workers')
    _git(root, 'config', 'user.name', 'Test')
    _git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt'); _git(root, 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **kw: None)
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **kw: (True, 'fixture verification'))
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: False)
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)
    # Births run inline so this journey observes each delegation's outcome; their
    # own thread is specified in tests/project_mod/test_coordinator_births.py.
    from lib.conversations import coordinator_births
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    binding = ProjectCoordinationRepository(owner).create(str(root), folder_id='native-folder',
        expected_revision=0, command_id='coordinator')
    conv = binding['conversationId']
    pair = turns.create_turn_pair(conv, user_id=owner, command_id='original-request',
        input_projection={'content': 'Implement both parts and verify the result'}, config={})
    root_id = 'native-file-coordinator-' + uuid.uuid4().hex
    turns.bind_task(pair['attempt']['attemptId'], root_id, user_id=owner)
    turns.mark_task_started(pair['attempt']['attemptId'], root_id, user_id=owner)
    task = {'id': root_id, 'convId': conv, '_userId': owner, '_attended': True, 'messages': [],
            '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            '_principalContext': PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                tenant_id='native-test', scopes=[]).to_payload(),
            'config': {'projectPath': str(root), 'codeExecEnabled': True, 'autoApply': True,
                       # As pinned at Coordinator request ingress.
                       'tools': {'programmaticCalling': 'off'},
                       '_coordinatorSubmittedToolSettings': {'programmaticCalling': 'on'},
                       # Server-stamped into every accepted attempt config (docs/PROJECT_COORDINATION.md,
                       # "Conversation execution identity boundary"); tool assembly reads the owner here.
                       '_turnOwnerUserId': owner}}
    from lib.project_dispatch.repository import CONTRACT
    names = ['read_files', 'write_file', 'run_command', 'spawn_agents', 'timer_create']
    if continue_after_restart:
        names += CONTRACT['continuationTools']
    capture_worker_policy(task, task['config'], [{'function': {'name': n}} for n in names], 'fixture-model',
                          worker_model='fixture-model')
    if continue_after_restart:
        # Capture the original ceiling through actual root assembly, then later
        # recover it from a planning-only synthetic Coordinator catalog.
        assemble_round_tools(task['config'], task, {'model': 'fixture-model',
            'project_path': str(root), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False})
        assert 'get_project_workers' in task['_coordinatorWorkerPolicy'].coordinator_tools
    repo = ProjectDispatchRepository(owner, str(root))
    from lib.project_dispatch.request_budget import bind_source_account, usage
    source_budget = bind_source_account(task, repository=repo)
    source_lease = task['_executionBudget'].reserve_model(task, input_tokens=5, max_output_tokens=5)
    source_lease.mark_dispatched(); source_lease.settle({'prompt_tokens': 5, 'completion_tokens': 5})
    sends, workspaces, work_ids = [], [], []

    def transport(conv, cfg, data, watermark, bind, **kwargs):
        identity = 'native-file-worker-' + uuid.uuid4().hex
        bind(identity)
        worker = {'id': identity, 'convId': conv, 'config': cfg, 'status': 'running',
            'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': owner,
            '_principalContext': admission.principal_for(cfg[admission.KEY]).to_payload(),
            '_tenant_id': admission.principal_for(cfg[admission.KEY]).tenant_id,
            '_attended': False, '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
            'abort_event': threading.Event()}
        admission.bind_account(worker)
        if conv == binding['conversationId']:
            assert not admission.requires_isolated_workspace(worker)
            schemas, available = assemble_round_tools(cfg, worker, {'model': 'fixture-model',
                'project_path': str(root), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
                'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
                'desktop_enabled': False, 'image_gen_enabled': False})
            assert available
            catalog = {s['function']['name'] for s in worker['_executable_tool_catalog']}
            assert {'delegate_project_work', 'get_project_workers', 'finish_project_request'} <= catalog
            assert not {'write_file', 'run_command', 'spawn_agents'} & catalog
            worker['_testCoordinatorSchemas'] = schemas
            lease = model_budget_admission(worker)(input_tokens=4, max_output_tokens=6)
            lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
            continuations.append(worker)
            return identity, None
        bind_worker_workspace(worker)
        work = Path(worker['config']['projectPath']); workspaces.append(work)
        # The checkout belongs to the assignment's work id, not the physical task.
        work_ids.append(admission.owned_workspace(worker)['task_id'])
        assert work != root and work_ids[-1] != identity
        schemas, available = assemble_round_tools(cfg, worker, {'model': 'fixture-model',
            'project_path': str(work), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False})
        assert available
        catalog = {s['function']['name'] for s in worker['_executable_tool_catalog']}
        # Same capabilities as the human's ordinary conversation, minus role
        # tools and deferred wakes that would outlive the grant.
        assert {'read_files', 'write_file', 'run_command', 'spawn_agents'} <= catalog
        assert not {'timer_create', 'delegate_project_work'} & catalog
        assert cfg['tools']['programmaticCalling'] == 'on'
        from lib.tasks_pkg.orchestrator._tool_assembly_prep import attach_gateway_contract_documents
        assert attach_gateway_contract_documents(worker, 'off')
        assert 'execute_program' in worker['_tool_gateway_names']
        lease = model_budget_admission(worker)(input_tokens=4, max_output_tokens=6)
        lease.mark_dispatched(); sends.append(identity)
        lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        args = {'path': 'result.txt', 'content': 'native independent worker result\n'}
        entry = {'name': 'write_file', 'arguments': args, 'query': 'Implement the assigned result'}
        result = _execute_tool_one(worker, {}, 'write_file', 'native-write', args, 0, entry,
                                   cfg, str(work), True, schemas)
        assert 'Error:' not in str(result[1]), result
        assert (work / 'result.txt').read_text() == args['content']
        assert not (root / 'result.txt').exists()
        command = {'command': 'python3 -c "from pathlib import Path; assert Path(\'result.txt\').read_text().startswith(\'native independent\')"'}
        checked = _execute_tool_one(worker, {}, 'run_command', 'native-check', command, 1,
            {'name': 'run_command', 'arguments': command, 'query': 'Verify written result'}, cfg, str(work), True, schemas)
        assert 'Error:' not in str(checked[1]), checked
        # A Worker's sub-agent borrows the Worker's live grant and isolated workspace.
        from lib.tasks_pkg.child_invocation import create_child_invocation, close_unstarted_child_invocation
        child = create_child_invocation(worker, model='fixture-model',
            tools=[s for s in schemas if s['function']['name'] in ('read_files', 'run_command')],
            objective='Inspect result.txt', project_path=str(work),
            event_sink=lambda event: None, abort_check=lambda: False)
        try:
            assert admission.validate_task(child) == admission.validate_task(worker)
            child_cfg = child['config']
            child_read = _execute_tool_one(child, {}, 'read_files', 'native-child-read', {'path': 'result.txt'}, 0,
                {'name': 'read_files', 'arguments': {'path': 'result.txt'}, 'query': 'Inspect'},
                child_cfg, child_cfg['projectPath'], True, schemas)
            assert 'native independent worker result' in str(child_read[1]), child_read
            assert not child.get('aborted')
            # Stopping the Worker fences its sub-agent through the same live root.
            worker['aborted'] = True
            try:
                with pytest.raises(Exception):
                    admission.validate_task(child)
            finally:
                worker['aborted'] = False
        finally:
            close_unstarted_child_invocation(child, reason='spec complete', cause='child_admission_failed')
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=cfg['_attemptId'], task_id=identity, terminal=True, status='completed',
            projection={'content': 'Verified independent worker bytes'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        return identity, None

    continuations = []
    commands = ConversationTurnCommandService(build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=transport)
    service = ProjectDispatchService(repo, commands=commands)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda repository: service)
    brief = 'Write and verify result.txt.\n' + 'Keep the original authorization and every constraint.\n' * 100
    arguments = {'title': 'Independent implementation', 'brief': brief, 'idempotency_key': 'implementation'}
    first = json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments))
    assert first.get('status') == 'completed', first
    second = json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments))
    assert second == first and len(sends) == 1
    worker = ConversationRepository().get(first['conversation_id'], user_id=owner, include_messages=False)
    assert worker.metadata['settings']['folderId'] == 'native-folder'
    evidence = json.loads(execute_coordinator_tool(task, 'poll', {'ids': [first['assignment_id']], 'wait_s': 1500}))
    assert evidence['wait'] == 'settled' and evidence['workers'][0]['result'] == 'Verified independent worker bytes'
    row = repo.read(first['assignment_id'])
    assert row['request']['brief'] == brief
    assert row['modelAttempts'][0]['reported_tokens'] == 10
    assert usage(repo, repo._load()[0], source_budget['id'])['tokens'] == 20
    assert row['toolAttempts'][0]['phase'] == 'executor_returned'
    assert row['toolAttempts'][1]['phase'] == 'executor_returned'
    assert work_ids == ['project-worker:' + first['assignment_id']]
    delivery.settle_task_workspace(str(root), work_ids[0], user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    view = service.view_for_coordinator(first['assignment_id'], scope={'user_id': owner,
        'project_path': str(root), 'conversation_id': conv, 'binding_revision': binding['revision']})
    assert view['delivery']['status'] == 'blocked' and not view['canonicalDelivery']
    # A completed Worker with a held receipt settles a wait at once and names its cause.
    held = json.loads(execute_coordinator_tool(task, 'poll', {'ids': [first['assignment_id']], 'wait_s': 60}))
    assert held['wait'] == 'settled' and held['workers'][0]['delivery'] == view['delivery'], held
    assert 'delivery.reason' in held['instruction']
    if continue_after_restart:
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=task['_attemptId'], task_id=task['id'], terminal=True, status='completed',
            projection={'content': 'Delegated independent implementation'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        engine.crash_cycle()
        from lib.project_dispatch.recovery import recover_persisted_dispatch
        from lib.project_dispatch.continuation import recover_coordinator
        monkeypatch.setattr('lib.project_dispatch.recovery.ProjectDispatchService', lambda repository: service)
        recover_persisted_dispatch(client=factory())
        assert len(continuations) == 1
        task = continuations[0]
        assert task['_principalContext']['kind'] == 'system'
        assert recover_coordinator(repo, service=service) == []
        page = json.loads(execute_coordinator_tool(task, 'get_project_workers', {}))
        assert page['workers'][0]['assignment_id'] == first['assignment_id'], page
        assert page['request_usage']['id'] == source_budget['id']
        assert page['request_usage']['usage']['tokens'] >= 20
        result = _execute_tool_one(task, {}, 'get_project_workers', 'native-control-read', {}, 0,
            {'name': 'get_project_workers', 'arguments': {}, 'query': 'Follow Worker results'},
            task['config'], str(root), True, task['_testCoordinatorSchemas'])
        assert first['assignment_id'] in str(result[1]) and 'Error:' not in str(result[1]), result
    reviewed = json.loads(execute_coordinator_tool(task, 'review_project_work', {
        'assignment_id': first['assignment_id'], 'result_revision': view['execution']['resultRevision'],
        'decision': 'accept', 'note': 'Worker file bytes and command verification confirmed.',
        'checkpoint_sha': view['delivery']['checkpointSha']}))
    assert reviewed.get('decision') == 'accept', reviewed
    assert reviewed['canonical_delivery'] is False
    delivery.resume_pending_workspace_deliveries()
    # Once publication was requested the Worker settles at its canonical delivery.
    delivered = json.loads(execute_coordinator_tool(task, 'poll', {'ids': [first['assignment_id']], 'wait_s': 0}))
    assert delivered['wait'] == 'settled' and delivered['workers'][0]['canonical_delivery'], delivered
    assert delivered['workers'][0]['publication_requested'], delivered
    assert (root / 'result.txt').read_text() == 'native independent worker result\n'
    report = json.loads(execute_coordinator_tool(task, 'report_to_user', {
        'title': 'Implementation delivered', 'message': 'Worker verification passed and canonical delivery is confirmed.',
        'kind': 'delivery', 'idempotency_key': 'verified'}))
    assert report.get('turnId'), report
    from lib.conversations.coordinator_budget import read_state
    engine.crash_cycle()
    persisted = read_state(owner, str(root))
    assert persisted['request']['id'] == source_budget['id'] and 'policy' not in persisted['request']
    assert persisted['request']['usage']['tokens'] >= 20
    assert CoordinatorReports(owner, str(root)).read()['unread'] is True
    assert json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments)) == first
    assert len(sends) == 1 and not workspaces[0].exists()
    assert (root / 'result.txt').read_text() == 'native independent worker result\n'
    if continue_after_restart:
        receipt = json.loads(execute_coordinator_tool(task, 'finish_project_request', {
            'report_turn_id': report['turnId'], 'outcome': 'complete'}))
        assert receipt.get('reason') == 'complete', receipt
        engine.crash_cycle()
        assert recover_coordinator(repo, service=service) == []
        assert len(continuations) == 1


def _settle_native_prompt_overflow(task, messages, monkeypatch):
    """Fail the summary seam; keep real token admission, activity and terminal writes."""
    from lib.llm.llm_errors import ContextCompactionError
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.compaction import _prompt_admission as prompt
    from lib.tasks_pkg.compaction._layer2 import _compact
    from lib.tasks_pkg.failure_attribution import record_active_round
    from lib.tasks_pkg.orchestrator._post_loop import handle_task_fatal

    def unavailable_summary(*args, **kwargs):
        kwargs['_result_meta'].update(compacted=False, reason='summary failed',
                                      summaryFailureReason='summary_dispatch_failed')

    with monkeypatch.context() as fault:
        # Synthetic narrow route; the prompt itself and its measured size are real.
        fault.setattr(prompt, '_resolved_ceiling', lambda *a, **kw: (128, 128))
        fault.setattr(_compact, 'execute_compact_tool', unavailable_summary)
        # Simulate shutdown after the terminal commit, before the retry drain.
        fault.setattr(manager, 'task_admission_closed', lambda: True)
        record_active_round(task, 0)
        try:
            prompt.enforce_dispatch_prompt_limit(messages, [], task, round_num=0,
                                                model=task['config']['model'])
        except ContextCompactionError as failure:
            assert 'summary_dispatch_failed' in str(failure)
            assert task['_lastPromptAdmission']['totalTokens'] > 128
            handle_task_fatal(task, failure, allow_retry=False)
        else:
            pytest.fail('The oversized Coordinator prompt was admitted')
    assert not task.get('_terminalTurnPending')


@pytest.mark.parametrize('complete_at_commit', [False, True])
def test_native_prompt_overflow_blocks_once_across_two_reopens(
        native_dispatch_authority, tmp_path, monkeypatch, complete_at_commit):
    """A real non-retryable local overflow must settle once without erasing the working view."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversations import coordinator_births, coordinator_workers
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.project_dispatch import admission, continuation, recovery
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.project_dispatch.repository import CONTRACT, ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.storage.model_continuation import ModelContinuationStore, settled_witness
    from lib.tasks_pkg.model_replay import policy_witness
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.integration_control import checkpoint_workspace
    import json
    import time

    factory, owner, engine = native_dispatch_authority
    # The requirements reader imports this facade by value; point each test
    # invocation at its own engine even when the module is already imported.
    from lib.acceptance import turns as acceptance_turns
    monkeypatch.setattr(acceptance_turns, 'get_storage_client', factory)
    root = tmp_path / 'project'
    root.mkdir()
    _git(root, 'init', '-b', 'wake-replay')
    _git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
         'commit', '--allow-empty', '-m', 'baseline')
    project = str(root)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    binding = ProjectCoordinationRepository(owner).create(project, folder_id='native-folder',
        expected_revision=0, command_id='coordinator')
    conv = binding['conversationId']
    scope = {'user_id': owner, 'conversation_id': conv}
    lane = dict(scope, lane_id='main')
    board = CoordinatorTaskProgress(owner, project, notify=lambda: None)
    board_title = 'Verify the restored task board'
    board.publish({'owner': owner, 'projectPath': project, 'coordinatorId': conv,
                   'bindingRevision': binding['revision']},
                  {'summary': 'Verification is underway.', 'tasks': [
                      {'title': board_title, 'status': 'in_progress',
                       'progress': ('Compare the transcript, work receipts, and the retained task board '
                                    'against the acknowledged model history and its recovery checkpoint.')}]})
    board_before = board.read()
    source = turns.create_turn_pair(conv, user_id=owner, command_id='source',
        input_projection={'content': 'Verify the whole result'}, config={})
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id='native-test', scopes=[])
    lead = {'id': 'native-file-overflow-source', 'convId': conv, '_userId': owner,
            '_attended': True, '_turnId': source['turn']['turnId'],
            '_attemptId': source['attempt']['attemptId'], '_principalContext': principal.to_payload(),
            'config': {'projectPath': project, 'codeExecEnabled': True}}
    turns.bind_task(lead['_attemptId'], lead['id'], user_id=owner)
    turns.mark_task_started(lead['_attemptId'], lead['id'], user_id=owner)
    tools = ['read_files', *CONTRACT['continuationTools']]
    capture_worker_policy(lead, lead['config'], [{'function': {'name': name}} for name in tools],
                          'fixture-model', worker_model='fixture-model')
    clock = [time.time()]
    schedules, launches, wakes, worker_checkpoints = [], [], [], {}
    repo = ProjectDispatchRepository(owner, project, clock=lambda: clock[0])
    monkeypatch.setattr(continuation, 'schedule_wake', lambda *a, **k: schedules.append(a[2]))
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})

    def start(recipient, cfg, data, watermark, bind, **kwargs):
        task_id = 'native-file-overflow-' + uuid.uuid4().hex
        bind(task_id)
        turns.mark_task_started(cfg['_attemptId'], task_id, user_id=owner)
        launches.append(recipient)
        principal = admission.principal_for(cfg[admission.KEY])
        task = {'id': task_id, 'convId': recipient, '_userId': owner, 'config': cfg,
                '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
                '_principalContext': principal.to_payload(), '_tenant_id': principal.tenant_id,
                '_attended': False, 'status': 'running', 'messages': [], 'toolRounds': [], 'events': [],
                'abort_event': threading.Event()}
        if recipient == conv:
            wakes.append(task)
        else:
            admission.bind_account(task)
            bind_worker_workspace(task)
            workspace = admission.owned_workspace(task)
            (Path(workspace['workspace_path']) / 'evidence.txt').write_text('Retained Worker evidence\n')
            checkpoint_workspace(project, workspace['task_id'], user_id=owner)
            worker_checkpoints[workspace['task_id']] = repo.get_workspace(project, workspace['task_id'], user_id=owner)
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda message, *a: message,
        was_aborted_after=lambda *a: False, start_task=start)
    service = ProjectDispatchService(repo, commands=commands)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda _: service)

    def settle(task_id, attempt_id, content):
        factory(write=True).command('turn.event.record', dict(user_id=owner, attempt_id=attempt_id,
            task_id=task_id, terminal=True, status='completed', projection={'content': content},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)

    assigned = []
    for title in ('Finished evidence', 'Still-running evidence'):
        receipt = json.loads(execute_coordinator_tool(lead, 'delegate_project_work',
            {'title': title, 'brief': 'Inspect the assigned evidence.', 'idempotency_key': title}))
        assert receipt.get('assignment_id'), receipt
        assigned.append(repo.read(receipt['assignment_id']))
    done, running = assigned
    settle(done['execution']['taskId'], done['execution']['attemptId'], 'Worker finding')
    source_reply = 'Initial Coordinator analysis'
    settle(lead['id'], lead['_attemptId'], source_reply)
    def get_turn(identity):
        return turns.get_turn(conv, identity, user_id=owner)

    replay_policy = {'excludeLast': True}
    continuation_store = ModelContinuationStore(client=factory())
    source_turn = factory().query('turn.get', dict(scope, turn_id=lead['_turnId']))
    assert source_turn['status'] == 'completed'
    proof = continuation_store.read_context(**scope, turn_id=lead['_turnId'], replay=False)['historyProof']
    prefix = [{'role': 'user', 'content': 'Verify the whole result'},
              {'role': 'assistant', 'content': 'Retained working-view checkpoint'}]
    checkpoint = {'historyProof': proof, 'turns': [settled_witness(source_turn)], 'messages': prefix,
                  'policy': policy_witness(replay_policy), 'lastModelActivityAtMs': 1}
    assert continuation_store.put(**lane, projection=checkpoint)
    before = continuation_store.get(**lane)
    assert before['messages'] == prefix
    assert continuation.recover_coordinator(repo, service=service) == ['accepted']
    failed, = wakes
    cfg = dict(replay_policy)
    messages = turns.build_api_messages(conv, failed['_turnId'], cfg, user_id=owner)
    assert cfg['_modelReplayStatus'] == 'restored' and messages[:-1] == prefix
    assert board_title in messages[-1]['content']
    _settle_native_prompt_overflow(failed, messages, monkeypatch)
    failed_turn = get_turn(failed['_turnId'])
    failed_input = get_turn(failed_turn['parentTurnId'])
    assert failed_turn['status'] == 'failed'
    assert failed_turn['settlement']['error']['failureAttribution']['roundNum'] == 1
    assert 'summary_dispatch_failed' in failed_turn['settlement']['error']['raw']
    assert failed_turn['settlement']['error']['retryPolicy']['automatic'] is False
    assert not failed_turn['projection'].get('apiRounds')
    assert any(item['reasonCode'] == 'context_compaction' and item['status'] == 'failed'
               for item in failed_turn['projection']['activityTimeline']['entries'])
    assert failed_input['projection']['content'] == messages[-1]['content']

    def assert_preserved():
        assert CoordinatorTaskProgress(owner, project).read() == board_before
        assert ModelContinuationStore(client=factory()).get(**lane) == before
        assert get_turn(failed['_turnId']) == failed_turn
        assert get_turn(failed_turn['parentTurnId']) == failed_input
        assert len(worker_checkpoints) == 2
        for work_id, checkpoint in worker_checkpoints.items():
            assert checkpoint['checkpoint_sha']
            assert repo.get_workspace(project, work_id, user_id=owner) == checkpoint

    def reopen():
        engine.crash_cycle()
        # Storage is a new process; the host's in-memory wake observations/timer are lost too.
        schedules.clear()
        continuation._LANE_OBSERVATIONS.clear()
        fresh = ProjectDispatchRepository(owner, project, clock=lambda: clock[0])
        assert_preserved()
        outcomes = []

        def recover(owner, project, *, repository):
            outcomes.append(recovery.recover_project_dispatch(owner, project, repository=repository,
                service=ProjectDispatchService(repository, commands=commands), clock=repository.clock))

        assert recovery.recover_persisted_dispatch(client=factory(), repository_factory=lambda *a: fresh,
                                                   recovery=recover) == 1
        return fresh, outcomes[0]

    def blocked_reports():
        return [turn for turn in turns.list_turns(conv, user_id=owner)['turns']
                if turn['kind'] == 'coordinator_report.blocked']

    if complete_at_commit:
        commit = ProjectDispatchRepository._commit_value
        fired = []

        def settle_before_dispatch_commit(self, original, value, version, **kwargs):
            if not fired and value.get('coordinatorSettlements'):
                fired.append(True)
                settle(running['execution']['taskId'], running['execution']['attemptId'], 'New Worker evidence')
                assert self.read(running['id']) == running
            return commit(self, original, value, version, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(ProjectDispatchRepository, '_commit_value', settle_before_dispatch_commit)
            repo, outcome = reopen()
        assert fired and outcome['accepted'] == 0
        assert not blocked_reports() and not repo._load()[0].get('coordinatorSettlements')
        repo, outcome = reopen()
        assert outcome['accepted'] == 1 and not blocked_reports()
        assert launches == [done['request']['recipientId'], running['request']['recipientId'], conv, conv]
        return

    publish = CoordinatorReports.publish

    def lose_report_ack(self, *args, **kwargs):
        publish(self, *args, **kwargs)
        raise ConnectionError('lost acknowledgement after writing the blocked report')

    # First reopen commits a stop and publishes the ordinary report, but the
    # dispatch book does not see that publication's acknowledgement.
    with monkeypatch.context() as fault:
        fault.setattr(CoordinatorReports, 'publish', lose_report_ack)
        repo, outcome = reopen()
    assert outcome['accepted'] == 0 and not schedules
    [pending_stop] = repo._load()[0]['coordinatorSettlements']
    assert pending_stop['sourceAttemptId'] == lead['_attemptId']
    assert pending_stop['stopReport'] == 'automatic_retry_blocked' and 'reportTurnId' not in pending_stop
    [report] = blocked_reports()
    assert 'unsafe to retry automatically' in report['projection']['content']

    # Second reopen reconciles that exact Turn, with no additional wake or report.
    repo, outcome = reopen()
    assert outcome['accepted'] == 0 and not schedules
    assert repo._load()[0]['coordinatorSettlements'] == [{**pending_stop, 'reportTurnId': report['turnId']}]
    assert blocked_reports() == [report]
    assert continuation.recover_coordinator(repo, service=ProjectDispatchService(repo, commands=commands)) == []
    assert launches == [done['request']['recipientId'], running['request']['recipientId'], conv]
    assert repo.read(running['id']) == running
    assert turns.get_attempt(running['execution']['attemptId'], user_id=owner)['status'] == 'running'
    assert_preserved()
