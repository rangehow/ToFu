"""Transport cancellation cannot escape one exclusive request's ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from tofu_db._request_cancellation import RequestCancellation

pytestmark = pytest.mark.unit


def test_detach_waits_for_interrupt_before_connection_can_be_reused():
    cancellation = RequestCancellation()
    entered, release, detaching, detached = (threading.Event() for _ in range(4))
    calls = []
    def interrupt():
        calls.append('interrupt')
        entered.set()
        assert release.wait(5)
    binding = cancellation.bind(interrupt)
    binding.__enter__()
    def detach():
        detaching.set()
        binding.__exit__(None, None, None)
        detached.set()
    with ThreadPoolExecutor() as executor:
        cancelled = executor.submit(cancellation.cancel)
        assert entered.wait(5)
        finished = executor.submit(detach)
        try:
            assert detaching.wait(5)
            assert not detached.is_set()
        finally:
            release.set()
        cancelled.result(5)
        finished.result(5)
    cancellation.cancel()
    assert calls == ['interrupt']
    with pytest.raises(OSError, match='before dispatch'):
        with cancellation.bind(lambda: pytest.fail('cancelled request borrowed a new connection')):
            pytest.fail('cancelled request dispatched')


def test_native_pool_retires_interrupted_exchange_even_if_response_completed():
    from lib.storage.v2_client import StorageV2Client
    cancellation = RequestCancellation()
    interrupted, releases = [], []
    def request(*args, **kwargs):
        cancellation.cancel()
        return SimpleNamespace(status=0, json=lambda: {'complete': True})
    session = SimpleNamespace(interrupt=lambda: interrupted.append(True), request=request)
    client = object.__new__(StorageV2Client)
    client._pool = SimpleNamespace(acquire=lambda timeout, **kwargs: session,
        release=lambda returned, *, broken: releases.append((returned, broken)))
    result = client._request_once('worker_control.watch', {}, timeout=1,
        command_id=None, deadline_unix_ms=1, cancellation=cancellation)
    assert result == {'complete': True}
    assert interrupted == [True]
    assert releases == [(session, True)]


@pytest.mark.parametrize('transport', ['sidecar-connect', 'native-connect', 'native-hello'])
def test_worker_stop_joins_cold_watch_before_storage_close(monkeypatch, transport):
    import socket
    from tofu_db import _loopback, wire
    from lib.durable_worker.runtime import create_service
    from lib.storage.client import StorageClient
    from lib.storage.v2_client import StorageV2Client

    waiting = threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        host, port = listener.getsockname()
        if transport.endswith('connect'):
            # Model a TCP connect that never becomes writable. The only
            # selector wake is the real cancellation socketpair byte.
            real_selector = _loopback.selectors.DefaultSelector

            class StalledConnectSelector:
                def __enter__(self):
                    self.selector = real_selector()
                    return self

                def __exit__(self, *exc):
                    self.selector.close()

                def register(self, sock, event):
                    if event == _loopback.selectors.EVENT_READ:
                        self.selector.register(sock, event)

                def select(self, timeout):
                    waiting.set()
                    return self.selector.select(timeout)

            import errno
            monkeypatch.setattr(socket.socket, 'connect_ex', lambda *a: errno.EINPROGRESS)
            monkeypatch.setattr(_loopback.selectors, 'DefaultSelector', StalledConnectSelector)
        if transport.startswith('sidecar'):
            client = StorageClient(host, port, 'fixture-token-' * 4)
        else:
            client = StorageV2Client(host, port, b'x' * 32, owner_id=1, tenant_id=None)
        service = create_service(client_factory=lambda **_: client, max_workers=1)

        def hold_hello():
            listener.settimeout(5)
            with listener.accept()[0] as peer:
                peer.settimeout(5)
                assert wire.recv_frame(peer)[wire.FIELD_KIND] == wire.KIND_HELLO
                waiting.set()  # no ACK; client shutdown must terminate the read
                assert peer.recv(1) == b''

        with ThreadPoolExecutor() as executor:
            peer = executor.submit(hold_hello) if transport == 'native-hello' else None
            start = executor.submit(service.start)
            try:
                assert waiting.wait(5)
                assert service.snapshot()['state'] == 'starting'
                assert not start.done()
                assert service.stop(timeout=2), 'stop must join before closing storage'
                with pytest.raises(RuntimeError, match='stopped during startup'):
                    start.result(2)
                if peer is not None:
                    peer.result(2)
                assert service.snapshot()['state'] == 'stopped'
                assert client._pool.resource_snapshot()['used'] == 0
                assert client._pool.resource_snapshot()['idle'] == 0
            finally:
                service.stop()
                client.close()


@pytest.mark.parametrize('native', [False, True])
def test_cancelled_watch_does_not_dial(monkeypatch, native):
    import socket
    import time
    from lib.storage.client import StorageClient
    from lib.storage.errors import StorageError
    from lib.storage.v2_client import StorageV2Client

    cancellation = RequestCancellation()
    cancellation.cancel()
    client = (StorageV2Client('127.0.0.1', 9, b'x' * 32, owner_id=1, tenant_id=None)
              if native else StorageClient('127.0.0.1', 9, 'fixture-token-' * 4))

    def forbid_dial(*args, **kwargs):
        pytest.fail('cancelled watch attempted to dial')

    monkeypatch.setattr(socket.socket, 'connect', forbid_dial)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbid_dial)
    try:
        with pytest.raises(StorageError, match='unavailable'):
            client.worker_watch(cursor=None, deadline_unix_ms=int(time.time() * 1000) + 5000,
                                cancellation=cancellation)
        assert client._pool.resource_snapshot()['used'] == 0
    finally:
        client.close()
