"""Exercise conversation delivery lifecycle with a controlled native TCP peer."""
from concurrent.futures import ThreadPoolExecutor
import socket
from types import SimpleNamespace

import pytest

from lib import storage
from lib.conversation_sync import delivery_runtime
from lib.storage.v2_client import StorageV2Client
from tofu_db import wire

pytestmark = pytest.mark.api


def test_stop_joins_cold_hello_before_storage_close(monkeypatch):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        client = StorageV2Client(*listener.getsockname(), b'x' * 32, owner_id=1, tenant_id=None)
        monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: client)
        app = SimpleNamespace(extensions={})
        with ThreadPoolExecutor(max_workers=2) as executor:
            startup = executor.submit(delivery_runtime.ensure_started, app)
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(5)
                    assert wire.recv_frame(peer)[wire.FIELD_KIND] == wire.KIND_HELLO
                    assert not startup.done()
                    stopped = executor.submit(delivery_runtime.stop, app)
                    # Keep the peer open without ever sending HelloAck. Stop,
                    # not peer teardown or storage close, must release the read.
                    stopped.result(2)
                    assert peer.recv(1) == b''
                    assert not app.extensions
                    snapshot = client._pool.resource_snapshot()
                    assert snapshot['used'] == snapshot['idle'] == 0
                    startup.result(2)
            finally:
                delivery_runtime.stop(app)
                client.close()


def _respond(peer, request, schema, payload=b''):
    peer.sendall(wire.pack_frame(wire.encode_message({
        wire.FIELD_KIND: wire.KIND_RESPONSE,
        wire.FIELD_PROTOCOL_VERSION: wire.PROTOCOL_VERSION,
        wire.FIELD_CORRELATION_ID: request[wire.FIELD_CORRELATION_ID],
        wire.FIELD_SCHEMA_ID: schema,
        wire.FIELD_PAYLOAD: payload,
        wire.FIELD_STATUS: 0,
        wire.FIELD_ERROR_CODE: None,
        wire.FIELD_ERROR_MESSAGE: None,
        wire.FIELD_RETRYABLE: False,
    })))


def test_delivery_reuses_enrollment_session_for_polls(monkeypatch):
    import json
    from lib.conversation_sync.broker import broker
    from lib.storage.generated_worker_control import CONVERSATION_FORMAT

    hints = []
    monkeypatch.setattr(broker, 'invalidate', hints.append)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        client = StorageV2Client(*listener.getsockname(), b'x' * 32, owner_id=1, tenant_id=None)
        monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: client)
        app = SimpleNamespace(extensions={})
        with ThreadPoolExecutor() as executor:
            startup = executor.submit(delivery_runtime.ensure_started, app)
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(5)
                    hello = wire.recv_frame(peer)
                    schema = hello[wire.FIELD_SCHEMA_IDS][0]
                    _respond(peer, hello, schema)
                    scoped = [{'userId': 1, 'conversationId': 'one'}]
                    for change, scopes in [(0, []), (0, []), (1, scoped)]:
                        poll = wire.recv_frame(peer)
                        assert poll[wire.FIELD_KIND] == wire.KIND_REQUEST
                        assert poll[wire.FIELD_OPERATION] == 'conversation_control.watch'
                        _respond(peer, poll, schema, json.dumps({
                            'format': CONVERSATION_FORMAT,
                            'cursor': {'epoch': 'fixture', 'change': change},
                            'conversations': scopes,
                        }).encode())
                    # Next poll proves the preceding response was delivered.
                    assert wire.recv_frame(peer)[wire.FIELD_KIND] == wire.KIND_REQUEST
                    startup.result(2)
                    assert hints == [scoped]
                    delivery_runtime.stop(app)
                    assert peer.recv(1) == b''
                    snapshot = client._pool.resource_snapshot()
                    assert snapshot['used'] == snapshot['idle'] == 0
            finally:
                delivery_runtime.stop(app)
                client.close()
