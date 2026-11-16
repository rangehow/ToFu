"""Sampled running checkpoints write only where no other durable home exists (executable specification).

A running task's sampled checkpoint copies its progress at each round end. A conversation attempt's Turn already
commits that progress through the ordered event bridge, and every reader of its running or restart-retired row
reads that Turn (``tests/tasks_pkg/test_durable_chat_task_turn_view.py``), so a conversation attempt writes no
sampled task-result row. A task without a conversation has no other durable home: it writes one sampled row per
round, at the round end. The provider boundary writes no row for any task; like a conversation attempt's round
end, it only refreshes the in-memory segment mirror the Turn projection copies on its next event. The recovery
boundary (``code_exec``) still writes its row before code runs.
"""
import threading
import time

import pytest

from lib.tasks_pkg.manager import _sync
from lib.tasks_pkg.segments import derive_content, derive_tool_rounds

pytestmark = pytest.mark.unit


def _task(*, conversation):
    task = {
        'id': 'sampled-conversation' if conversation else 'sampled-standalone',
        'convId': 'conv-sampled' if conversation else '',
        'content': '', 'thinking': '', 'toolRounds': [], 'aborted': False, 'status': 'running',
        '_userId': 1, 'created_at': time.time(), 'config': {},
        'content_lock': threading.Lock(), 'events': [], 'events_lock': threading.Lock(),
    }
    if conversation:
        task.update(_turnId='turn-sampled', _attemptId='attempt-sampled')
    return task


def _row_writes(monkeypatch):
    writes = []

    def upsert(task, conversation_id, **payload):
        writes.append((task['id'], payload['status']))
        return True
    monkeypatch.setattr(_sync, '_upsert_task_row', upsert)
    return writes


def _stream_one_answer(monkeypatch, task):
    import lib.tasks_pkg.manager as manager
    import lib.tasks_pkg.manager._stream as stream

    monkeypatch.setattr(stream, 'append_event', lambda task, event: task['events'].append(event))

    def dispatch(body, *, on_content=None, **kwargs):
        on_content('hello ')
        on_content('world')
        return {'content': 'hello world', 'tool_calls': []}, 'stop', {'completion_tokens': 2}
    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
    manager.stream_llm_response(task, {'model': 'test-model'})


def _close_round(monkeypatch, task, round_num):
    from lib.tasks_pkg.orchestrator import _round_checkpoint

    events = []
    monkeypatch.setattr(_round_checkpoint, 'append_event', lambda task, event: events.append(event))
    _round_checkpoint.run_round_checkpoint_and_close(task, round_num=round_num, tid='sampled')
    return events


def _finished_tool_round(round_num):
    return {'roundNum': round_num, 'llmRound': round_num, 'toolCallId': f'call-{round_num}',
            'toolName': 'read_files', 'toolArgs': {'path': 'a.py'}, 'toolContent': 'body',
            'status': 'done', 'assistantContent': f'reading {round_num}'}


def test_a_conversation_attempt_writes_no_row_at_its_provider_boundary(monkeypatch):
    writes = _row_writes(monkeypatch)
    task = _task(conversation=True)
    _stream_one_answer(monkeypatch, task)
    assert task['content'] == 'hello world'
    assert writes == []
    assert derive_content(task['segments']) == 'hello world'


def test_a_task_without_a_conversation_writes_no_row_at_its_provider_boundary(monkeypatch):
    writes = _row_writes(monkeypatch)
    task = _task(conversation=False)
    _stream_one_answer(monkeypatch, task)
    assert task['content'] == 'hello world'
    assert writes == []
    assert derive_content(task['segments']) == 'hello world'


def test_a_task_without_a_conversation_writes_one_sampled_row_per_round(monkeypatch):
    import lib.tasks_pkg.manager as manager
    import lib.tasks_pkg.manager._stream as stream

    writes = _row_writes(monkeypatch)
    task = _task(conversation=False)
    monkeypatch.setattr(stream, 'append_event', lambda task, event: task['events'].append(event))
    for round_num in range(3):
        def dispatch(body, *, on_content=None, round_num=round_num, **kwargs):
            on_content(f'reading {round_num}')
            call = {'id': f'call-{round_num}', 'type': 'function',
                    'function': {'name': 'read_files', 'arguments': '{"path": "a.py"}'}}
            return {'content': f'reading {round_num}', 'tool_calls': [call]}, 'tool_use', {}
        monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
        manager.stream_llm_response(task, {'model': 'test-model'})
        task['toolRounds'].append(_finished_tool_round(round_num))
        task['content'] = ''
        _close_round(monkeypatch, task, round_num)
    assert writes == [(task['id'], 'running')] * 3


def test_a_conversation_attempt_writes_no_row_at_its_round_end(monkeypatch):
    writes = _row_writes(monkeypatch)
    task = _task(conversation=True)
    task['toolRounds'] = [_finished_tool_round(0)]
    events = _close_round(monkeypatch, task, 0)
    assert writes == []
    assert [event['type'] for event in events] == ['round_end']
    assert [row['toolCallId'] for row in derive_tool_rounds(task['segments'])] == ['call-0']


def test_a_task_without_a_conversation_checkpoints_at_every_round_boundary(monkeypatch):
    writes = _row_writes(monkeypatch)
    task = _task(conversation=False)
    for round_num in range(3):
        task['toolRounds'].append(_finished_tool_round(round_num))
        _close_round(monkeypatch, task, round_num)
    assert writes == [(task['id'], 'running')] * 3


def test_a_conversation_attempt_keeps_its_recovery_boundary(monkeypatch):
    writes = _row_writes(monkeypatch)
    task = _task(conversation=True)
    task['content'] = 'before the code runs'
    assert _sync.checkpoint_task_partial(task, force=True) is True
    assert writes == [(task['id'], 'running')]
