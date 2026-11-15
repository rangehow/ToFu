"""Tofu-started work is the kernel's first OOM victim; the spawning server keeps its own.

Under memory exhaustion the kernel kills the process with the highest
``oom_score``. Every process inherits the container's ``oom_score_adj``, so
without this the largest process, the storage engine, dies first while the
commands that caused the pressure survive. Started work raises its own value
to the kernel maximum before it runs; its descendants inherit it, and the
spawning process (the execution host in production, this test process here)
keeps its value (docs/RESOURCE_CONTROL.md, "Tofu-started work is the kernel's
first OOM victim").
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import textwrap
from unittest.mock import Mock

import pytest

from lib.runtime import work_process_priority
from lib.runtime.work_process_priority import OOM_SCORE_ADJ_MAX

SELF = '/proc/self/oom_score_adj'

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not os.path.exists(SELF), reason='Linux oom_score_adj')]

# ``read`` is a shell builtin, so ``own`` is the started shell itself; the
# command substitution runs ``cat`` as its child.
PROBE = 'read own < /proc/self/oom_score_adj; echo "own=$own child=$(cat /proc/self/oom_score_adj)"'
RAISED = (OOM_SCORE_ADJ_MAX, OOM_SCORE_ADJ_MAX)

_MCP_PROBE_SERVER = textwrap.dedent(r'''
    import json
    import sys

    def send(request_id, **body):
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request_id, **body}) + '\n')
        sys.stdout.flush()

    for line in sys.stdin:
        request = json.loads(line)
        method, request_id = request.get('method', ''), request.get('id')
        if request_id is None:
            continue
        if method == 'initialize':
            send(request_id, result={'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                                     'serverInfo': {'name': 'oom-probe', 'version': '1.0'}})
        elif method == 'tools/list':
            send(request_id, result={'tools': [{
                'name': 'oom_score_adj', 'description': 'Report this server process value.',
                'inputSchema': {'type': 'object', 'properties': {}}}]})
        elif method == 'tools/call':
            with open('/proc/self/oom_score_adj', encoding='ascii') as handle:
                value = handle.read().strip()
            send(request_id, result={'content': [{'type': 'text', 'text': value}], 'isError': False})
        else:
            send(request_id, error={'code': -32601, 'message': 'Method not found: ' + method})
''')


def _read_self() -> int:
    with open(SELF, encoding='ascii') as handle:
        return int(handle.read())


def _write_self(value: int) -> None:
    with open(SELF, 'w', encoding='ascii') as handle:
        handle.write(str(value))


@pytest.fixture
def server_priority():
    """Hold this process, the stand-in for the spawning server, below the maximum.

    A test run started by a raised command inherits the maximum. Lowering it
    by one needs no privilege above the container's floor, and a child that
    skipped the raise would then visibly read the lower value.
    """
    original = _read_self()
    if original == OOM_SCORE_ADJ_MAX:
        try:
            _write_self(OOM_SCORE_ADJ_MAX - 1)
        except PermissionError:
            pytest.skip('this process cannot hold its OOM priority below the maximum')
    held = _read_self()
    try:
        yield held
        assert _read_self() == held, 'starting work changed the spawning process'
    finally:
        _write_self(original)


def _values(output: str) -> tuple[int, int]:
    match = re.search(r'own=(-?\d+) child=(-?\d+)', output)
    assert match, output
    return int(match.group(1)), int(match.group(2))


def _child_env() -> dict[str, str]:
    return {'PATH': os.environ.get('PATH', os.defpath)}


def _command_id(tool_result) -> str:
    return next(word for word in str(tool_result[1]).replace(':', ' ').replace('.', ' ').split()
                if word.startswith('bg_'))


def test_foreground_command_and_its_children_are_the_preferred_victim(server_priority, tmp_path):
    from lib.project_mod.run_command import tool_run_command
    output = tool_run_command(str(tmp_path), PROBE)
    assert _values(output) == RAISED, output


def test_interactive_command_is_the_preferred_victim(server_priority, tmp_path):
    from lib.project_mod.run_command import tool_run_command
    output = tool_run_command(str(tmp_path), PROBE, stdin_callback=lambda prompt: None)
    assert _values(output) == RAISED, output


def test_structured_process_launch_is_the_preferred_victim(server_priority, tmp_path):
    from lib.project_mod.run_command import run_command_process
    output = run_command_process(['/bin/sh', '-c', PROBE], command='probe', timeout=60,
                                 base=str(tmp_path), child_env=_child_env())
    assert _values(output) == RAISED, output


def test_background_command_is_the_preferred_victim(server_priority, tmp_path, monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import project
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path / 'records'))
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    task = {'id': 'task-bg-oom', 'convId': '', '_userId': 7, 'config': {}}
    result = project._handle_project_tool(
        task, {}, 'run_command', 'tc-bg', {'command': PROBE, 'background': True}, 1,
        {'toolName': 'run_command', 'toolCallId': 'tc-bg'}, {}, str(tmp_path), True)
    snapshot = background_commands.check_background_command(
        _command_id(result), user_id=7, wait_s=60)
    assert snapshot['status'] == 'completed', snapshot
    assert _values(snapshot['result']) == RAISED, snapshot


def test_standalone_background_command_is_the_preferred_victim(server_priority, tmp_path,
                                                               monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import code_exec
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path / 'records'))
    monkeypatch.setattr(code_exec, '_finalize_tool_round', Mock())
    task = {'id': 'task-bg-standalone-oom', 'convId': '', '_userId': 7}
    result = code_exec._handle_code_exec(
        task, {}, 'run_command', 'tc-bg', {'command': PROBE, 'background': True},
        1, {}, {}, None, False)
    snapshot = background_commands.check_background_command(
        _command_id(result), user_id=7, wait_s=60)
    assert snapshot['status'] == 'completed', snapshot
    assert _values(snapshot['result']) == RAISED, snapshot


def test_scheduled_job_is_the_preferred_victim_and_keeps_launch_errors(server_priority, tmp_path):
    from lib.identity import PrincipalContext
    from lib.scheduler.process_runner import run_bounded_process
    principal = PrincipalContext.user(subject_id='oom-priority-spec', owner_user_id=1,
                                      scopes={'agents:scheduler'})
    script = tmp_path / 'probe.sh'
    script.write_text(f'#!/bin/sh\n{PROBE}\n', encoding='utf-8')
    script.chmod(0o755)

    def run(argv):
        return run_bounded_process(argv, max_runtime=60, job_id='oom-priority-spec',
                                   job_type='test', principal=principal, cwd=str(tmp_path))

    # Evolution criteria pass shlex-split argv: a PATH name or a cwd-relative path.
    by_name, by_relative_path = run(['sh', '-c', PROBE]), run(['./probe.sh'])
    assert by_name.ok and _values(by_name.stdout) == RAISED, by_name
    assert by_relative_path.ok and _values(by_relative_path.stdout) == RAISED, by_relative_path
    # A program the spawn call cannot find stays its launch error, not a shell's exit status.
    missing = run(['tofu-oom-priority-spec-absent-program'])
    assert missing.returncode is None and missing.error, missing


def test_one_job_helper_is_the_preferred_victim(server_priority, tmp_path):
    from lib.runtime.owned_helper_process import run_owned_helper
    output = tmp_path / 'helper.out'
    with open(output, 'wb') as stdout:
        assert run_owned_helper(['/bin/sh', '-c', PROBE], stdin=subprocess.DEVNULL,
                                stdout=stdout) == 0
    assert _values(output.read_text(encoding='utf-8')) == RAISED


def test_stdio_mcp_server_is_the_preferred_victim(server_priority, tmp_path):
    pytest.importorskip('mcp')
    from lib.mcp.client import MCPBridge
    server = tmp_path / 'oom_probe_mcp.py'
    server.write_text(_MCP_PROBE_SERVER, encoding='utf-8')
    bridge = MCPBridge()
    try:
        bridge.connect_server('oomprobe', {'transport': 'stdio', 'command': sys.executable,
                                           'args': [str(server)], 'enabled': True})
        assert bridge.call_tool('mcp__oomprobe__oom_score_adj', {}) == str(OOM_SCORE_ADJ_MAX)
    finally:
        bridge.disconnect_all()


def test_the_raise_keeps_arguments_and_launch_failures(server_priority, tmp_path):
    from lib.project_mod.run_command import run_command_process
    exact = run_command_process(['/bin/sh', '-c', 'printf "%s|" "$@"', 'sh', 'a b', '$HOME', '',
                                 "it's", '-c'], command='printf', timeout=60, base=str(tmp_path),
                                child_env=_child_env())
    assert 'a b|$HOME||it\'s|-c|' in exact, exact
    missing = run_command_process([str(tmp_path / 'absent')], command='absent', timeout=60,
                                  base=str(tmp_path), child_env=_child_env())
    assert 'Error starting command' in missing and '[exit code: -1]' in missing, missing


def test_without_kernel_support_work_runs_unchanged_and_it_is_logged_once(
        server_priority, tmp_path, monkeypatch, caplog):
    from lib.project_mod.run_command import tool_run_command
    monkeypatch.setattr(work_process_priority, '_SELF_OOM_SCORE_ADJ', str(tmp_path / 'absent'))
    monkeypatch.setattr(work_process_priority, '_supported', None)
    with caplog.at_level(logging.WARNING, logger=work_process_priority.__name__):
        first = tool_run_command(str(tmp_path), PROBE)
        second = tool_run_command(str(tmp_path), PROBE)
    assert _values(first) == _values(second) == (server_priority, server_priority)
    warnings = [record for record in caplog.records if record.name == work_process_priority.__name__]
    assert len(warnings) == 1, warnings
    assert 'cannot be raised' in warnings[0].getMessage()
