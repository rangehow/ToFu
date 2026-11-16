"""Pipelined independent reads (``query_many``) on every selected authority.

Each read is answered on its own, exactly as a lone ``query`` answers it, and the
reads share one exchange: one storage.v1 connection or one storage.v2 session
request/response train.
"""
import socket
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.storage.port import query_many

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api

NAMESPACE = 'query-many-spec'


def _outcome(client, operation, payload):
    try:
        return client.query(operation, payload)
    except StorageError as error:
        return error


def _same(left, right):
    if isinstance(left, StorageError) or isinstance(right, StorageError):
        return (type(left) is type(right) and left.code == right.code
                and left.retryable == right.retryable)
    return left == right


@pytest.fixture
def seeded(semantic_authority):
    factory, _, _ = semantic_authority
    keys = [f'key-{index:02}-{uuid.uuid4().hex}' for index in range(24)]
    for index, key in enumerate(keys):
        factory(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'expected_version': 0,
            'value': {'index': index, 'text': 'x' * (index * 97)}}, uuid.uuid4().hex)
    reads = [('record.get', {'namespace': NAMESPACE, 'key': key}) for key in keys]
    reads.insert(5, ('record.get', {'namespace': NAMESPACE, 'key': 'absent-' + uuid.uuid4().hex}))
    # A read the authority refuses is that read's own answer, not the exchange's.
    reads.insert(9, ('record.get', {'namespace': NAMESPACE}))
    return factory, reads


class _Exchanges:
    """Counts storage.v1 wire exchanges and storage.v2 request trains.

    The v1 client may carry several lone queries over one keep-alive socket,
    so the boundary is ``_pooled_exchange`` (one request/answer) and
    ``_pipeline`` (one train), not a TCP connect.
    """

    def __init__(self, monkeypatch):
        from lib.storage import client as v1_client
        from tofu_db.wire import StorageV2Session
        self.count = 0
        for name in ('_pooled_exchange', '_pipeline'):
            original = getattr(v1_client.StorageClient, name)

            def counted_exchange(client, *args, _original=original, **kwargs):
                self.count += 1
                return _original(client, *args, **kwargs)
            monkeypatch.setattr(v1_client.StorageClient, name, counted_exchange)
        for name in ('request', 'request_many'):
            original = getattr(StorageV2Session, name)

            def counted(session, *args, _original=original, **kwargs):
                self.count += 1
                return _original(session, *args, **kwargs)
            monkeypatch.setattr(StorageV2Session, name, counted)


def test_each_read_is_answered_as_a_lone_query(seeded):
    factory, reads = seeded
    client = factory()
    expected = [_outcome(client, *read) for read in reads]
    assert isinstance(expected[9], StorageError) and expected[5] is None
    answers = query_many(client, reads)
    assert len(answers) == len(expected)
    assert all(_same(answer, wanted) for answer, wanted in zip(answers, expected))


def test_independent_reads_share_one_exchange(seeded, monkeypatch):
    factory, reads = seeded
    client = factory()
    exchanges = _Exchanges(monkeypatch)
    for operation, payload in reads:
        _outcome(client, operation, payload)
    assert exchanges.count == len(reads)
    exchanges.count = 0
    query_many(client, reads)
    assert exchanges.count == 1


def test_reads_beyond_one_send_buffer_take_more_exchanges_in_order(seeded, monkeypatch):
    """A send never waits on a peer that is waiting for its answers to be read."""
    factory, reads = seeded
    client = factory()
    expected = [_outcome(client, *read) for read in reads]
    from lib.storage import client as v1_client
    from tofu_db import wire
    connect = v1_client.create_connection

    def small_send_buffer(*args, **kwargs):
        sock = connect(*args, **kwargs)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)
        return sock
    monkeypatch.setattr(v1_client, 'create_connection', small_send_buffer)
    original = wire.StorageV2Session.request_many

    def through_small_buffer(session, *args, **kwargs):
        session._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)
        return original(session, *args, **kwargs)
    monkeypatch.setattr(wire.StorageV2Session, 'request_many', through_small_buffer)
    answers = query_many(client, reads)
    assert all(_same(answer, wanted) for answer, wanted in zip(answers, expected))
