"""Public timeout is execution admission, never evidence of task sufficiency."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time

import pytest

from tests.tasks_pkg.test_public_agent_runtime import _access, _patch_route_slot

pytestmark = pytest.mark.unit


@pytest.fixture
def started(monkeypatch):
    from lib.tasks_pkg import manager, spawn
    import tofu_agent.runtime as runtime_module
    from tofu_agent import AgentRuntime

    _patch_route_slot(monkeypatch)
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(time=lambda: 1000.0, monotonic=time.monotonic))
    tasks = []

    def create(_conversation, _messages, config, **kwargs):
        task = {'id': f'deadline-{len(tasks)}', 'config': config,
                '_userId': kwargs['principal'].owner_user_id,
                'status': 'pending', 'events': [], 'toolRounds': [],
                'abort_event': threading.Event()}
        tasks.append(task)
        return task

    def submit(task, *, executor):
        future = Future()
        task['status'] = 'done'
        future.set_result(None)
        return future

    monkeypatch.setattr(manager, 'create_task', create)
    monkeypatch.setattr(spawn, 'spawn_task', submit)
    runtime = AgentRuntime.local(model_routing=_access())
    yield runtime, tasks
    runtime.close(abort=False)


@pytest.mark.parametrize('timeout,caller,expected', [
    (60, None, 1060), (60, 1030, 1030), (60, 2000, 1060),
    (60, 1060, 1060), (60, 0, 0),
    (None, 1030, 1030), (None, None, None),
])
def test_public_timeout_reaches_canonical_admission(started, timeout, caller, expected):
    runtime, tasks = started
    original = {'max_tokens': 123, 'max_tool_calls': 17, 'deadline': caller}
    runtime.start([{'role': 'user', 'content': 'work'}],
                  config={'executionLimits': original}, timeout_s=timeout)
    limits = tasks[0]['config']['executionLimits']
    assert limits == {**original, 'deadline': expected}
    assert original['deadline'] == caller
    from lib.agent_core.execution_limits import execution_limit_reason
    import lib.agent_core.execution_limits as owner
    if expected is not None:
        previous = owner.time
        try:
            owner.time = SimpleNamespace(time=lambda: expected)
            assert execution_limit_reason(tasks[0]) == 'execution_deadline_exceeded'
        finally:
            owner.time = previous


def test_timeout_is_stamped_before_setup_without_mutating_config(started, monkeypatch):
    import copy
    import tofu_agent.runtime as runtime_module
    runtime, tasks = started
    clock = [1000.0]
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    build = runtime_module.build_agent_config

    def slow_setup(*args, **kwargs):
        clock[0] = 1045.0
        return build(*args, **kwargs)

    monkeypatch.setattr(runtime_module, 'build_agent_config', slow_setup)
    config = {'executionLimits': {'max_tokens': 123, 'max_tool_calls': 17,
                                  'deadline': 2000}, 'tools': []}
    original = copy.deepcopy(config)
    runtime.start([{'role': 'user', 'content': 'work'}], config=config, timeout_s=60)
    assert tasks[0]['config']['executionLimits'] == {
        'max_tokens': 123, 'max_tool_calls': 17, 'deadline': 1060.0}
    assert config == original


def test_unbounded_run_does_not_invent_an_account(started):
    runtime, tasks = started
    runtime.start([{'role': 'user', 'content': 'work'}], timeout_s=None)
    assert 'executionLimits' not in tasks[0]['config']
    assert '_executionBudget' not in tasks[0]


def test_partial_artifact_survives_deadline_and_no_later_tool_is_admitted(started, monkeypatch, tmp_path):
    runtime, tasks = started
    runtime.start([{'role': 'user', 'content': 'work'}], timeout_s=60)
    task = tasks[0]
    assert task['config']['executionLimits'] == {
        'max_tokens': None, 'max_tool_calls': None, 'deadline': 1060.0}
    task['status'] = 'running'
    from lib.agent_core import execution_limits
    monkeypatch.setattr(execution_limits, 'time', SimpleNamespace(time=lambda: 1060.0))
    artifact = tmp_path / 'answer.txt'
    artifact.write_text('useful partial result')
    assert execution_limits.admit_tool_execution(task) == 'execution_deadline_exceeded'
    assert task['abort_event'].is_set()
    assert artifact.read_text() == 'useful partial result'
    assert not task.get('_executionToolsAdmitted')


@pytest.mark.parametrize('fake_check', ['true', 'cat answer.txt', 'printf PASS'])
def test_command_exit_zero_never_stops_following_required_work(fake_check):
    from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
    calls = []

    def dispatch(round_num, tools):
        if round_num < 2:
            return {'tool_calls': [{'id': str(round_num), 'function': {
                'name': 'run_command' if round_num == 0 else 'read_files',
                'arguments': '{}'}}]}, 'tool_calls', {}
        return {'content': 'done'}, 'stop', {}

    def execute(round_num, call):
        calls.append(call['function']['name'])
        return f'{fake_check}\n[exit code: 0]' if round_num == 0 else 'required evidence'

    result = run_agent_loop(abort=AbortSignal.never(), round_tools=[],
                            dispatch=dispatch, execute_tool=execute)
    assert calls == ['run_command', 'read_files']
    assert result.rounds == 3


def test_cancellation_wins_before_a_model_or_finalize_call():
    from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
    abort = threading.Event()
    abort.set()
    def dispatch(*args):
        pytest.fail('cancelled work cannot dispatch even a final answer request')
    outcome = run_agent_loop(abort=AbortSignal.from_event(abort),
                             round_tools=[], dispatch=dispatch)
    assert outcome.aborted and outcome.rounds == 0


@pytest.mark.api
def test_real_root_loop_does_not_infer_sufficiency_from_fake_check(monkeypatch, tmp_path):
    import json
    from tests.tasks_pkg.test_request_prefix_stability import _provider
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    requests = []
    calls = [
        ('write_file', {'path': 'answer.txt', 'content': 'partial answer'}),
        ('run_command', {'command': 'printf PASS'}),
        ('read_files', {'path': 'required.txt'}),
    ]
    (tmp_path / 'required.txt').write_text('the remaining requirement')

    def frames(index):
        if index < len(calls):
            name, args = calls[index]
            delta = {'tool_calls': [{'index': 0, 'id': f'deadline-call-{index}',
                'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
            finish = 'tool_calls'
        else:
            delta, finish = {'content': 'Partial answer saved; task not verified.'}, 'stop'
        return ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in [
            {'choices': [{'index': 0, 'delta': delta}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
             'usage': {'prompt_tokens': 100, 'completion_tokens': 10}},
        ]) + 'data: [DONE]\n\n'

    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    server = _provider(requests, frames)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only', model_id='deadline-test')
    for row in (*envelope['model_routing']['models'], *envelope['model_routing']['offerings']):
        row['capabilities'].append('tools')
    try:
        with AgentRuntime.local(model_routing=ModelRoutingConfig.from_mapping(envelope),
                                owner_user_id=7) as runtime:
            result = runtime.run([{'role': 'user', 'content': 'Save an answer; read required.txt.'}],
                config={'tools': '*', 'projectPath': str(tmp_path)}, timeout_s=600)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    assert result.status == 'done', result.error
    assert len(requests) == 4
    assert (tmp_path / 'answer.txt').read_text() == 'partial answer'
    last = json.dumps(requests[-1]['messages'])
    assert 'the remaining requirement' in last
    assert 'sufficiency checkpoint' not in last
