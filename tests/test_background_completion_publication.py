"""Transient completion publication retries preserve one durable identity."""
from __future__ import annotations

import pytest

from lib.background_command_delivery import publish_completion
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def test_retry_uses_same_arguments_and_stops_after_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', sleeps.append)
    calls = []
    payload = {'timestamp': 42}
    def enqueue(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise StorageError('database_unavailable', 'temporary')
        return {'queueId': 'stable'}
    assert publish_completion(enqueue, 'conv', payload, user_id=1) == {'queueId': 'stable'}
    assert calls[0] == calls[1]
    assert calls[1][0][1] is payload
    assert sleeps == [0.1]


@pytest.mark.parametrize('code,count', [('database_forbidden', 1), ('database_timeout', 3)])
def test_retry_budget_and_permanent_failure(monkeypatch, code, count):
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _: None)
    calls = []
    def enqueue():
        calls.append(1)
        raise StorageError(code, 'unavailable')
    with pytest.raises(StorageError):
        publish_completion(enqueue)
    assert len(calls) == count


def test_completion_identity_and_payload_are_stable_after_lost_ack(monkeypatch):
    from lib import message_queue
    calls = []
    class Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, payload, command_id))
            if len(calls) == 1:
                raise StorageError('database_timeout', 'lost acknowledgement')
            return {'queueId': payload['queue_id']}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **_: Client())
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _: None)
    payload = {'_backgroundCommand': 'bg_one', 'timestamp': 123, 'text': 'result'}
    result = publish_completion(message_queue.enqueue_message, 'conv', payload, {},
                                kind=message_queue.KIND_WORKFLOW, user_id=7)
    assert calls[0] == calls[1]
    assert result['queueId'] == calls[0][2]
