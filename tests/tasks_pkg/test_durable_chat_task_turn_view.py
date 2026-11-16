"""The durable view of a running conversation attempt is its Turn (executable specification).

A conversation attempt's visible state is owned by its Turn projection and attempt authority; its task-result
row only names them (``turnId``/``attemptId`` from birth). ``GET /api/v1/tasks/<id>?view=durable``, its cold
fallback and the durable events/SSE replay therefore read a NON-terminal conversation attempt's content,
thinking and status from the Turn and the attempt, not from whatever running checkpoint the row last carried.
The same holds for a row restart recovery retired (``interrupted``) while its attempt is still its Turn's
current attempt: recovery settles that Turn from its durable projection and only flips the row's status.
Rows their own attempt settled and tasks without a conversation are served from the row exactly as before,
without reading any Turn. Runs on both authorities.
"""
import json

import pytest

from lib.tasks_pkg.durable_chat_replay import (
    _owned_task_replay_record, _snapshot_from_value, load_durable_chat_replay, load_durable_chat_task,
)
from lib.tasks_pkg.event_log import read_event_bounds
from lib.tasks_pkg.manager._persist import _upsert_task_row
from tests.turn_lifecycle.test_task_event_delivery import (
    _discard, _task, storage as storage, turn_service as turn_service,
)

pytestmark = pytest.mark.unit

_TURN_READS = ('turn.get', 'turn.attempt.get')


def _observed_reads(storage, monkeypatch):
    seen = []
    original = storage.client.query

    def query(operation, *args, **kwargs):
        seen.append(operation)
        return original(operation, *args, **kwargs)
    monkeypatch.setattr(storage.client, 'query', query)
    return seen


def _row_only_view(task_id):
    """The public view projected from the task-result row alone (the pre-Turn projection)."""
    value, updated_at_ms = _owned_task_replay_record(
        task_id, user_id=1, include_terminal_payload=True, include_metadata=True)
    return _snapshot_from_value(
        task_id, user_id=1, value=value, record_updated_at_ms=updated_at_ms,
        event_replay=read_event_bounds(task_id), terminal_payload_loaded=True).public_task()


def _same_bytes(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _row(task, *, status, content, conv_id=None, meta=None):
    metadata = {'turnId': task['_turnId'], 'attemptId': task['_attemptId']} if meta is None else meta
    assert _upsert_task_row(
        task, task['convId'] if conv_id is None else conv_id, content=content, thinking='', status=status,
        error_json=None, tr_json=None, meta_json=json.dumps(metadata)) is not False


def test_the_durable_view_of_a_running_conversation_attempt_is_its_turn(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'durable-turn-view')
    try:
        # The row is the birth row: no running checkpoint ever carried the streamed text.
        _row(task, status='pending', content='')
        task['content'], task['thinking'] = 'streamed from the Turn', 'reasoning'
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'streaming'})
        turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        attempt = turn_service.get_attempt(task['_attemptId'], user_id=1)
        # Recording the frame entered the attempt; the row still says 'pending'.
        assert attempt['status'] == 'running'
        row_view = _row_only_view(task['id'])
        assert (row_view['status'], row_view['content']) == ('pending', '')
        view = load_durable_chat_task(task['id'], user_id=1).public_task()
        assert view['content'] == turn['projection']['content'] == 'streamed from the Turn'
        assert view['thinking'] == turn['projection']['thinking'] == 'reasoning'
        assert view['status'] == 'running'
        assert view['convId'] == task['convId'] and view['finished_at'] is None
        assert view['updated_at'] >= row_view['updated_at']
        # The response shape is unchanged: only Turn-owned values differ.
        assert set(view) <= set(row_view) | {
            'usage', 'apiRounds', 'todoState', 'waitingOn', 'modifiedFiles', 'modifiedFileList',
            'model', 'preset', 'thinkingDepth', 'provider_id', 'routeSnapshot'}
        replay = load_durable_chat_replay(task['id'], user_id=1)
        assert replay.status == 'running' and not replay.terminal
    finally:
        _discard(task)


def test_terminal_rows_are_served_from_the_row_without_a_turn_read(turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'durable-terminal-view')
    try:
        task['content'] = 'the Turn moved on'
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'streaming'})
        _row(task, status='done', content='the settled row')
        expected = _row_only_view(task['id'])
        reads = _observed_reads(storage, monkeypatch)
        view = load_durable_chat_task(task['id'], user_id=1).public_task()
        replay = load_durable_chat_replay(task['id'], user_id=1)
        assert (view['status'], view['content']) == ('done', 'the settled row')
        assert _same_bytes(view, expected)
        assert replay.status == 'done'
        assert replay.with_terminal_payload().content == 'the settled row'
        assert not set(reads) & set(_TURN_READS)
    finally:
        _discard(task)


def test_a_restart_interrupted_conversation_attempt_is_served_from_its_turn(turn_service, storage):
    """Restart recovery settles the Turn from its durable projection but only flips the row's status."""
    _, task = _task(turn_service, storage, 'durable-interrupted-view')
    try:
        _row(task, status='pending', content='')
        task['content'], task['thinking'] = 'streamed before the crash', 'reasoning'
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'streaming'})
        assert turn_service.recover_running_attempts() >= 1
        storage.client.command('task_results.recover_running',
                               {'interrupted_reason': 'server_restart'}, None, deadline=30.0)
        turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert turn['currentAttemptId'] == task['_attemptId']
        row_view = _row_only_view(task['id'])
        assert (row_view['status'], row_view['content']) == ('interrupted', '')
        view = load_durable_chat_task(task['id'], user_id=1).public_task()
        assert view['status'] == 'interrupted'
        assert view['content'] == turn['projection']['content'] == 'streamed before the crash'
        assert view['thinking'] == turn['projection']['thinking']
        assert view['finished_at'] == row_view['finished_at']
        replay = load_durable_chat_replay(task['id'], user_id=1)
        assert replay.terminal and replay.status == 'interrupted'
        assert replay.with_terminal_payload().content == 'streamed before the crash'
    finally:
        _discard(task)


def test_tasks_without_a_conversation_are_served_from_the_row_without_a_turn_read(
        turn_service, storage, monkeypatch):
    _, task = _task(turn_service, storage, 'durable-standalone-view')
    try:
        standalone = dict(task, id=task['id'] + '-standalone', convId='')
        for key in ('_turnId', '_attemptId'):
            standalone.pop(key, None)
        _row(standalone, status='running', content='standalone checkpoint', conv_id='', meta={})
        expected = _row_only_view(standalone['id'])
        reads = _observed_reads(storage, monkeypatch)
        view = load_durable_chat_task(standalone['id'], user_id=1).public_task()
        replay = load_durable_chat_replay(standalone['id'], user_id=1)
        assert (view['status'], view['content'], view['convId']) == ('running', 'standalone checkpoint', '')
        assert _same_bytes(view, expected)
        assert replay.status == 'running'
        assert not set(reads) & set(_TURN_READS)
    finally:
        _discard(task)
