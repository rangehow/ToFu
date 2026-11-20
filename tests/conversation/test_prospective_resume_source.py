"""The selected authority alone supplies exact, detached resume source facts."""

import pytest

from lib import storage
from lib.agent_core.storage_errors import StorageError
from lib.conversation_sync.dispatch_contract import ResumeSourceSnapshot, read_resume_source_snapshot

pytestmark = pytest.mark.unit

_PIN = dict(user_id=5, conversation_id='conv', turn_id='turn', attempt_id='attempt',
            projection_revision=7)
_WITNESS = dict(status='source_pinned', conversationId='conv', turnId='turn',
                attemptId='attempt', projectionRevision=7, sourceStatus='failed')


@pytest.mark.parametrize(('response', 'expected'), [
    (_WITNESS, ResumeSourceSnapshot('conv', 'turn', 'attempt', 7, 'failed')),
    ({'status': 'stale_source'}, 'stale_source'),
    ({'status': 'source_unavailable'}, 'source_unavailable'),
    (StorageError('unknown_operation', 'older authority'), 'source_unavailable'),
    (StorageError('database_protocol_error', 'Unknown storage operation'), 'source_unavailable'),
    (StorageError('database_protocol_error', 'Invalid payload'), 'Invalid payload'),
    ({**_WITNESS, 'projectionRevision': 8}, None),
    ({**_WITNESS, '_resumeSource': {}}, None),
])
def test_read_resume_source_snapshot(monkeypatch, response, expected):
    calls = []

    class ReadOnlyAuthority:
        def query(self, operation, payload):
            calls.append((operation, payload))
            if isinstance(response, Exception):
                raise response
            return response

    def read_client(*, write=False):
        assert not write
        return ReadOnlyAuthority()

    monkeypatch.setattr(storage, 'get_storage_client', read_client)
    if expected is None:
        with pytest.raises(StorageError, match='Malformed resume source snapshot'):
            read_resume_source_snapshot(**_PIN)
    elif expected == 'Invalid payload':
        with pytest.raises(StorageError, match=expected):
            read_resume_source_snapshot(**_PIN)
    else:
        assert read_resume_source_snapshot(**_PIN) == expected
    assert calls == [('turn.resume.plan.read', _PIN)]
