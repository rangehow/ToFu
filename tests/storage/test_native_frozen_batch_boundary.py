"""Synthetic client-only evidence: frozen event objects are not encoded again.

No engine, durability barrier, receipt store or native artifact is exercised.
The peer uses the pinned storage.v2 codec on an owned socketpair.
"""
from collections import Counter
import threading
from types import SimpleNamespace

import orjson
import pytest

from lib.agent_core import storage_json
from lib.storage.errors import StorageError
from lib.storage.protocol import FrozenPayload
from lib.storage.v2_semantics import request_payload
from tests.tofudb.test_tofudb_v2_client import _socketpair_session
from tofu_db import wire

pytestmark = pytest.mark.unit


@pytest.fixture
def batch(monkeypatch):
    from lib import turn_lifecycle as lifecycle

    task = {'id': 'task-synthetic', 'user_id': 11, 'phase': 'calling'}
    projection = {'content': '', 'thinking': '', 'segments': [], 'fileChanges': []}
    monkeypatch.setattr(lifecycle, '_now_ms', lambda: 1234)
    monkeypatch.setattr(lifecycle, '_frame_projection',
                        lambda source, previous, raw_event, **kwargs:
                        (False, previous, {**projection, 'content': raw_event['summary']}))
    entries = []
    for index, text in enumerate(['摘要 "quoted"\n', 'second', 'third']):
        event = {'type': 'tool_compacted', 'summary': text, 'ratio': 1e-5}
        entries.append(lifecycle.TaskEventBatchEntry(
            raw_event=event, sequence=index,
            task_event={'task_id': task['id'], 'sequence': index, 'event': event}))

    def prepare():
        previous = projection
        prepared = []
        for index, entry in enumerate(entries):
            item = lifecycle._prepare_task_event_batch_entry(
                task, entry, attempt_id='attempt-synthetic', user_id=11,
                actor='assistant', previous=previous, segments_stable=True,
                revision=20 + index, fold_due=True, live_flow=False)
            prepared.append(item)
            previous = item.applied_projection
        return prepared

    return SimpleNamespace(task=task, entries=entries, prepare=prepare,
                           projection=projection, lifecycle=lifecycle)


def _results(entries):
    return [{'applied': True, 'projection_revision': 21 + index,
             'task_event': {'inserted': True, 'task_id': 'task-synthetic',
                            'sequence': entry.sequence}, 'task_event_count': 1}
            for index, entry in enumerate(entries)]


def test_frozen_batch_encodes_each_event_once_not_again_on_send_or_replay(monkeypatch, batch):
    counts = Counter()
    held = []
    original = storage_json.json_value_bytes

    def counted(value):
        counts[id(value)] += 1
        held.append(value)
        return original(value)

    monkeypatch.setattr(storage_json, 'json_value_bytes', counted)
    prepared = batch.prepare()
    expected = orjson.dumps({'user_id': 11, 'entries': [dict(item.payload) for item in prepared]})
    frozen = FrozenPayload({'user_id': 11, 'entries': [item.payload for item in prepared]},
                           {'entries': b'[' + b','.join(item.wire for item in prepared) + b']'})
    assert [counts[id(entry.task_event['event'])] for entry in batch.entries] == [1, 1, 1]
    assert all(item.payload['user_id'] == 11 for item in prepared)
    assert all(item.payload['task_id'] == 'task-synthetic' for item in prepared)
    prepared_counts = counts.copy()
    # The encoded revision/sequence witness, not a later mutable caller graph,
    # defines each retry. This mutation must never cross the freeze boundary.
    batch.entries[0].task_event['event']['summary'] = 'not the frozen event'
    session, peer_socket = _socketpair_session()
    seen, failures = [], []
    result_bytes = orjson.dumps(_results(batch.entries))

    def peer():
        try:
            for status, payload in [(1, b'{}'), (0, result_bytes), (0, result_bytes)]:
                message = wire.recv_frame(peer_socket)
                seen.append(message)
                peer_socket.sendall(wire.pack_frame(wire.encode_message({
                    wire.FIELD_KIND: wire.KIND_RESPONSE,
                    wire.FIELD_PROTOCOL_VERSION: wire.PROTOCOL_VERSION,
                    wire.FIELD_CORRELATION_ID: message[wire.FIELD_CORRELATION_ID],
                    wire.FIELD_SCHEMA_ID: wire.STORAGE_SCHEMA_VERSION,
                    wire.FIELD_PAYLOAD: payload, wire.FIELD_STATUS: status,
                    wire.FIELD_ERROR_CODE: 'database_conflict' if status else None,
                    wire.FIELD_ERROR_MESSAGE: 'synthetic refusal' if status else None,
                    wire.FIELD_RETRYABLE: False})))
        except BaseException as error:
            failures.append(error)
        finally:
            peer_socket.close()

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        responses = [session.request('turn.event.record_batch',
                     request_payload('turn.event.record_batch', frozen, owner_id=11),
                     command_id='batch:attempt-synthetic:20:0-2',
                     deadline_unix_ms=10_000) for _ in range(3)]
        assert responses[0].status == 1
        assert responses[0].error_code == 'database_conflict'
        assert responses[0].error_message == 'synthetic refusal'
        assert responses[0].retryable is False
        assert [response.payload for response in responses[1:]] == [result_bytes, result_bytes]
        assert responses[1].json() == orjson.loads(result_bytes)
    finally:
        session._sock.close()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures
    assert counts == prepared_counts
    assert [message[wire.FIELD_PAYLOAD] for message in seen] == [expected] * 3
    assert [message[wire.FIELD_COMMAND_ID] for message in seen] == ['batch:attempt-synthetic:20:0-2'] * 3
    assert len({message[wire.FIELD_CORRELATION_ID] for message in seen}) == 3
    assert [entry['task_event']['sequence'] for entry in orjson.loads(expected)['entries']] == [0, 1, 2]


