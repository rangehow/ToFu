"""Metadata admission validates the public Turn schema and requested identity."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from lib import turn_lifecycle
from lib.acceptance import turns as acceptance
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def metadata():
    return {
        'turnId': 'turn', 'presentationId': 'presentation',
        'conversationId': 'conversation', 'laneId': 'main',
        'parentTurnId': None, 'ordinal': 1, 'actor': 'assistant',
        'kind': 'message', 'runId': '', 'status': 'running',
        'currentAttemptId': 'attempt', 'projectionRevision': 1,
        'modelContextRevision': 1, 'settlement': {},
        'createdAt': 1, 'updatedAt': 1,
    }


class Client:
    def __init__(self, response):
        self.response = response

    def query(self, operation, payload):
        assert operation == 'turn.get' and payload['include_projection'] is False
        return deepcopy(self.response)


@pytest.mark.parametrize('response', [[], 'untrusted-success', {}, 7, False])
def test_nonmetadata_response_fails_as_protocol_error(monkeypatch, response):
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(response))
    with pytest.raises(StorageError) as caught:
        turn_lifecycle.get_turn('conversation', 'turn', user_id=7, include_projection=False)
    assert caught.value.code == 'database_protocol_error'


@pytest.mark.parametrize(('field', 'value'), [
    ('turnId', 'foreign-turn'), ('conversationId', 'foreign-conversation'),
    ('projectionRevision', True), ('modelContextRevision', -1),
    ('createdAt', '1'), ('ordinal', False), ('actor', 'unsupported'),
    ('status', 'unsupported'), ('settlement', []), ('currentAttemptId', 1),
    ('_privateLocator', 'private'), ('projection', {}),
])
def test_malformed_or_mismatched_metadata_fails_closed(monkeypatch, field, value):
    response = metadata()
    response[field] = value
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(response))
    with pytest.raises(StorageError) as caught:
        turn_lifecycle.get_turn('conversation', 'turn', user_id=7, include_projection=False)
    assert caught.value.code == 'database_protocol_error'


@pytest.mark.parametrize('field', ['turnId', 'presentationId', 'conversationId', 'actor', 'settlement'])
def test_missing_public_metadata_is_not_synthesized(monkeypatch, field):
    response = metadata()
    del response[field]
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(response))
    with pytest.raises(StorageError) as caught:
        turn_lifecycle.get_turn('conversation', 'turn', user_id=7, include_projection=False)
    assert caught.value.code == 'database_protocol_error'


def test_valid_metadata_remains_exact_and_absence_remains_not_found(monkeypatch):
    response = metadata()
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(response))
    assert turn_lifecycle.get_turn('conversation', 'turn', user_id=7,
                                   include_projection=False) == response
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(None))
    with pytest.raises(turn_lifecycle.LifecycleNotFound):
        turn_lifecycle.get_turn('conversation', 'turn', user_id=7, include_projection=False)


@pytest.fixture
def native_metadata():
    # The native semantic turn.get regression pins its complete response to
    # this wire fixture (including the nested receipt), without a projection.
    return json.loads((Path(__file__).parents[1] / 'fixtures/native_turn_metadata.json').read_text())


@pytest.mark.parametrize('presentation_id', ['optimistic-turn', 'turn'], ids=['normal', 'legacy'])
def test_native_turn_metadata_passes_strict_public_contract(native_metadata, presentation_id):
    native_metadata['presentationId'] = presentation_id
    turn = turn_lifecycle.get_turn('metadata', 'turn', user_id=11,
                                   include_projection=False, reads=Client(native_metadata))
    assert turn == native_metadata
    assert 'projection' not in turn


@pytest.mark.parametrize(('field', 'value'), [
    ('attemptId', ''), ('resultRevision', True), ('ownerUserId', 11),
])
def test_native_turn_metadata_refuses_invalid_worker_receipts(native_metadata, field, value):
    native_metadata['settlement']['coordinatorWorkerObservations']['assignment'][field] = value
    with pytest.raises(StorageError) as caught:
        turn_lifecycle.get_turn('metadata', 'turn', user_id=11,
                                include_projection=False, reads=Client(native_metadata))
    assert caught.value.code == 'database_protocol_error'


def test_invalid_actor_cannot_bypass_required_execution_context(monkeypatch):
    response = metadata()
    response['actor'] = 'unsupported'
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client(response))
    monkeypatch.setattr(acceptance, 'get_attempt', lambda *args, **kwargs: {
        'attemptId': 'attempt', 'conversationId': 'conversation',
        'turnId': 'turn', 'taskId': 'task', 'config': {},
    })
    principal = PrincipalContext.user(subject_id='metadata-reader', owner_user_id=7)
    with pytest.raises(StorageError) as caught:
        acceptance.read_execution_requirements('conversation', 'turn', 'attempt',
                                               'task', principal=principal)
    assert caught.value.code == 'database_protocol_error'
