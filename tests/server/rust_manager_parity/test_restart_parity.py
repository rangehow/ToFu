"""Restart and ownership parity beyond the happy path, for both managers.

- A replacement that dies after the commit point is retried until a worker serves.
- Deferred restarts acknowledge first and admit one pending request per project.
- ``/reload`` refuses a fingerprint that is not on disk and is a no-op when current.
- A manager whose declared owner exits retires its worker and itself.
- A worker survives its manager: a successor manager of EITHER implementation
  adopts it from the shared state file (the cutover in both directions).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from supervisor_protocol import supervisor_source_fingerprint
from tests.server.rust_manager_parity.conftest import (
    REPO, ManagerHarness, _launch_argv, _scrubbed_environment, free_port, pid_alive, wait_for,
)
from tests.server.rust_manager_parity.fake_project import build_project
from tests.support.owned_processes import processes_under, retire_processes_under

pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]


def test_a_replacement_that_dies_after_stop_is_retried_until_one_serves(manager):
    first = manager.start_serving()
    (manager.project / 'worker-crash-once').write_text('')
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    serving = manager.wait_serving(other_than=first['pid'])
    assert any(line.startswith('worker-crash ') for line in manager.events())
    log = manager.log_text()[since:]
    assert 'worker failure 1/' in log
    assert serving['pid'] != first['pid']


def test_deferred_restart_acknowledges_first_and_admits_one_pending_request(manager):
    first = manager.start_serving()
    body = {'projectPath': str(manager.project), 'source': 'application-restart'}

    code, accepted = manager.post('/restart-deferred', body)
    again_code, again = manager.post('/restart-deferred', body)

    assert code == 202 and accepted.get('accepted') is True and accepted.get('waitsForQuiescence') is True
    assert again_code == 409 and again.get('accepted') is False
    manager.wait_serving(other_than=first['pid'])
    manager.wait_log('deferred worker restart accepted')


def test_reload_refuses_a_foreign_fingerprint_and_is_a_noop_when_current(manager):
    code, refused = manager.post('/reload', {'projectPath': str(manager.project),
                                             'expectedFingerprint': '0' * 64, 'source': 'parity'})
    current = supervisor_source_fingerprint(str(REPO))
    noop_code, noop = manager.post('/reload', {'projectPath': str(manager.project),
                                               'expectedFingerprint': current, 'source': 'parity'})

    assert code == 409 and refused['diskFingerprint'] == current
    assert noop_code == 200 and noop == {'ok': True, 'reloading': False, 'sourceFingerprint': current}
    assert manager.get('/health')[1]['sourceFingerprint'] == current


def _launch(impl: str, project: Path, port: int, worker_port: int, owner_pid: int, tmp_path: Path):
    environment = {
        **_scrubbed_environment(tmp_path),
        'TOFU_SUPERVISOR_PROJECTS': str(project), 'TOFU_SUPERVISOR_PORT': str(port),
        'TOFU_SUPERVISOR_HOST': '127.0.0.1', 'TOFU_SUPERVISOR_PYTHON': sys.executable,
        'TOFU_SUPERVISOR_OWNER_PID': str(owner_pid), 'PORT': str(worker_port),
        'TOFU_MANAGER_INTERVAL': '0.2', 'TOFU_HEARTBEAT_DIR': str(tmp_path / 'hb'),
    }
    with (project / 'logs' / 'server-manager.log').open('ab') as log:
        process = subprocess.Popen(_launch_argv(impl), cwd=REPO, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    harness = ManagerHarness(impl, project, port, worker_port, process)
    wait_for(lambda: _healthy(harness), f'{impl} manager /health')
    return harness


def _healthy(harness: ManagerHarness):
    if harness.process.poll() is not None:
        raise AssertionError(f'{harness.impl} manager exited:\n{harness.log_text()[-3000:]}')
    try:
        code, payload = harness.get('/health', timeout=5)
    except OSError:
        return None
    return code == 200 and payload.get('ok') is True


def test_a_manager_whose_owner_exits_retires_its_worker_and_itself(manager_impl, tmp_path):
    project = build_project(tmp_path)
    owner = subprocess.Popen(['sleep', '600'], start_new_session=True)
    try:
        harness = _launch(manager_impl, project, free_port(), free_port(), owner.pid, tmp_path)
        serving = harness.start_serving()

        owner.send_signal(signal.SIGTERM)
        owner.wait()

        harness.process.wait(timeout=60)
        wait_for(lambda: not pid_alive(serving['pid']), 'the owned worker to stop')
        assert harness.state().get('desired') == 'stopped'
    finally:
        if owner.poll() is None:
            owner.kill()
        assert retire_processes_under(tmp_path) == []


@pytest.mark.parametrize('successor', ['python', 'rust'])
def test_a_successor_manager_adopts_the_live_worker(manager_impl, successor, tmp_path):
    # Resolve both implementations before starting anything: a skip after the
    # first launch would strand its live worker, which an exiting manager
    # deliberately keeps running.
    _launch_argv(manager_impl)
    _launch_argv(successor)
    project = build_project(tmp_path)
    port, worker_port = free_port(), free_port()
    first = second = None
    try:
        first = _launch(manager_impl, project, port, worker_port, os.getpid(), tmp_path)
        serving = first.start_serving()
        first.process.send_signal(signal.SIGTERM)
        first.process.wait(timeout=60)
        assert pid_alive(serving['pid']), 'a manager exit never stops its worker'

        second = _launch(successor, project, port, worker_port, os.getpid(), tmp_path)
        adopted = second.wait_serving()

        assert adopted['pid'] == serving['pid']
        assert second.state().get('desired') == 'running'
        code, stopped = second.control('stop')
        assert code == 200 and stopped.get('ok') is True, (code, stopped)
        wait_for(lambda: not pid_alive(serving['pid']), 'the adopted worker to stop')
    finally:
        for harness in (first, second):
            if harness is not None and harness.process.poll() is None:
                harness.process.send_signal(signal.SIGTERM)
                harness.process.wait(timeout=60)
        assert retire_processes_under(tmp_path) == []


def test_the_leak_detector_fires_for_a_genuine_leak(tmp_path):
    """The teardown assertion is not vacuous: a detached process under tmp_path is found."""
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                               cwd=tmp_path, start_new_session=True)
    try:
        assert [found['pid'] for found in processes_under(tmp_path)] == [process.pid]
        leftovers = retire_processes_under(tmp_path)
        assert [found['pid'] for found in leftovers] == [process.pid]
        with pytest.raises(AssertionError):
            assert leftovers == []
        assert retire_processes_under(tmp_path) == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
