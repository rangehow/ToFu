"""Desktop poll liveness, replay safety and bounded transport contracts."""
import threading

import pytest
import requests

from lib.desktop_agent import _run
from lib.desktop_agent._runtime import Outbox, RuntimeState, retry_delay, validate_poll_response


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('body', [None, {}, {'commands': {}}, {'commands': [None]},
                                       {'commands': [{'id': '', 'type': 'x'}]}])
def test_invalid_poll_cannot_ack(body):
    with pytest.raises(ValueError):
        validate_poll_response(body)


def test_admission_never_replays_and_is_bounded():
    state = RuntimeState(threading.Event())
    for i in range(state.MAX_RUNNING):
        assert state.admit(str(i))
    assert not state.admit('busy')
    assert len(state.running) == state.MAX_RUNNING
    state.finish('0')
    assert not state.admit('0')
    assert not state.admit('busy')
    state.MAX_IDS = len(state.seen)
    assert not state.admit('new')
    assert len(state.seen) == state.MAX_IDS
    assert state.results.items[-1]['error'].startswith('Execution ID ledger full')


def test_outbox_backpressure_wakes_on_ack_without_drop():
    lock = threading.Condition(threading.RLock())
    box = Outbox(lock, threading.Event(), 1, 100)
    box.append({'n': 1})
    done = threading.Event()
    thread = threading.Thread(target=lambda: (box.append({'n': 2}), done.set()))
    thread.start()
    assert not done.wait(.03)
    with lock:
        assert list(box) == [{'n': 1}]
        del box[:1]
    assert done.wait(1)
    thread.join()
    assert list(box) == [{'n': 2}]
    with pytest.raises(ValueError):
        box.append({'data': 'x' * 101})


def test_poll_keeps_heartbeating_and_retains_results_on_bad_200(monkeypatch):
    stop = threading.Event()
    release = threading.Event()
    started = threading.Event()
    executions = []
    sent = []
    command = {'id': 'one', 'type': 'slow', 'params': {}}
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'retry_delay', lambda *args: .001)

    def dispatch(*args):
        executions.append(args)
        started.set()
        assert release.wait(2)
        return {'value': 1}

    class Response:
        status_code = 200
        def __init__(self, body):
            self.body = body
        def json(self):
            return self.body

    def post(url, **kwargs):
        payload = kwargs['json']
        sent.append(payload)
        if len(sent) == 1:
            return Response({'commands': [command]})
        assert started.wait(1)
        if len(sent) == 2:
            release.set()
        if payload['results']:
            result_polls = [p for p in sent if p['results']]
            if len(result_polls) == 1:
                return Response({'landing': 'not Tofu'})
            assert result_polls[0]['results'] == payload['results']
            stop.set()
        if len(sent) > 30:
            stop.set()
            pytest.fail('No terminal result delivered')
        return Response({'commands': [command]})

    monkeypatch.setattr(_run, 'dispatch_command', dispatch)
    monkeypatch.setattr(_run.requests, 'post', post)
    _run.run_agent('http://test', {}, .001, stop_event=stop)
    assert len(executions) == 1
    assert len([p for p in sent if p['results']]) == 2
    assert all(p['agent']['agent_id'] == 'test-agent' for p in sent)


@pytest.mark.parametrize('failure', [requests.ReadTimeout(), 503, 401, 403])
def test_retry_wait_is_interruptible(monkeypatch, failure):
    stop = threading.Event()
    states = []
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'retry_delay', lambda *args: 60)
    class Response:
        status_code = failure
        def json(self):
            return {'ok': False, 'error': {'code': 'UNAUTHORIZED', 'message': 'Denied'}}
    def post(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return Response()
    def status(value):
        states.append(value['state'])
        stop.set()
    monkeypatch.setattr(_run.requests, 'post', post)
    thread = threading.Thread(target=_run.run_agent, args=('http://test', {}),
                              kwargs={'stop_event': stop, 'on_status': status})
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert states == [('unreachable' if isinstance(failure, Exception) else
                       'auth' if failure in (401, 403) else 'http')]


@pytest.mark.parametrize('kind', ['project_run_command', 'egress_http_stream'])
def test_streamed_work_retains_admission_until_terminal(monkeypatch, kind):
    stop, started, release, finished = (threading.Event() for _ in range(4))
    calls = []
    terminal = []
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})

    def stream(cmd_id, params, permissions, results, streams, lock):
        calls.append(cmd_id)
        started.set()
        def finish():
            assert release.wait(2)
            results.append({'id': cmd_id, 'result': {}, 'error': None})
            finished.set()
        if kind == 'project_run_command':
            threading.Thread(target=finish).start()
        else:
            finish()  # Egress implementation is synchronous.

    monkeypatch.setattr(_run, '_start_project_run_streamed' if kind == 'project_run_command'
                        else '_start_egress_stream_streamed', stream)
    count = 0
    class Response:
        status_code = 200
        def json(self):
            return {'commands': [{'id': 'stream', 'type': kind, 'params': {}}]}
    def post(*args, **kwargs):
        nonlocal count
        count += 1
        if count > 1:
            assert started.wait(1)
        if count == 3:
            release.set()
        terminal.extend(kwargs['json']['results'])
        if terminal or count > 30:
            stop.set()
        return Response()
    monkeypatch.setattr(_run.requests, 'post', post)
    _run.run_agent('http://test', {}, .001, stop_event=stop)
    assert finished.wait(1)
    assert calls == ['stream']
    assert terminal


def test_retry_jitter_has_a_hard_ceiling():
    for failures in (1, 5, 10000):
        delay = retry_delay(1, failures)
        assert 0 < delay <= 60


def test_execution_capacity_uses_shared_probe_and_hard_cap(monkeypatch):
    from types import SimpleNamespace
    import runtime_guards
    from lib.desktop_agent._runtime import execution_capacity
    monkeypatch.delenv('TOFU_DESKTOP_EXECUTION_WORKERS', raising=False)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=16, effective_memory_capacity_mb=8192,
        effective_memory_available_mb=2048))
    assert execution_capacity() == 2
    monkeypatch.setenv('TOFU_DESKTOP_EXECUTION_WORKERS', '10000')
    assert execution_capacity() == 4
    monkeypatch.setenv('TOFU_DESKTOP_EXECUTION_WORKERS', 'invalid')
    assert execution_capacity() == 2


def test_poll_never_forwards_credential_on_redirect(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})

    def post(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        assert 'proxies' not in kwargs  # Honor deployment proxy and CA settings.
        assert callable(kwargs['auth'])  # Suppress ambient netrc identity.
        stop.set()
        return type('Response', (), {'status_code': 302})()

    monkeypatch.setattr(_run.requests, 'post', post)
    _run.run_agent('https://test', {}, .001, stop_event=stop, bridge_secret='test-only')
