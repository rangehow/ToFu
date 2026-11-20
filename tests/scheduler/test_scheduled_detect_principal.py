"""Detect authority from authenticated Turn admission through the scheduler handler."""
from concurrent.futures import Future
import os
from uuid import uuid4

import pytest

from lib.api_keys._context import AuthContext
from lib.identity import PrincipalContext, principal_from_auth_context
from lib.model_routing import probe_automation
from lib.model_routing.repository import ModelRoutingRepository
from lib.scheduler import manager as scheduler
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit
pytest_plugins = ('tests.support.postgres_cluster',
                  'tests.support.postgres_worker_clock')
TENANT = 'detect-scratch-tenant'
ACTION = 'schedule_manage.run.provider_probe_daily'


@pytest.fixture(params=['sqlite', 'postgres', 'native'])
def detect_authority(request, tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service

    backend = 'tofudb' if request.param == 'native' else request.param
    if backend == 'tofudb':
        if not os.environ.get('TOFU_TEST_TOFUDB_BIN'):
            pytest.skip('requires an explicit source-matched native daemon')
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
        monkeypatch.setenv('TOFUDB_OWNER_ID', '23')
        monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    elif backend == 'postgres':
        from tests.support.postgres_worker_clock import install_clock_function
        dsn = request.getfixturevalue('postgres_cluster')
        install_clock_function(dsn, request.getfixturevalue('postgres_worker_clock_library'))
        dsn_file = tmp_path / 'private-dsn'
        dsn_file.write_text(dsn)
        dsn_file.chmod(0o600)
        monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn_file))
        monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
        monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '2')
    supervisor = StorageSupervisor(project_root=tmp_path, backend=backend, connection_file='')
    runtime = StorageRuntime(supervisor, auto_restart=False)
    monkeypatch.setattr(service, '_runtime', runtime)
    try:
        runtime.start()
        assert runtime.status()['backend'] == backend
        expected = os.environ.get('TOFU_REMOTE_BINARY_SHA256')
        if backend == 'tofudb' and expected:
            import hashlib
            assert supervisor.owns_engine_process and supervisor._process is not None
            with open(f'/proc/{supervisor._process.pid}/exe', 'rb') as running_binary:
                assert hashlib.file_digest(running_binary, 'sha256').hexdigest() == expected
        yield runtime
    finally:
        runtime.stop()


