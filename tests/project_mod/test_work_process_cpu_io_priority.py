"""Tofu-started work runs at the lowest CPU and I/O priority; the spawning server keeps its own.

Worker builds, tests and evaluations share the host with the API host, the
execution host and TofuDB. At the same priority a burst of work starves the
server's event loop (2026-11-18: 10 LoopWatch stalls and 64-74 s readiness
outages while Worker cargo fleets ran at nice 0). Started work therefore lowers
its own nice value and I/O class to the fixed values declared in
``contracts/resource_policy_v1.json`` (``process_priority.work_process``)
before it runs; descendants inherit both. This is priority only: every
accepted job still starts and runs (docs/RESOURCE_CONTROL.md, "Tofu-started
work yields CPU and I/O to the servers").

An unprivileged process can never lower its nice value again and the pytest
harness already runs at nice 19, so only the I/O class can show a difference
here: the specs move this process to best-effort level 4, which needs no
privilege, and check the started work reads ``idle``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from unittest.mock import Mock

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not os.path.exists('/proc/self/stat'),
                                 reason='per-thread CPU and I/O priority is Linux behavior')]

# ``read`` is a shell builtin, so field 19 of the stat line is the started
# shell's own nice value; ``ionice -p $$`` names the shell and a bare
# ``ionice`` runs as its child.
PROBE = ('read -r s < /proc/self/stat; set -- $s; '
         'echo "own_nice=${19} own_io=$(ionice -p $$) child_io=$(ionice)"')


def _policy() -> dict:
    document = json.loads((ROOT / 'contracts/resource_policy_v1.json').read_text(encoding='utf-8'))
    return document['process_priority']


def _all_threads(ioclass, value=None):
    for thread in psutil.Process().threads():
        try:
            psutil.Process(thread.id).ionice(ioclass, value)
        except psutil.NoSuchProcess:
            pass  # a worker thread that ended since the listing


@pytest.fixture
def server_io_priority():
    """Hold every thread of this process, the stand-in for the server, at best-effort 4."""
    original = psutil.Process().ionice()
    _all_threads(psutil.IOPRIO_CLASS_BE, 4)
    try:
        yield
        assert psutil.Process().ionice().ioclass == psutil.IOPRIO_CLASS_BE, (
            'starting work changed the spawning process')
    finally:
        if original.ioclass == psutil.IOPRIO_CLASS_BE:
            _all_threads(original.ioclass, original.value)
        else:
            _all_threads(original.ioclass)


def _lowered(output: str) -> None:
    match = re.search(r'own_nice=(-?\d+) own_io=(\S+).* child_io=(\S+)', output)
    assert match, output
    work = _policy()['work_process']
    assert int(match.group(1)) == work['nice'], output
    assert match.group(2).rstrip(':') == work['io_class'], output
    assert match.group(3).rstrip(':') == work['io_class'], output


def _command_id(tool_result) -> str:
    return next(word for word in str(tool_result[1]).replace(':', ' ').replace('.', ' ').split()
                if word.startswith('bg_'))


def test_policy_is_a_fixed_hardware_independent_priority_not_a_limit():
    policy = _policy()
    assert policy['class'] == 'fixed'
    # The kernel's extremes: the highest nice value (setpriority(2)) and the
    # idle I/O class (ioprio_set(2)). No count, quota or share is declared.
    assert policy['work_process'] == {'nice': 19, 'io_class': 'idle', 'oom_score_adj': 1000}
    assert set(policy['server_process'].values()) == {'inherited'}


def test_foreground_command_runs_at_the_lowest_priority(server_io_priority, tmp_path):
    from lib.project_mod.run_command import tool_run_command
    _lowered(tool_run_command(str(tmp_path), PROBE))


def test_structured_process_launch_runs_at_the_lowest_priority(server_io_priority, tmp_path):
    from lib.project_mod.run_command import run_command_process
    _lowered(run_command_process(['/bin/sh', '-c', PROBE], command='probe', timeout=60,
                                base=str(tmp_path),
                                child_env={'PATH': os.environ.get('PATH', os.defpath)}))


def test_background_command_runs_at_the_lowest_priority(server_io_priority, tmp_path,
                                                         monkeypatch):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import project
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path / 'records'))
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    task = {'id': 'task-bg-priority', 'convId': '', '_userId': 7, 'config': {}}
    result = project._handle_project_tool(
        task, {}, 'run_command', 'tc-bg', {'command': PROBE, 'background': True}, 1,
        {'toolName': 'run_command', 'toolCallId': 'tc-bg'}, {}, str(tmp_path), True)
    snapshot = background_commands.check_background_command(
        _command_id(result), user_id=7, wait_s=60)
    assert snapshot['status'] == 'completed', snapshot
    _lowered(snapshot['result'])


def test_lowering_keeps_arguments_exit_status_and_launch_failures(server_io_priority, tmp_path):
    from lib.project_mod.run_command import run_command_process
    env = {'PATH': os.environ.get('PATH', os.defpath)}
    exact = run_command_process(['/bin/sh', '-c', 'printf "%s|" "$@"; exit 3', 'sh', '-n', 'a b',
                                 '--', '-c3'], command='printf', timeout=60, base=str(tmp_path),
                                child_env=env)
    assert '-n|a b|--|-c3|' in exact and '[exit code: 3]' in exact, exact
    missing = run_command_process([str(tmp_path / 'absent')], command='absent', timeout=60,
                                  base=str(tmp_path), child_env=env)
    assert 'Error starting command' in missing and '[exit code: -1]' in missing, missing
