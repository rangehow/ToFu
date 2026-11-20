"""Execution evidence stays bounded, owner-scoped, and free of raw detail."""
import pytest

from lib.router.project_execution_health import execution_health
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

IDENTITY = {'conversation_id': 'lead-a', 'user_id': 23}


@pytest.mark.parametrize(('cause', 'withdrawn'), [('generation_error', False), ('dispatch_grant_revoked', True)])
def test_observed_failure_uses_only_current_main_lane_metadata(cause, withdrawn):
    rows = [
        {'turn_id': 'branch', 'attempt_id': 'a'},
        {'turn_id': 'superseded', 'attempt_id': 'b'},
        {'turn_id': 'failed', 'attempt_id': 'c'},
    ]
    turns = {
        'branch': {'laneId': 'branch-a', 'currentAttemptId': 'a'},
        'superseded': {'laneId': 'main', 'currentAttemptId': 'new'},
        'failed': {'laneId': 'main', 'currentAttemptId': 'c', 'status': 'failed',
                   'settlement': {'cause': cause, 'error': {'detail': 'Never expose raw detail'}}},
    }

    class Storage:
        def query(self, operation, payload):
            if operation == 'turn.timing_trace.list':
                assert payload == IDENTITY
                return {'records': rows, 'has_more': True}
            assert operation == 'turn.get'
            turn_id = payload['turn_id']
            assert payload == {**IDENTITY, 'turn_id': turn_id, 'include_projection': False}
            return {**turns[turn_id], 'conversationId': 'lead-a', 'turnId': turn_id}

    assert execution_health(23, 'lead-a', client=Storage) == {
        'state': 'observed', 'turn_id': 'failed', 'status': 'failed', 'authority_withdrawn': withdrawn}


@pytest.mark.parametrize(('result', 'state'), [
    ({'records': [], 'has_more': False}, 'no_bound_attempt'),
    ({'records': [], 'has_more': True}, 'unknown'),
    ({'records': [{'turn_id': 'gone', 'attempt_id': 'a'}], 'has_more': False}, 'unknown'),
    (StorageError('database_unavailable', 'Sensitive storage diagnostics'), 'unknown'),
])
def test_incomplete_or_unreadable_metadata_never_looks_healthy(result, state):
    calls = []

    class Storage:
        def query(self, operation, payload):
            calls.append(operation)
            if operation == 'turn.get':
                return None
            assert payload == IDENTITY
            if isinstance(result, Exception):
                raise result
            return result

    assert execution_health(23, 'lead-a', client=Storage) == {'state': state}
    assert calls.count('turn.timing_trace.list') == 1