@pytest.fixture
def detect_boundary(detect_authority, monkeypatch, request):
    from lib.storage import get_storage_client

    client = get_storage_client(write=True)
    owner = 23
    document = _full_document()
    document['provider_accesses'][1]['enabled'] = False
    tenant = getattr(request, 'param', TENANT)
    boundary = {'owner_user_id': owner, 'tenant_id': tenant or ''}
    client.command('model_routing.commit', {
        **boundary, 'expected_revision': 0, 'updated_at': 1,
        'document': dict(document, revision=1),
    }, 'detect-fixture-authority')
    manager = scheduler.ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='scratch-scheduler', owner_user_id=owner,
        tenant_id=tenant, scopes={'scheduler:run'})
    row = manager._ensure_default_provider_probe_task()
    reads, queued = [], []
    real_client = ModelRoutingRepository._client

    class ObservedClient:
        def __init__(self, delegate):
            self.delegate = delegate

        def query(self, operation, payload):
            if operation == 'model_routing.get':
                reads.append(payload)
            return self.delegate.query(operation, payload)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    monkeypatch.setattr(ModelRoutingRepository, '_client',
                        lambda self, **kw: ObservedClient(real_client(self, **kw)))
    monkeypatch.setattr('lib.scheduler.executor.get_scheduler', lambda: manager)
    monkeypatch.setattr(probe_automation, 'matrix_probe_plan', lambda *args: {
        'provider': {'base_url': 'https://example.test', 'protocol': 'openai'},
        'models': [], 'api_keys': [], 'extra_headers': {}, 'oauth': '', 'adapter': ''})
    monkeypatch.setattr(probe_automation.probe, 'build_probe_work', lambda *a, **kw: [('alpha',)])
    monkeypatch.setattr(probe_automation.probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(probe_automation.probe, 'public_probe_snapshot', lambda row: row)

    def submit(row, work, timeout):
        queued.append(tuple(work))
        row.update(status='done', done_count=1, cells={
            'alpha': {'key_idx': 0, 'root_model_id': 'alpha',
                      'model_id': 'alpha-wire-a', 'status': 'rate_limited'}})
        result = Future()
        result.set_result(None)
        return result

    monkeypatch.setattr(probe_automation.probe, 'submit_provider_probe_task', submit)
    return client, manager, row, boundary, reads, queued


def _accept(monkeypatch, client, owner, principal):
    from lib.conversation_sync import command_service
    from lib.conversation_sync.execution_identity import EXECUTION_IDENTITY_CONFIG_KEY as KEY

    conv_id = uuid4().hex
    client.command('conversation.create', {
        'conv_id': conv_id, 'user_id': owner, 'title': 'Detect',
        'created_at': 1, 'updated_at': 1, 'settings': {},
    }, f'detect-conversation:{conv_id}')
    service = command_service.ConversationTurnCommandService(
        build_user_message=lambda message, *_: message,
        was_aborted_after=lambda *_: False,
        start_task=lambda *_a, **_kw: pytest.fail('unexpected command-side execution'))
    # Pause delivery, not command admission: the real accepted config is read
    # back through the owner-only storage port, never from the request.
    monkeypatch.setattr(command_service, '_bind_accepted_dispatch', lambda *a, **kw: None)
    accepted = []
    monkeypatch.setattr(service, '_start_accepted_attempt',
                        lambda result, *_a, **_kw: accepted.append(result))
    service.create_turn(conv_id, owner, {
        'commandId': f'detect-turn:{conv_id}', 'inputTurn': {'content': 'Run Detect'},
        'config': {'model': 'offline', KEY: {'capabilities': [ACTION]},
                   '_turnPrincipalScopes': ['admin']},
    }, request_started_at=1, principal=principal)
    result, = accepted
    assert KEY not in result['attempt'].get('config', {})
    return result


def _accept_and_restore(monkeypatch, request, client, owner, principal):
    from lib.conversation_sync import command_service, task_start
    from lib.tasks_pkg import manager
    from lib.turn_lifecycle import bind_task, mark_task_started

    result = _accept(monkeypatch, client, owner, principal)
    conv_id = result['turn']['conversationId']
    config = command_service.restore_accepted_attempt_config(
        result['turn'], result['attempt'], user_id=owner)
    tasks = []
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(task_start, '_prepare_unbound_task_dispatch',
                        lambda task, conv, cfg, owner, after: (cfg, None, False))
    monkeypatch.setattr('lib.project_dispatch.request_budget.bind_source_account', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', tasks.append)
    def bind(task_id):
        assert bind_task(config['_attemptId'], task_id, user_id=owner)
        assert mark_task_started(config['_attemptId'], task_id, user_id=owner)

    task_id, reason = task_start.start_conversation_attempt_executor(
        conv_id, config, on_task_registered=bind)
    assert reason is None
    task, = tasks
    assert task_id == task['id']
    assert task['_principalContext']['scopes'] == []
    task['_suppressEvents'] = True
    request.addfinalizer(lambda: manager.discard_task(task_id))
    return task


def test_admitted_detect_attempt_dispatches_with_original_principal(monkeypatch, detect_boundary):
    from lib.conversation_sync.worker_dispatch import dispatch_accepted_attempt
    from lib.turn_lifecycle import get_attempt

    client, _, _, scope, _, queued = detect_boundary
    owner = scope['owner_user_id']
    principal = principal_from_auth_context(AuthContext(
        key_id='fixture-owner', owner_user_id=owner, tenant_id=TENANT,
        scopes=frozenset({'conversations', 'agents:scheduler'})), allow_personal_owner=False)
    result = _accept(monkeypatch, client, owner, principal)
    attempt_id = result['attempt']['attemptId']
    dispatch_accepted_attempt(attempt_id, user_id=owner)
    assert get_attempt(attempt_id, user_id=owner)['taskId'] == f'conversation-attempt:{attempt_id}'
    assert queued == []


@pytest.mark.parametrize('boundary', ['command-read', 'command-copied-config', 'executor-restore'])
def test_detect_restore_refuses_mismatched_accepted_attempt(monkeypatch, detect_boundary, boundary):
    from lib.conversation_sync import command_service, task_start
    from lib.turn_lifecycle import get_attempt

    client, _, _, scope, _, queued = detect_boundary
    owner = scope['owner_user_id']
    principal = principal_from_auth_context(AuthContext(
        key_id='fixture-owner', owner_user_id=owner, tenant_id=TENANT,
        scopes=frozenset({'conversations', 'agents:scheduler'})), allow_personal_owner=False)
    original = _accept(monkeypatch, client, owner, principal)
    other = _accept(monkeypatch, client, owner, principal)
    if boundary == 'command-read':
        with pytest.raises(PermissionError):
            command_service.restore_accepted_attempt_config(other['turn'], original['attempt'], user_id=owner)
    elif boundary == 'command-copied-config':
        config = get_attempt(original['attempt']['attemptId'], user_id=owner,
                             include_execution_config=True)['config']
        with pytest.raises(PermissionError):
            command_service.restore_accepted_attempt_config(other['turn'], other['attempt'],
                                                            user_id=owner, accepted_config=config)
    else:
        config = command_service.restore_accepted_attempt_config(original['turn'], original['attempt'], user_id=owner)
        config['_attemptId'] = other['attempt']['attemptId']
        monkeypatch.setattr(task_start, 'create_task', lambda *a, **kw: pytest.fail('registered mismatched attempt'))
        with pytest.raises(PermissionError):
            task_start.start_conversation_attempt_executor(original['turn']['conversationId'], config)
    assert queued == []


@pytest.mark.parametrize('case', [
    'owner', 'scoped-owner', 'underscoped', 'wrong-tenant', 'wrong-owner',
    'wrong-kind', 'absent', 'raw-scope', 'copied-task', 'copied-turn', 'copied-attempt',
    'stale-attempt', 'project-role', 'manager',
    'scheduled', 'scheduled-wrong-owner', 'scheduled-unbound',
])
def test_detect_dispatch_uses_only_its_bound_owner_and_tenant(
        monkeypatch, request, detect_boundary, case):
    from lib.tasks_pkg.handlers.misc import _agents

    client, manager, row, boundary, reads, queued = detect_boundary
    owner = boundary['owner_user_id']
    allowed = case in {'owner', 'scoped-owner', 'scheduled'}
    if case.startswith('scheduled'):
        if case == 'scheduled-wrong-owner':
            manager._process_principal = PrincipalContext.system(
                subject_id='foreign-scheduler', owner_user_id=owner + 1,
                tenant_id=TENANT, scopes={'scheduler:run'})
        elif case == 'scheduled-unbound':
            manager._process_principal = None
        manager._run_claimed_task(row)
    elif case == 'manager':
        assert manager.run_task_now(row['id'], user_id=owner) == (None, 'Task not found')
    else:
        scopes = {'conversations'}
        if case in {'owner', 'wrong-tenant', 'wrong-owner', 'copied-task', 'copied-turn',
                    'copied-attempt', 'stale-attempt', 'project-role'}:
            scopes.add('admin')
        if case == 'scoped-owner':
            scopes.add('agents:scheduler')
        caller_owner = owner + 1 if case == 'wrong-owner' else owner
        auth = AuthContext(key_id='fixture-owner', owner_user_id=caller_owner,
                           tenant_id='foreign-tenant' if case == 'wrong-tenant' else TENANT,
                           scopes=frozenset(scopes))
        principal = principal_from_auth_context(auth, allow_personal_owner=False)
        if case in {'absent', 'wrong-kind', 'raw-scope'}:
            task = {'id': 'detached-call', '_userId': owner, 'convId': 'control',
                    '_suppressEvents': True, 'config': {}}
            if case != 'absent':
                build = PrincipalContext.system if case == 'wrong-kind' else PrincipalContext.user
                task['_principalContext'] = build(subject_id='detached', owner_user_id=owner,
                    tenant_id=TENANT, scopes={'agents:scheduler'}).to_payload()
        elif case == 'wrong-owner' and getattr(client, '_owner_id', None) == owner:
            # A native process is owner-bound; the foreign command must refuse
            # before it can construct an executor or access scheduler state.
            from lib.storage import StorageError
            with pytest.raises(StorageError) as refused:
                _accept(monkeypatch, client, caller_owner, principal)
            assert refused.value.code == 'database_forbidden'
            assert reads == queued == []
            return
        else:
            task = _accept_and_restore(monkeypatch, request, client, caller_owner, principal)
        if case == 'copied-task':
            task = dict(task, id='different-invocation')
        if case == 'copied-turn':
            task = dict(task, _turnId=uuid4().hex)
        if case == 'copied-attempt':
            task = dict(task, _attemptId=uuid4().hex)
        if case == 'stale-attempt':
            from lib.turn_lifecycle import get_attempt, record_task_event
            record_task_event(dict(task, status='aborted', aborted=True), {'type': 'aborted'})
            assert get_attempt(task['_attemptId'], user_id=owner)['status'] == 'interrupted'
        if case == 'project-role':
            monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope', lambda task: {
                'project_path': '/fixture', 'conversation_id': task['convId'], 'binding_revision': 1})
            monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_tool_ceiling',
                                lambda *a: ())
        # Registration may consult the ordinary model catalog; measure only
        # authority reads attributable to the scheduler tool invocation.
        reads.clear()
        assert queued == []
        round_entry = {'roundNum': 1, 'toolCallId': 'detect-now',
                       'toolName': 'schedule_manage', 'query': 'Run Detect'}
        _, text, _ = _agents._handle_scheduler_tool(
            task, {}, 'schedule_manage', 'detect-now',
            {'action': 'run', 'task_id': row['id'], '_tenant_id': TENANT,
             '_manualDetectAdmission': {'capabilities': [ACTION]},
             '_principalContext': principal.to_payload()},
            1, round_entry, {}, '', False)
        assert ('[OK] Task executed' in str(text)) == allowed, text
        if not allowed and case != 'project-role':
            assert 'not found' in str(text), text

    assert reads == ([boundary] if allowed else [])
    assert queued == ([(('alpha',),)] if allowed else [])


