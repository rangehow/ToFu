"""Manager policy black-box, against both managers.

Covers the manager side of test_server_manager.py, test_server_native_exit.py,
test_engine_certification_gate.py, test_manager_state_replace_window.py,
test_loopback_channels_disable_nagle.py and test_native_storage_service.py.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for
from tests.server.rust_manager_parity.test_restart_parity import _launch

pytestmark = pytest.mark.api


@pytest.fixture
def native_storage() -> bool:
    return True


@pytest.fixture
def manager_environment(parity_shim) -> dict[str, str]:
    return {'LD_PRELOAD': parity_shim, 'TOFU_PARITY_NODELAY_LOG': '{project}/nodelay.log',
            'TOFU_MANAGER_MAX_FAILURES': '2'}


def ready_storage(manager) -> dict:
    return wait_for(lambda: manager.storage_record().get('state') == 'ready' and manager.storage_record(),
                    'a ready storage host')


def stop_quietly(harness) -> None:
    if harness.process.poll() is None:
        try:
            harness.control('stop')
        finally:
            harness.process.send_signal(signal.SIGTERM)
            harness.process.wait(timeout=60)


# --- test_loopback_channels_disable_nagle.py ------------------------------

def test_manager_control_connections_disable_nagle(manager):
    for _ in range(3):
        assert manager.get('/health')[0] == 200
    lines = wait_for(lambda: (manager.project / 'nodelay.log').exists()
                     and len((manager.project / 'nodelay.log').read_text().split()) >= 3
                     and (manager.project / 'nodelay.log').read_text().split(),
                     'the accepted control sockets to close')
    assert set(lines) == {'nodelay=1'}, lines


# --- test_manager_state_replace_window.py ---------------------------------

def test_the_manager_publishes_its_state_under_the_store_lock(manager):
    manager.start_serving()
    lock_path = manager.state_path.with_name(manager.state_path.name + '.lock')
    assert lock_path.exists()
    before = manager.state_path.read_bytes()
    replies = []
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        stopper = threading.Thread(target=lambda: replies.append(manager.control('stop')), daemon=True)
        stopper.start()
        time.sleep(1.5)
        assert manager.state_path.read_bytes() == before, 'the state was replaced without its lock'
        assert not any(name.endswith('.tmp') and name.startswith(manager.state_path.name)
                       for name in os.listdir(manager.state_path.parent))
    finally:
        os.close(descriptor)
    stopper.join(timeout=60)
    assert replies and replies[0][1].get('ok') is True, replies
    assert manager.state_path.read_bytes() != before
    assert manager.state()['desired'] == 'stopped'


# --- test_engine_certification_gate.py ------------------------------------

def test_automatic_spawns_never_prepare_the_storage_engine_and_explicit_start_does(manager):
    first = manager.start_serving()
    assert manager.prep_calls() == ['all']

    os.kill(first['pid'], signal.SIGKILL)

    manager.wait_serving(other_than=first['pid'])
    assert manager.prep_calls() == ['all', 'frontend']


def test_a_refused_engine_refuses_the_restart_and_never_retains_storage(manager):
    first = manager.start_serving()
    host = ready_storage(manager)
    (manager.project / 'prep-engine-decision').write_text(json.dumps({
        'decision': 'refused', 'message': 'HEAD is uncertified and the deployed engine cannot serve it'}))

    code, result = manager.control('restart')

    assert result.get('ok') is False, (code, result)
    assert 'uncertified' in result['message']
    current = manager.wait_serving()
    assert current['pid'] == first['pid']
    assert not manager.state().get('storageRetention')
    assert 'storageRetained' not in manager.last_restart()
    assert manager.storage_record()['serviceId'] == host['serviceId']


def test_an_engine_failure_retains_the_ready_storage_host_and_replaces_the_application(manager):
    first = manager.start_serving()
    host = ready_storage(manager)
    (manager.project / 'prep-engine-fail').write_text('cargo build failed: injected')

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    assert manager.storage_record()['serviceId'] == host['serviceId']
    assert manager.state()['storageRetention']['reason']
    assert manager.last_restart()['storageRetained']


# --- test_server_native_exit.py -------------------------------------------

def test_a_fatal_signal_is_recorded_as_the_worker_exit_and_respawned(manager):
    first = manager.start_serving()

    os.kill(first['pid'], signal.SIGSEGV)

    manager.wait_serving(other_than=first['pid'])
    state = manager.state()
    assert state['lastExitCause'] == 'fatal_signal'
    assert 'SIGSEGV' in state['lastFailureReason'] and str(first['pid']) in state['lastFailureReason']
    exit_record = state['lastWorkerProcessExit']
    assert exit_record['pid'] == first['pid'] and exit_record['signal'] == signal.SIGSEGV
    assert not Path(f'/proc/{first["pid"]}').exists(), 'the exited worker was never reaped'


# --- test_server_manager.py -----------------------------------------------

def test_a_worker_that_keeps_crashing_latches_crashloop_until_an_explicit_start(manager):
    (manager.project / 'worker-crash-always').write_text('')
    code, started = manager.control('start')
    assert code == 200, (code, started)
    wait_for(lambda: manager.status()['observed'] == 'crashloop', 'the failure budget to latch crashloop',
             timeout=120)
    assert manager.state()['consecutiveFailures'] >= 2

    (manager.project / 'worker-crash-always').unlink()
    manager.start_serving()
    assert manager.status()['observed'] == 'running'


def test_a_manual_stop_persists_across_a_manager_replacement(manager, tmp_path):
    manager.start_serving()
    code, stopped = manager.control('stop')
    assert code == 200 and stopped.get('ok') is True
    manager.process.send_signal(signal.SIGTERM)
    manager.process.wait(timeout=60)

    successor = _launch(manager.impl, manager.project, manager.port, manager.worker_port, os.getpid(), tmp_path)
    try:
        time.sleep(2.0)
        status = successor.status(probe=True)
        assert status['desired'] == 'stopped' and status['observed'] == 'stopped'
        assert sum(line.startswith('worker-start') for line in successor.events()) == 1
    finally:
        stop_quietly(successor)


def test_a_foreign_listener_on_the_worker_port_fails_closed(manager):
    foreign = socket.socket()
    foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    foreign.bind(('127.0.0.1', manager.worker_port))
    foreign.listen()
    try:
        code, started = manager.control('start')
        assert started.get('ok') is False, (code, started)
        assert str(manager.worker_port) in started['message']
        assert manager.lock_pid() is None
        assert not any(line.startswith('worker-start') for line in manager.events())
    finally:
        foreign.close()


def test_project_dotenv_values_reach_the_worker_and_its_hosts(manager, tmp_path):
    relocated = tmp_path / 'relocated' / 'data'
    (manager.project / '.env').write_text(f'TOFU_AGENT_QUEUE_CAPACITY=7\nTOFU_DATA_DIR={relocated}\n')

    code, started = manager.control('start')
    assert code == 200 and started.get('ok') is True, (code, started)
    wait_for(lambda: manager.status(probe=True).get('ready') is True, 'a ready worker')
    ready_storage(manager)

    worker = json.loads((manager.project / 'env-worker.json').read_text())
    assert worker['TOFU_AGENT_QUEUE_CAPACITY'] == '7'
    assert worker['TOFU_DATA_DIR'] == str(relocated)
    assert json.loads((manager.project / 'env-storage.json').read_text())['TOFU_DATA_DIR'] == str(relocated)
    assert (relocated / '.server.lock').exists()


# --- test_native_storage_service.py ---------------------------------------

def test_manager_death_drains_its_storage_host(manager):
    manager.start_serving()
    host = ready_storage(manager)

    manager.process.send_signal(signal.SIGKILL)
    manager.process.wait(timeout=30)

    wait_for(lambda: f'host-owner-gone {host["serviceId"]}' in manager.events(), 'the host to see EOF')
    wait_for(lambda: not pid_alive(host['pid']), 'the orphaned host to exit')
    # The worker is independently owned: it survives its manager.
    worker = manager.lock_pid()
    assert worker and pid_alive(worker)
    os.kill(worker, signal.SIGTERM)
    wait_for(lambda: not pid_alive(worker), 'the orphaned worker to exit')


@pytest.mark.parametrize('generation', ['unchanged', 'changed'])
def test_a_successor_manager_restores_storage_for_the_preserved_worker(manager, tmp_path, generation):
    """2026-11-05: manager death drained its host; the successor must not strand the worker."""
    if generation == 'unchanged':
        (manager.project / 'storage-plan-fixed').write_text('')
    first = manager.start_serving()
    host = ready_storage(manager)
    manager.process.send_signal(signal.SIGKILL)
    manager.process.wait(timeout=30)
    wait_for(lambda: not pid_alive(host['pid']), 'the orphaned host to exit')
    assert manager.worker_health_pid() == first['pid']

    successor = _launch(manager.impl, manager.project, manager.port, manager.worker_port, os.getpid(), tmp_path)
    try:
        restored = wait_for(lambda: (record := successor.storage_record()).get('state') == 'ready'
                            and record.get('serviceId') != host['serviceId'] and record,
                            'a restored storage host')
        assert restored['managerPid'] == successor.process.pid
        current = successor.wait_serving()
        if generation == 'unchanged':
            assert current['pid'] == first['pid']
            assert restored['plan'] == host['plan']
        else:
            # The live worker never adopts another generation; it is drained first.
            assert current['pid'] != first['pid'] and not pid_alive(first['pid'])
            assert restored['plan'] != host['plan']
    finally:
        stop_quietly(successor)


def test_an_unknown_holder_of_the_storage_lease_is_never_duplicated(manager):
    """A lease held with no record (an unknown, still-exiting owner) is waited out, never duplicated."""
    lock_path = manager.project / 'data' / '.native-storage-service.lock'
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        code, started = manager.control('start')
        assert code == 200 and started.get('ok') is True and started.get('retrying') is True, (code, started)
        assert 'still draining' in manager.state()['lastFailureReason']
        time.sleep(1.0)
        assert not any(line.startswith(('host-ready', 'worker-start')) for line in manager.events())
    finally:
        os.close(descriptor)
    # Once the unknown owner releases the lease, the retry admits one host by itself.
    manager.wait_serving()
    ready_storage(manager)
    assert sum(line.startswith('host-ready') for line in manager.events()) == 1
