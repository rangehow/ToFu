"""Every lane that cuts a tool call off names who stopped it.

2026-11-11: during the 18:26 server restart, poll, await_project_workers and
search_tools returned "Task aborted by user." to a Coordinator although nobody
pressed Stop. ``turn_verdict.stopped_tool_result`` maps the recorded
``_abort_reason``; these specs pin the lanes the pipeline spec in
``test_system_abort_cause.py`` does not reach.
"""
from __future__ import annotations

import pytest

from lib.turn_verdict import RESTART_CONTINUE_GUIDANCE, stopped_tool_result

pytestmark = pytest.mark.unit

_RESTART = f'Task interrupted by server restart. {RESTART_CONTINUE_GUIDANCE}'
_USER = 'Task aborted by user.'


@pytest.mark.parametrize(('reason', 'cause'), [
    ('server_shutdown', 'server restart'),
    ('restart_recovery', 'server restart'),
    ('dispatch_grant_revoked', 'project dispatch authority'),
    ('worker_lease_lost', 'storage authority fence'),
    ('stuck_no_progress', 'stuck-task reaper'),
])
def test_system_stop_names_its_cause(reason, cause):
    text = stopped_tool_result(reason)
    assert text.startswith(f'Task interrupted by {cause}')
    assert 'user' not in text


@pytest.mark.parametrize('reason', ['', None, 'user_abort', 'human_stop'])
def test_person_stop_keeps_its_wording(reason):
    assert stopped_tool_result(reason) == _USER


def _task(reason):
    task = {'id': 'abort-reason-task', 'convId': 'cv-abort-reason',
            'status': 'running', 'aborted': True, 'model': 'test-model',
            '_userId': 1}
    if reason:
        task['_abort_reason'] = reason
    return task


@pytest.mark.parametrize(('reason', 'expected'), [
    ('server_shutdown', _RESTART), (None, _USER)])
def test_executor_skip_reports_the_recorded_cause(reason, expected):
    from lib.tasks_pkg.executor._execute import _execute_admitted_tool_one
    call_id, text, is_search = _execute_admitted_tool_one(
        _task(reason), {}, 'poll', 'tc-poll', {}, 1, {}, {}, None, False, [])
    assert (call_id, text, is_search) == ('tc-poll', expected, False)


@pytest.mark.parametrize(('reason', 'expected'), [
    ('server_shutdown', _RESTART), (None, _USER)])
def test_streaming_prefetch_skip_reports_the_recorded_cause(reason, expected):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    task = {'id': 'abort-reason-stream', 'aborted': True}
    if reason:
        task['_abort_reason'] = reason
    accumulator = StreamingToolAccumulator(task, project_path='/tmp')
    try:
        result = accumulator._execute_one('read_files', {'path': 'a.py'})
    finally:
        accumulator.close(wait=True)
    assert str(result) == expected


@pytest.mark.parametrize(('reason', 'expected'), [
    ('server_shutdown',
     'Task interrupted by server restart before the client returned a result.'),
    (None, 'Task aborted by user before the client returned a result.')])
def test_client_tool_handoff_reports_the_recorded_cause(reason, expected):
    from lib.tools.tool_env import request_client_tool_result
    content, is_error = request_client_tool_result(
        'call-client', task=_task(reason), timeout=5)
    assert (content, is_error) == (expected, True)
