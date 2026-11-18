"""Session verbs share one kept relay connection per scope (loopback_relay "Session verbs").

A Turn asks the execution host's MCP bridge for its catalog every round; one
connection (endpoint-record read + TCP connect) per ask was measurable round
cost. Inside ``relay_session`` a declared read verb reuses one connection; the
owner restarting under it, a concurrent reader and every other verb keep the
one-connection-per-call behaviour, and the owner's close still ends promptly.
"""
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _owner(path, calls):
    from lib.runtime.loopback_relay import RelayServer

    def read(params, cancel):
        calls.append(('read', params.get('n')))
        return {'n': params.get('n')}

    def act(params, cancel):
        calls.append(('act', params.get('n')))
        return 'done'

    server = RelayServer(path, format='test/v1', methods={'read': read, 'act': act},
                         name='test', session_methods=frozenset({'read'}))
    server.start()
    return server


def _client(path):
    from lib.runtime.loopback_relay import RelayClient
    client = RelayClient(path, format='test/v1', name='test', cancel_observation_seconds=0.05,
                         session_methods=frozenset({'read'}))
    reads = []
    endpoint = client._endpoint

    def counted():
        reads.append(1)
        return endpoint()

    client._endpoint = counted
    return client, reads


def test_session_reads_share_one_connection_and_record_read(tmp_path):
    from lib.runtime.loopback_relay import relay_session
    calls = []
    server = _owner(tmp_path / 'relay.json', calls)
    client, record_reads = _client(tmp_path / 'relay.json')
    try:
        with relay_session():
            assert [client.call('read', {'n': n}) for n in range(5)] == [{'n': n} for n in range(5)]
            assert record_reads == [1]
            assert len(server._calls) == 1  # one serving thread holds the kept connection
            # A non-session verb inside the scope keeps its own connection per call.
            assert client.call('act', {'n': 9}) == 'done'
            assert record_reads == [1, 1]
        deadline = time.monotonic() + 5
        while server._calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not server._calls, 'the scope end closes its kept connection'
        assert calls == [('read', n) for n in range(5)] + [('act', 9)]
        # Outside a scope every read is its own connection again.
        client.call('read', {'n': 7})
        client.call('read', {'n': 8})
        assert len(record_reads) == 4
    finally:
        assert server.close(timeout=10.0)


def test_owner_restart_under_a_kept_connection_reconnects_once(tmp_path):
    from lib.runtime.loopback_relay import relay_session
    calls = []
    path = tmp_path / 'relay.json'
    server = _owner(path, calls)
    client, record_reads = _client(path)
    try:
        with relay_session():
            assert client.call('read', {'n': 1}) == {'n': 1}
            assert server.close(timeout=10.0)  # wakes the idle kept connection at once
            server = _owner(path, calls)  # new port and token
            assert client.call('read', {'n': 2}) == {'n': 2}
            assert client.call('read', {'n': 3}) == {'n': 3}
        assert calls == [('read', 1), ('read', 2), ('read', 3)]
        assert record_reads == [1, 1]
    finally:
        assert server.close(timeout=10.0)


def test_concurrent_reader_in_one_scope_takes_its_own_connection(tmp_path):
    from contextvars import copy_context
    from lib.runtime.loopback_relay import relay_session
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(params, cancel):
        entered.set()
        release.wait(10)
        return 'slow'

    from lib.runtime.loopback_relay import RelayServer
    server = RelayServer(tmp_path / 'relay.json', format='test/v1',
                         methods={'read': lambda p, c: calls.append(p) or 'fast', 'slow': slow},
                         name='test', session_methods=frozenset({'read', 'slow'}))
    server.start()
    from lib.runtime.loopback_relay import RelayClient
    client = RelayClient(tmp_path / 'relay.json', format='test/v1', name='test',
                         cancel_observation_seconds=0.05,
                         session_methods=frozenset({'read', 'slow'}))
    try:
        with relay_session():
            result = {}
            context = copy_context()
            worker = threading.Thread(
                target=lambda: result.setdefault('slow', context.run(client.call, 'slow', {})))
            worker.start()
            assert entered.wait(10)
            assert client.call('read', {'n': 1}) == 'fast'  # not queued behind the kept call
            release.set()
            worker.join(10)
            assert result == {'slow': 'slow'}
    finally:
        release.set()
        assert server.close(timeout=10.0)


def test_close_ends_an_idle_kept_connection_promptly(tmp_path):
    from lib.runtime.loopback_relay import relay_session
    server = _owner(tmp_path / 'relay.json', [])
    client, _ = _client(tmp_path / 'relay.json')
    with relay_session():
        client.call('read', {'n': 1})
        started = time.monotonic()
        assert server.close(timeout=30.0)
        assert time.monotonic() - started < 5.0
