"""Browser replay leaves diagnostic bodies at the existing Inspector authority."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.conversation_sync.cursor import STREAM_START, ReplayPosition
from lib.conversation_sync.service import ConversationSyncService
from lib.conversation_sync.validation import decode

EPOCH = '00000000000000e1'

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('evidence_type', ['messages_snapshot', 'tool_compacted'])
def test_state_only_replay_keeps_patch_sequence_and_full_durable_evidence(evidence_type):
    diagnostic = {'type': evidence_type, 'messages': [{'content': 'exact evidence ' * 200_000}]}
    patch = {'version': 1, 'baseRevision': 1, 'targetRevision': 2,
             'operations': [{'op': 'set', 'path': ['content'], 'value': 'done'}]}
    attempt = {'attemptId': 'attempt', 'turnId': 'turn', 'conversationId': 'conv',
               'seq': 1, 'projectionRevision': 2, 'type': 'projection_updated',
               'payload': {'status': 'running', 'phase': None,
                           'projectionPatch': patch, 'event': diagnostic}}
    change = {'contract': 'tofu.conversation-sync.event/v1', 'type': 'attempt.event',
              'conversationId': 'conv', 'syncSeq': 2, 'occurredAt': 1,
              'payload': {'event': attempt}}
    terminal = {**change, 'syncSeq': 3, 'payload': {'event': {
        **attempt, 'seq': 2, 'type': 'terminal_settlement', 'projectionRevision': 3,
        'payload': {'status': 'completed', 'phase': None, 'settlement': {'cause': 'completed'}},
    }}}
    stored = {'head': 3, 'events': [change, terminal], 'hasMore': False,
              'authorityEpoch': EPOCH, 'headDigest': 'd3' * 8,
              'changeDigests': ['d2' * 8, 'd3' * 8], 'anchorDigest': 'd1' * 8}
    after = ReplayPosition(1, EPOCH, 'd1' * 8)
    original = deepcopy(stored)
    calls = []

    def changes(*args, **kwargs):
        calls.append((args, kwargs))
        return stored

    service = ConversationSyncService(SimpleNamespace(changes=changes))
    projected = service.changes('conv', 7, after=after, diagnostic_payload='none')
    replay = projected['events'][0]
    assert replay['payload']['event']['payload'] == {
        'status': 'running', 'phase': None, 'projectionPatch': patch,
    }
    assert replay['syncSeq'] == 2
    assert projected['events'][1] == terminal
    assert projected['head'] == 3 and not projected['hasMore']
    assert stored == original
    assert service.changes('conv', 7, after=after)['events'] == original['events']
    assert all(args == ('conv', 7) for args, _kwargs in calls)
    assert calls[0][1] == {'after_sequence': 1, 'limit': 500}
    for event in projected['events']:
        decode('ConversationChange', event)
        decode('AttemptEvent', event['payload']['event'])


def test_unknown_diagnostic_representation_refuses_before_repository_read():
    def forbidden(*_args, **_kwargs):
        pytest.fail('invalid representation reached storage')
    service = ConversationSyncService(SimpleNamespace(changes=forbidden))
    with pytest.raises(ValueError, match='diagnostic'):
        service.changes('conv', 7, after=STREAM_START, diagnostic_payload='invalid')