def test_nonfinite_unfrozen_field_still_refuses_before_transport():
    frozen = FrozenPayload({'entries': [{'sequence': 0}]})
    frozen['unfrozen'] = float('nan')
    session, peer_socket = _socketpair_session()
    try:
        with pytest.raises(StorageError, match='non-finite') as error:
            session.request('turn.event.record_batch',
                            request_payload('turn.event.record_batch', frozen, owner_id=11),
                            command_id='synthetic-invalid', deadline_unix_ms=10_000)
        assert error.value.code == 'database_protocol_error'
        peer_socket.setblocking(False)
        with pytest.raises(BlockingIOError):
            peer_socket.recv(1)
    finally:
        session._sock.close()
        peer_socket.close()


@pytest.mark.parametrize('missing', [None, 'task_event', 'task_event_count'])
def test_production_batch_caller_confirms_only_complete_results(monkeypatch, batch, missing):
    lifecycle = batch.lifecycle
    batch.task['_userId'] = 11
    state = {'projectionRevision': 20, 'projection': batch.projection,
             'actor': 'assistant', 'conversationId': 'conv-synthetic',
             'turnId': 'turn-synthetic', 'segmentsStable': True}
    monkeypatch.setattr(lifecycle, '_task_projection_state', lambda *args: state)
    monkeypatch.setattr(lifecycle, '_structural_fold_due', lambda *args: True)
    remembered, finished = [], []
    monkeypatch.setattr(lifecycle, '_remember_task_projection_state',
                        lambda *args, **kwargs: remembered.append(kwargs))
    monkeypatch.setattr(lifecycle, '_finish_task_event_chunk',
                        lambda *args: finished.append(args))
    results = _results(batch.entries)
    if missing:
        results[1].pop(missing)
    session, peer_socket = _socketpair_session()
    failures, received = [], []

    def peer():
        try:
            message = wire.recv_frame(peer_socket)
            received.append(orjson.loads(message[wire.FIELD_PAYLOAD]))
            from tests.tofudb.test_tofudb_v2_client import _answer
            _answer(peer_socket, message[wire.FIELD_CORRELATION_ID], orjson.dumps(results))
        except BaseException as error:
            failures.append(error)
        finally:
            peer_socket.close()

    def command(operation, payload, command_id, **kwargs):
        return session.request(operation, request_payload(operation, payload, owner_id=11),
                               command_id=command_id, deadline_unix_ms=10_000).json()

    monkeypatch.setattr(lifecycle, '_turn_client',
                        lambda **kwargs: SimpleNamespace(command=command))
    thread = threading.Thread(target=peer)
    thread.start()
    try:
        budget = SimpleNamespace(batch_max_events=500, batch_max_bytes=62914560)
        if missing:
            with pytest.raises(StorageError, match='complete turn event batch') as error:
                lifecycle._record_task_event_chunk(batch.task, batch.entries,
                    'attempt-synthetic', budget, live_flow=False)
            assert error.value.code == 'database_protocol_error'
            assert remembered == finished == []
        else:
            assert lifecycle._record_task_event_chunk(batch.task, batch.entries,
                'attempt-synthetic', budget, live_flow=False) == (True, True, None)
            assert remembered[0]['projection_revision'] == 23
            assert len(finished) == 1 and finished[0][2] == batch.entries
    finally:
        session._sock.close()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures
    assert len(received) == 1
    assert received[0]['user_id'] == 11
    for index, entry in enumerate(received[0]['entries']):
        assert entry['user_id'] == 11 and entry['task_id'] == 'task-synthetic'
        assert entry['attempt_id'] == 'attempt-synthetic'
        assert entry['event_type'] == 'projection_updated' and entry['terminal'] is False
        assert entry['task_event']['sequence'] == index
        assert entry['task_event']['task_id'] == entry['task_id']
