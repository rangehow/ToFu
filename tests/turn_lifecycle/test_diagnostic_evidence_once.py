"""Diagnostic evidence is durable once per transaction (executable specification).

A ``messages_snapshot`` frame and its attempt event commit in one ``turn.event.record`` transaction together with
the frame's carried task-event row. That row is the evidence's single durable copy: the attempt event names it
(``eventRef``) instead of storing the frame a second time. Conversation replay with the full diagnostic payload
resolves the name to the identical frame; the browser's ``diagnosticPayload=none`` stream carries neither.
Evidence whose task-event row would not outlive conversation replay (``tool_compacted``, streaming retention)
stays inline. Runs on both authorities.
"""
import pytest

from lib.conversation_sync.cursor import STREAM_START
from lib.event_envelope import without_event_format_envelope
from lib.conversation_sync.generated_contract import STREAM_POLICY
from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.conversation_sync.service import ConversationSyncService
from lib.task_replay.task_event_contract import (
    ATTEMPT_EVIDENCE_REFERENCE_KEY, STRUCTURAL_EVENT_TYPES, TASK_EVENT_STRUCTURAL_RETENTION_MS,
)
from tests.turn_lifecycle.test_task_event_delivery import (
    _task, storage as storage, turn_service as turn_service,
)
from tests.turn_lifecycle.test_turn_event_encode_once import (
    _GROWN, _HISTORY, _capture, _finish, _record, _snapshot,
)

pytestmark = pytest.mark.unit


def _attempt_bodies(changes, update_kind):
    return [change['payload']['event']['payload'] for change in changes
            if change['type'] == 'attempt.event'
            and change['payload']['event']['payload'].get('updateKind') == update_kind]


def test_a_snapshot_is_stored_once_and_replays_from_its_row(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'evidence-once')
    sent = _capture(storage, monkeypatch)
    small = _snapshot(1, [{'role': 'system', 'content': 'tiny'}])
    frames = [small, _snapshot(2, _HISTORY), _snapshot(3, _GROWN)]
    try:
        for sequence, frame in enumerate(frames):
            _record(turn_service, task, sequence, frame)
        # The command carries the frame once: in its task-event row. The attempt event names that row,
        # whatever the frame's size.
        assert len(sent) == len(frames)
        for sequence, payload in enumerate(sent):
            body = payload['event_payload']
            assert 'event' not in body
            assert body[ATTEMPT_EVIDENCE_REFERENCE_KEY] == {'taskId': task['id'], 'sequence': sequence}
            assert payload['task_event']['sequence'] == sequence
        rows = {row['sequence']: row['event'] for row in storage.client.query(
            'event.list', {'task_id': task['id'], 'after_sequence': -1, 'limit': 100})}
        assert sorted(rows) == [0, 1, 2]
        # The authority stores no second copy in the attempt event.
        stored = storage.client.query('turn.sync.changes', {
            'conversation_id': task['convId'], 'user_id': 1, 'after': 0, 'limit': 500})
        stored_bodies = _attempt_bodies(stored['events'], 'messages_snapshot')
        assert len(stored_bodies) == len(frames)
        assert all('event' not in body for body in stored_bodies)
        # Full-payload replay resolves each name to the frame its row holds, without the row's persistence
        # envelope; the name itself never leaves the service.
        service = ConversationSyncService(SidecarConversationSyncRepository())
        full = _attempt_bodies(service.changes(task['convId'], 1, after=STREAM_START)['events'],
                               'messages_snapshot')
        assert [body['event'] for body in full] == [
            without_event_format_envelope(rows[sequence]) for sequence in (0, 1, 2)]
        assert all('v' not in body['event'] for body in full)
        assert all(ATTEMPT_EVIDENCE_REFERENCE_KEY not in body for body in full)
        none = _attempt_bodies(service.changes(task['convId'], 1, after=STREAM_START,
                                               diagnostic_payload='none')['events'], 'messages_snapshot')
        assert len(none) == len(frames)
        assert all('event' not in body and ATTEMPT_EVIDENCE_REFERENCE_KEY not in body for body in none)
    finally:
        _finish(task)


def test_evidence_whose_row_expires_before_replay_stays_inline(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'evidence-inline')
    sent = _capture(storage, monkeypatch)
    frame = {'type': 'tool_compacted', 'roundNum': 1, 'summary': 'summary ' * 200}
    try:
        _record(turn_service, task, 0, frame)
        body = sent[-1]['event_payload']
        assert body['event'] == frame
        assert ATTEMPT_EVIDENCE_REFERENCE_KEY not in body
    finally:
        _finish(task)


def test_referenced_rows_outlive_conversation_replay():
    """A reference is safe only while its row outlives every replay of the attempt event."""
    assert 'messages_snapshot' in STRUCTURAL_EVENT_TYPES
    assert 'tool_compacted' not in STRUCTURAL_EVENT_TYPES
    assert TASK_EVENT_STRUCTURAL_RETENTION_MS >= int(STREAM_POLICY['replayRetentionMs'])
