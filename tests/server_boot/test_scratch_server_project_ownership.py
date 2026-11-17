"""A scratch server never serves a project another live server owns.

2026-11-13: a scratch preview server followed the tests/AGENTS.md recipe (TOFU_RUN_SERVER=1, a
scratch TOFU_DATA_DIR, a free port) but inherited TOFU_PROJECT_PATH from the Tofu shell that
launched it: the production project root. Its project-scoped writes would have landed in the
production checkout. server.py now refuses, for a TOFU_RUN_SERVER start, a project whose own
server lock (``<project>/data/.server.lock``) another live server holds, with a typed error that
names that server (lib/server_boot/lock.py, ``refuse_project_owned_by_another_server``).

The "live server" here is a scratch process whose command line names ``server.py``, the identity
``_pid_is_live_server`` checks; nothing reads or touches the real production lock.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.server_boot import lock

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def live_owner(tmp_path):
    """A project whose server lock a live (stand-in) server holds: ``(project, pid)``."""
    script = tmp_path / 'owner' / 'server.py'
    script.parent.mkdir()
    script.write_text('import time\ntime.sleep(300)\n', encoding='utf-8')
    owner = subprocess.Popen([sys.executable, str(script)])
    project = tmp_path / 'live-project'
    (project / 'data').mkdir(parents=True)
    (project / 'data' / '.server.lock').write_text(f'{owner.pid}@{socket.gethostname()}\n', encoding='utf-8')
    deadline = time.monotonic() + 30
    while b'server.py' not in Path(f'/proc/{owner.pid}/cmdline').read_bytes() and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield project, owner.pid
    finally:
        owner.kill()
        owner.wait()


def _scratch_lock(tmp_path) -> str:
    return str(tmp_path / 'scratch' / 'data' / '.server.lock')


def test_a_project_another_live_server_owns_is_refused_naming_that_server(tmp_path, live_owner):
    project, pid = live_owner
    with pytest.raises(lock.ProjectOwnedByAnotherServer) as refused:
        lock.refuse_project_owned_by_another_server(str(project), _scratch_lock(tmp_path))
    assert (refused.value.pid, refused.value.host) == (pid, socket.gethostname())
    assert 'server.py' in refused.value.cmdline
    assert f'{pid}@{socket.gethostname()}' in str(refused.value) and str(project) in str(refused.value)


def test_a_project_owned_on_another_host_is_refused(tmp_path):
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'data' / '.server.lock').write_text('4242@another-host\n', encoding='utf-8')
    with pytest.raises(lock.ProjectOwnedByAnotherServer, match='4242@another-host'):
        lock.refuse_project_owned_by_another_server(str(project), _scratch_lock(tmp_path))


def test_a_free_project_and_the_server_own_lock_are_allowed(tmp_path, live_owner):
    project, _ = live_owner
    free = tmp_path / 'free-project'
    free.mkdir()
    assert lock.refuse_project_owned_by_another_server(str(free), _scratch_lock(tmp_path)) is None  # no lock file
    stale = tmp_path / 'stale-project'
    (stale / 'data').mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    (stale / 'data' / '.server.lock').write_text(f'{dead.pid}@{socket.gethostname()}\n', encoding='utf-8')
    assert lock.refuse_project_owned_by_another_server(str(stale), _scratch_lock(tmp_path)) is None  # a dead pid owns nothing
    # The project's own server: its instance lock is the project's lock and already serializes it.
    assert lock.refuse_project_owned_by_another_server(str(project), str(project / 'data' / '.server.lock')) is None
    mine = tmp_path / 'mine'
    (mine / 'data').mkdir(parents=True)
    (mine / 'data' / '.server.lock').write_text(f'{os.getpid()}@{socket.gethostname()}\n', encoding='utf-8')
    assert lock.refuse_project_owned_by_another_server(str(mine), _scratch_lock(tmp_path)) is None  # a re-exec keeps its pid


def test_a_scratch_server_that_inherited_a_live_project_refuses_to_start(tmp_path, live_owner):
    # The recipe's scratch server, launched from a shell that exports another server's project.
    project, pid = live_owner
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    environment = {**os.environ, 'TOFU_RUN_SERVER': '1', 'TOFU_DATA_DIR': str(tmp_path / 'scratch'),
                   'TOFU_HEARTBEAT_DIR': str(tmp_path / 'heartbeat'), 'TOFU_PROJECT_PATH': str(project)}
    server = subprocess.Popen([sys.executable, str(ROOT / 'server.py'), '--port', str(port)], cwd=tmp_path,
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True)
    try:
        _, stderr = server.communicate(timeout=180)
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait()
    assert server.returncode == 1, stderr[-3000:]
    assert f'TOFU_PROJECT_PATH={project} is owned by the live server {pid}@{socket.gethostname()}' in stderr
    assert not (project / 'data' / 'tofu.db').exists() and sorted(os.listdir(project / 'data')) == ['.server.lock']
