"""Tests for the background command registry and tool handlers."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.background_commands import (
    MAX_BACKGROUND_COMMANDS,
    check_background_command,
    kill_background_command,
    list_background_commands,
    start_background_command,
    _registry,
    _lock,
)


def _make_task(**overrides):
    task = {
        'id': 'test-task-bg',
        'convId': '',
    }
    task.update(overrides)
    return task


@pytest.fixture(autouse=True)
def _clean_registry():
    with _lock:
        _registry.clear()
    yield
    with _lock:
        _registry.clear()


class TestStartBackgroundCommand:
    def test_returns_command_id(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo hello',
            execute=lambda t: '$ echo hello\nhello\n[exit code: 0]')
        assert cid.startswith('bg_')

    def test_command_completes(self):
        task = _make_task()
        done = threading.Event()

        def _execute(t):
            done.wait(timeout=5)
            return '$ echo done\ndone\n[exit code: 0]'

        cid = start_background_command(
            task=task, command='echo done', execute=_execute)
        assert check_background_command(cid)['status'] == 'running'
        done.set()
        # Wait for the worker thread to finish
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert info['status'] == 'completed'
        assert 'done' in info['result']

    def test_error_in_execute_captured(self):
        task = _make_task()

        def _fail(t):
            raise RuntimeError('boom')

        cid = start_background_command(
            task=task, command='fail', execute=_fail)
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert info['status'] == 'completed'
        assert 'RuntimeError' in info['result']


def test_delivery_failure_is_queryable_separately_from_execution(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.handlers._background_command._queue_completion',
        lambda **kwargs: False)
    cid = start_background_command(
        task=_make_task(convId='conv-delivery', _userId=7),
        command='echo finished', execute=lambda _: 'finished [exit code: 0]')
    for _ in range(50):
        info = check_background_command(cid, user_id=7)
        if info['delivery_status'] == 'failed':
            break
        time.sleep(0.01)
    assert info['status'] == 'completed'
    assert info['delivery_status'] == 'failed'
    assert info['result'] == 'finished [exit code: 0]'
    assert check_background_command(cid, user_id=8) is None


class TestCheckBackgroundCommand:
    def test_unknown_returns_none(self):
        assert check_background_command('bg_nonexistent') is None

    def test_running_status(self):
        task = _make_task()
        blocker = threading.Event()
        cid = start_background_command(
            task=task, command='sleep 60',
            execute=lambda t: blocker.wait(10) or 'done')
        info = check_background_command(cid)
        assert info['status'] == 'running'
        assert info['elapsed_s'] >= 0
        blocker.set()

    def test_completed_has_result(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo test',
            execute=lambda t: '$ echo test\ntest\n[exit code: 0]')
        for _ in range(50):
            info = check_background_command(cid)
            if info['status'] == 'completed':
                break
            time.sleep(0.05)
        assert info['result'] is not None
        assert 'test' in info['result']


class TestKillBackgroundCommand:
    def test_kill_running(self):
        task = _make_task()
        blocker = threading.Event()

        def _execute(t):
            # Simulate the abort-poll pattern
            for _ in range(200):
                if t.get('aborted'):
                    return '$ cmd\n\n[Command aborted by user]\n[exit code: -1]'
                time.sleep(0.01)
            return '$ cmd\n\n[exit code: 0]'

        cid = start_background_command(
            task=task, command='long cmd', execute=_execute)
        assert kill_background_command(cid) is True
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert 'aborted' in info['result'].lower()

    def test_kill_unknown(self):
        assert kill_background_command('bg_nonexistent') is False

    def test_kill_already_finished(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo fast',
            execute=lambda t: 'done')
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        assert kill_background_command(cid) is False


class TestListBackgroundCommands:
    def test_empty(self):
        assert list_background_commands() == []

    def test_lists_running_and_completed(self):
        task = _make_task()
        blocker = threading.Event()
        cid1 = start_background_command(
            task=task, command='fast',
            execute=lambda t: 'done')
        cid2 = start_background_command(
            task=task, command='slow',
            execute=lambda t: blocker.wait(10) or 'done')
        for _ in range(50):
            if check_background_command(cid1)['status'] == 'completed':
                break
            time.sleep(0.05)
        cmds = list_background_commands()
        assert len(cmds) == 2
        statuses = {c['command_id']: c['status'] for c in cmds}
        assert statuses[cid1] == 'completed'
        assert statuses[cid2] == 'running'
        blocker.set()


class TestCapacity:
    def test_evicts_finished_when_full(self):
        task = _make_task()
        # Fill registry with completed commands
        for i in range(MAX_BACKGROUND_COMMANDS):
            start_background_command(
                task=task, command=f'echo {i}',
                execute=lambda t: 'done')
        # Wait for all to complete
        with _lock:
            entries = list(_registry.values())
        assert all(entry['done'].wait(3) for entry in entries)
        # Should not raise — evicts finished entries
        cid = start_background_command(
            task=task, command='echo overflow',
            execute=lambda t: 'done')
        assert cid.startswith('bg_')


class TestSchema:
    def test_run_command_has_background_param(self):
        from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
        props = PROJECT_TOOL_RUN_COMMAND['function']['parameters'][
            'properties']
        assert 'background' in props
        assert props['background']['type'] == 'boolean'

    def test_check_background_schema(self):
        from lib.tools.project import PROJECT_TOOL_CHECK_BACKGROUND
        assert PROJECT_TOOL_CHECK_BACKGROUND['function'][
            'name'] == 'check_background_command'
        assert 'command_id' in PROJECT_TOOL_CHECK_BACKGROUND[
            'function']['parameters']['required']

    def test_kill_background_schema(self):
        from lib.tools.project import PROJECT_TOOL_KILL_BACKGROUND
        assert PROJECT_TOOL_KILL_BACKGROUND['function'][
            'name'] == 'kill_background_command'
        assert 'command_id' in PROJECT_TOOL_KILL_BACKGROUND[
            'function']['parameters']['required']

    def test_tools_in_project_names(self):
        from lib.tools.project import PROJECT_TOOL_NAMES
        assert 'check_background_command' in PROJECT_TOOL_NAMES
        assert 'kill_background_command' in PROJECT_TOOL_NAMES

    def test_tools_in_unified_list(self):
        from lib.tools.project import PROJECT_TOOLS_UNIFIED
        names = [t['function']['name'] for t in PROJECT_TOOLS_UNIFIED]
        assert 'check_background_command' in names
        assert 'kill_background_command' in names


class TestLifecycle:
    def test_fast_completion_uses_existing_delivery_once(self):
        delivered = threading.Event()
        calls = []

        def delivery(**kwargs):
            calls.append(kwargs)
            delivered.set()

        with patch('lib.tasks_pkg.handlers._background_command._queue_completion',
                   side_effect=delivery):
            cid = start_background_command(
                task=_make_task(convId='conv-owner', _userId=7),
                config={'_turnId': 'old', 'model': 'test'},
                command='fast', execute=lambda t: 'result')
            assert delivered.wait(3)
        assert len(calls) == 1
        assert calls[0]['command_id'] == cid
        assert calls[0]['task'] == {'convId': 'conv-owner', '_userId': 7}
        assert calls[0]['config'] == {'model': 'test'}
        assert check_background_command(cid, user_id=7)['result'] == 'result'
        assert check_background_command(cid) is None
        assert check_background_command(cid, user_id=8) is None
        assert list_background_commands(user_id=8) == []
        with _lock:
            assert _registry[cid]['task'] is None

    def test_owner_and_stop_isolation_without_parent_objects(self):
        ready, release = threading.Event(), threading.Event()
        observed = {}
        parent = _make_task(_userId=7, aborted=True, _subprocess_pid=123,
                            _subprocess_pgid=123, _cmd_interrupt='stop',
                            messages=[object()], toolRounds=[object()])

        def execute(task):
            observed.update(task)
            ready.set()
            release.wait(3)
            return str(task.get('aborted'))

        cid = start_background_command(task=parent, command='wait', execute=execute)
        try:
            assert ready.wait(3)
            assert observed['aborted'] is False
            assert not {'messages', 'toolRounds', '_subprocess_pid',
                        '_subprocess_pgid', '_cmd_interrupt'} & observed.keys()
            assert not kill_background_command(cid, user_id=8)
            assert not kill_background_command(cid)
            assert kill_background_command(cid, user_id=7)
            assert parent['_subprocess_pid'] == 123
        finally:
            release.set()
        assert _registry[cid]['done'].wait(3)
        assert check_background_command(cid, user_id=7)['result'] == 'True'

    def test_thread_start_failure_releases_capacity(self):
        with patch('lib.tasks_pkg.background_commands.threading.Thread.start',
                   side_effect=RuntimeError('cannot start')):
            with pytest.raises(RuntimeError, match='cannot start'):
                start_background_command(task={}, command='never',
                                         execute=lambda t: pytest.fail('executed'))
        assert list_background_commands() == []

    def test_running_capacity_not_evicted(self):
        release = threading.Event()
        with patch('lib.tasks_pkg.background_commands.MAX_BACKGROUND_COMMANDS', 1):
            cid = start_background_command(task={}, command='wait',
                                           execute=lambda t: release.wait(3))
            try:
                with pytest.raises(RuntimeError, match='capacity'):
                    start_background_command(task={}, command='overflow',
                                             execute=lambda t: 'done')
                assert check_background_command(cid)['status'] == 'running'
            finally:
                release.set()
            assert _registry[cid]['done'].wait(3)
            next_id = start_background_command(task={}, command='next',
                                               execute=lambda t: 'done')
            assert check_background_command(cid) is None
            assert _registry[next_id]['done'].wait(3)

    def test_failure_stores_text_not_traceback(self):
        def execute(task):
            raise ValueError('failed')
        cid = start_background_command(task={}, command='fail', execute=execute)
        assert _registry[cid]['done'].wait(3)
        assert _registry[cid]['state']['error'] == 'ValueError: failed'