@pytest.mark.parametrize('detect_boundary', [None], indirect=True)
@pytest.mark.parametrize('case', ['ok', 'copied', 'wrong-tenant', 'stale', 'settled',
                                 'cancelled', 'unavailable', 'lost-reply', 'receiver-cancelled',
                                 'receiver-wrong-tenant', 'receiver-copied', 'receiver-stale-claim', 'entered-stop', 'shard-drain'])
def test_shard_manual_detect_crosses_primary_command_control(monkeypatch, detect_boundary, case):
    import asyncio
    import threading
    from types import SimpleNamespace
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import ManualDetectAdmission, read_durable_detect
    from lib.process_owner import process_owner
    from lib.scheduler import manual_detect_control
    from lib.tasks_pkg import command_control_runtime as control, command_control_transport as transport
    from lib.tasks_pkg.command_control_transport import CommandControlServer
    from lib.tasks_pkg.handlers.misc import _agents
    from serverctl_pkg import execution_host, execution_lifespan
    from serverctl_pkg.execution_host import _with_source_diagnostic
    from serverctl_pkg.execution_service import ExecutionService

    client, manager, row, scope, reads, probes = detect_boundary
    owner = scope['owner_user_id']
    tenant = 'foreign' if case == 'wrong-tenant' else None
    principal = principal_from_auth_context(AuthContext(
        key_id='shard-fixture', owner_user_id=owner, tenant_id=tenant,
        scopes=frozenset({'conversations', 'agents:scheduler'})), allow_personal_owner=False)
    accepted = _accept(monkeypatch, client, owner, principal)
    conversation, turn, attempt = (accepted['turn']['conversationId'], accepted['turn']['turnId'],
                                   accepted['attempt']['attemptId'])
    turns.dispatch_attempt_to_worker(principal, attempt)
    task_id = f'conversation-attempt:{attempt}'
    worker_id = uuid4().hex
    claim = client.command('worker_job.claim_next', {
        'worker_id': worker_id, 'task_kinds': ['conversation-attempt'], 'now_ms': 1,
        'lease_ms': 300_000}, worker_id)
    assert claim['taskId'] == task_id
    assert turns.mark_task_started(attempt, task_id, user_id=owner, worker_process=process_owner(),
        worker_claim={'worker_id': worker_id, 'fencing_token': claim['fencingToken']})['status'] == 'running'
    principal = read_durable_detect(owner, conversation, turn, attempt, running_task_id=task_id)
    carrier = dict(id=task_id, convId=conversation, _turnId=turn, _attemptId=attempt,
                   _userId=owner, _principalContext=principal.to_payload(), _attended=True,
                   _suppressEvents=True, config={}, status='running',
                   _manualDetectAdmission=ManualDetectAdmission(task_id, principal, conversation, turn, attempt))

    def cancel():
        assert client.command('worker_job.request_cancel', {'task_id': task_id, 'user_id': owner,
            'now_ms': 1, 'reason': 'fixture Stop'}, 'stop:' + attempt)['accepted']

    from lib.agent_core.abort_signal import AbortSignal
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    shutdown = AbortSignal()
    worker_stop = threading.Event()
    carrier['_manualDetectHostShutdown'] = shutdown
    carrier['_conversationWorkerContext'] = WorkerExecutionContext(
        WorkerJobClaim.from_document(claim, expected_worker_id=worker_id), shutdown_event=worker_stop)
    joined = []
    relay = []
    stop_shard = []
    probe_joining, release_probe = threading.Event(), threading.Event()
    if case in ('entered-stop', 'shard-drain'):
        def submit(probe_task, work, _timeout):
            probes.append(tuple(work))
            if case == 'entered-stop':
                cancel()
            else:
                stop_shard[0]()
                assert relay[0].wait(5), 'primary did not observe caller drain'

            class EnteredProbe(Future):
                def result(self, timeout=None):
                    assert timeout is None and probe_task['_abort']
                    if case == 'shard-drain':
                        probe_joining.set()
                        assert release_probe.wait(5)
                    joined.append(True)
                    return None

            return EnteredProbe()

        monkeypatch.setattr(probe_automation.probe, 'submit_provider_probe_task', submit)
        monkeypatch.setattr(ModelRoutingRepository, 'compare_and_swap',
                            lambda *_a, **_kw: pytest.fail('Cancellation must prevent publication'))

    if case == 'copied':
        carrier['id'] = 'copy'
    elif case == 'stale':
        forged = uuid4().hex
        carrier.update(_attemptId=forged,
            _manualDetectAdmission=ManualDetectAdmission(task_id, principal, conversation, turn, forged))
    elif case == 'settled':
        turns.record_task_event(dict(carrier, status='aborted', aborted=True), {'type': 'aborted'})
        assert turns.get_attempt(attempt, user_id=owner)['status'] == 'interrupted'
    elif case == 'cancelled':
        cancel()

    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'execution_shard')
    monkeypatch.setenv('TOFU_PROJECT_PATH', '.')
    monkeypatch.setattr(manual_detect_control, 'PERSONAL_USER_ID', owner)
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='personal'))
    # Emulate separate process scheduler composition without replacing the
    # real scheduler row lookup or the primary's execution implementation.
    class ShardScheduler:
        def get_task(self, *args, **kwargs):
            return manager.get_task(*args, **kwargs)

        def run_task_now(self, *args, **kwargs):
            return None, 'Task not found'

    monkeypatch.setattr('lib.scheduler.executor.get_scheduler', ShardScheduler)
    monkeypatch.setattr('lib.scheduler.manager.get_scheduler', lambda: manager)
    requests = []
    real_call = control.call

    def observe(*args, **kwargs):
        requests.append((args[1], kwargs['params']))
        if case == 'receiver-cancelled':
            cancel()
        elif case == 'receiver-wrong-tenant':
            manager._process_principal = PrincipalContext.system(
                subject_id='wrong-tenant', owner_user_id=owner, tenant_id='other', scopes={'scheduler:run'})
        elif case == 'receiver-stale-claim':
            query = type(client).query

            def stale_state(self, operation, *args, **params):
                if operation == 'worker_job.claim_state':
                    return {'ok': False, 'error': 'stale_fence'}
                return query(self, operation, *args, **params)

            monkeypatch.setattr(type(client), 'query', stale_state)
        elif case == 'receiver-copied':
            kwargs['params'] = {**kwargs['params'], 'turn_id': uuid4().hex}
        return real_call(*args, **kwargs)

    monkeypatch.setattr(control, 'call', observe)
    if case == 'lost-reply':
        encode = transport._encode

        def drop_reply(value):
            result = value.get('result')
            if isinstance(result, dict) and result.get('success') is True:
                raise OSError('fixture dropped the settled response')
            return encode(value)

        monkeypatch.setattr(transport, '_encode', drop_reply)

    async def journey():
        def receive(*args, **kwargs):
            relay.append(kwargs['relay_closed'])
            return manual_detect_control.apply_manual_detect(*args, **kwargs)

        handler = _with_source_diagnostic(SimpleNamespace(extensions={}),
            lambda *_a, **_kw: pytest.fail('Detect must not fan out'),
            manual_detect=receive)
        server = CommandControlServer('primary', handler)
        endpoint = await server.start()
        monkeypatch.setattr(ExecutionService, 'record', lambda _: {} if case == 'unavailable' else {
            'state': 'ready', 'serviceId': 'primary', 'commandControl': endpoint})
        monkeypatch.setattr(ExecutionService, 'host_matches', lambda *_: True)
        def tool():
            return _agents._handle_scheduler_tool(carrier, {}, 'schedule_manage', 'detect-shard',
                {'action': 'run', 'task_id': row['id']}, 1,
                {'roundNum': 1, 'toolCallId': 'detect-shard', 'toolName': 'schedule_manage',
                 'query': 'Run Detect'}, {}, '', False)

        try:
            if case != 'shard-drain':
                return await asyncio.to_thread(tool)
            # Exercise the actual owner-FD stop entry and pause teardown. The
            # later worker-service stop must not provide this withdrawal.
            reader, writer = os.pipe()
            stop_shard.append(lambda: os.close(writer))
            tool_results = []
            monkeypatch.setattr(execution_host, 'write_record', lambda *_: None)
            monkeypatch.setattr(execution_host, 'process_matches', lambda *_: True)
            monkeypatch.setattr(execution_host, '_claim_gate', lambda *_: None)

            async def lifespan(app, stopped, ready, *, before_shutdown):
                request = asyncio.create_task(asyncio.to_thread(tool))
                try:
                    await asyncio.wait_for(stopped.wait(), 5)
                    assert shutdown.is_set() and not worker_stop.is_set()
                    assert await asyncio.to_thread(probe_joining.wait, 5)
                    assert not request.done()
                finally:
                    release_probe.set()
                tool_results.append(await request)

            monkeypatch.setattr(execution_lifespan, 'run_execution_lifespan', lifespan)
            args = SimpleNamespace(manager_fd=reader, manager_pid=os.getpid(), manager_start=1,
                                   state_file='unused', report_fd=1)
            try:
                await execution_host._run_application(args, {'plan': {}}, asyncio.Event(), shutdown,
                    lambda stop, plan: SimpleNamespace(extensions={'tofu_shutdown_requested': stop}))
            finally:
                os.close(reader)
            return tool_results[0]
        finally:
            await server.close()

    _, text, _ = asyncio.run(journey())
    assert ('[OK] Task executed' in str(text)) == (case == 'ok'), text
    if case in ('lost-reply', 'unavailable', 'entered-stop', 'shard-drain'):
        assert 'not replayed' in str(text)
    expected = case in ('ok', 'lost-reply', 'entered-stop', 'shard-drain') or case.startswith('receiver-')
    assert len(requests) == int(expected)
    if requests:
        assert requests == [('manual_detect', dict(conversation_id=conversation, turn_id=turn,
                                     attempt_id=attempt, schedule_id=row['id']))]
    assert joined == ([True] if case in ('entered-stop', 'shard-drain') else [])
    entered = case in ('ok', 'lost-reply', 'entered-stop', 'shard-drain')
    assert reads == ([scope] if entered else [])
    if case in ('entered-stop', 'shard-drain'):
        assert manager.get_execution_log(user_id=owner) == []
    if case == 'shard-drain':
        state = client.query('worker_job.claim_state', {'task_id': task_id, 'worker_id': worker_id,
            'fencing_token': claim['fencingToken'], 'now_ms': 1})
        assert state['ok'] and not state['cancelRequestedAtMs']
        assert turns.get_attempt(attempt, user_id=owner)['status'] == 'running'
    assert probes == ([(('alpha',),)] if entered else [])


@pytest.mark.parametrize('role', ['execution', 'execution_shard'])
def test_ordinary_schedule_run_does_not_use_detect_relay(monkeypatch, role):
    from lib.scheduler.executor import _run_owner_task_now

    class Scheduler:
        def get_task(self, task_id, *, user_id):
            assert (task_id, user_id) == ('ordinary', 23)
            return {'task_type': 'prompt'}

        def run_task_now(self, task_id, *, user_id, principal, manual_detect_admission):
            assert (task_id, user_id, principal, manual_detect_admission) == ('ordinary', 23, None, grant)
            return True, 'local output'

    grant = object()
    monkeypatch.setenv('TOFU_PROCESS_ROLE', role)
    monkeypatch.setattr('lib.scheduler.manual_detect_control.run_on_primary',
                        lambda *_: pytest.fail('ordinary row must never relay'))
    assert _run_owner_task_now(Scheduler(), 'ordinary', 23, None, grant) == (True, 'local output')
