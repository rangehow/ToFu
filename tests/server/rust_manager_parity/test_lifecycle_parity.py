"""Lifecycle protocol parity: start, restart gates, stop, crash recovery, routes."""
from __future__ import annotations

import json
import os
import signal
import sys

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for

pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]


def _assert_old_worker_still_serves(manager, pid):
    assert pid_alive(pid)
    assert manager.lock_pid() == pid
    assert manager.worker_ready()
    assert manager.worker_health_pid() == pid
    current = manager.wait_serving()
    assert current['pid'] == pid


def test_start_serves_a_ready_worker(manager):
    serving = manager.start_serving()

    assert serving['pid'] == manager.lock_pid()
    assert serving['running'] is True
    assert serving['desired'] == 'running'
    manager.wait_log(f'worker ready pid={serving["pid"]}')
    assert f'worker spawned pid={serving["pid"]}' in manager.log_text()


def test_restart_replaces_the_worker(manager):
    first = manager.start_serving()

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    current = manager.wait_serving(other_than=first['pid'])
    wait_for(lambda: not pid_alive(first['pid']), 'the old worker to exit')
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'spawned'
                       and manager.last_restart(), 'lastRestart spawned')
    assert restart['source'] == 'parity'
    assert restart.get('workerPid') == current['pid']
    assert 'worker restart replacement spawned' in manager.log_text()


def test_frontend_preparation_failure_refuses_before_stop(manager):
    first = manager.start_serving()
    (manager.project / 'prep-frontend-fail').write_text('parity bundle broken')
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 409 and result.get('ok') is False, (code, result)
    assert 'parity bundle broken' in result.get('message', '')
    _assert_old_worker_still_serves(manager, first['pid'])
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'refused'
                       and manager.last_restart(), 'lastRestart refused')
    assert restart['stage'] == 'frontend preparation'
    log = manager.wait_log('worker restart refused at frontend preparation stage', since=since)
    assert f'worker pid={first["pid"]} SIGTERM sent' not in log


def test_refused_storage_engine_decision_refuses_before_stop(manager):
    first = manager.start_serving()
    decision = {'decision': 'refused', 'message': 'uncertified engine refused'}
    (manager.project / 'prep-engine-decision').write_text(json.dumps(decision))
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 409 and result.get('ok') is False, (code, result)
    assert result.get('message') == 'uncertified engine refused'
    assert result.get('engineDecision', {}).get('decision') == 'refused'
    _assert_old_worker_still_serves(manager, first['pid'])
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'refused'
                       and manager.last_restart(), 'lastRestart refused')
    assert restart['stage'] == 'storage engine'
    assert restart['message'] == 'uncertified engine refused'
    manager.wait_log('worker restart refused at storage engine stage', since=since)
    assert 'frontend' not in manager.prep_calls()


def test_kept_storage_engine_decision_lets_the_restart_proceed(manager):
    first = manager.start_serving()
    decision = {'decision': 'kept', 'message': 'deployed engine kept'}
    (manager.project / 'prep-engine-decision').write_text(json.dumps(decision))

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    assert result.get('engineDecision', {}).get('decision') == 'kept'
    manager.wait_serving(other_than=first['pid'])
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'spawned'
                       and manager.last_restart(), 'lastRestart spawned')
    assert restart['engineDecision'] == decision


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory write permission')
def test_unwritable_state_refuses_before_stop(manager):
    first = manager.start_serving()
    since = manager.log_size()
    data = manager.project / 'data'
    os.chmod(data, 0o500)
    try:
        code, result = manager.control('restart')
    finally:
        os.chmod(data, 0o700)

    assert code == 409 and result.get('ok') is False, (code, result)
    assert 'cannot write its state file' in result.get('message', '')
    assert str(manager.state_path) in result['message']
    _assert_old_worker_still_serves(manager, first['pid'])
    # The refusal could not be persisted while data/ was read-only; the next
    # successful state write carries it.
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'refused'
                       and manager.last_restart(), 'lastRestart refused')
    assert restart['stage'] == 'state persistence'
    log = manager.wait_log('worker restart refused at state persistence stage', since=since)
    assert f'worker pid={first["pid"]} SIGTERM sent' not in log


def test_restart_builds_before_it_stops_the_old_worker(manager):
    first = manager.start_serving()
    calls_before = len(manager.prep_calls())
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    assert manager.prep_calls()[calls_before:] == ['storage-engine', 'frontend']
    log = manager.log_text()[since:]
    sigterm = log.index(f'worker pid={first["pid"]} SIGTERM sent')
    assert log.index('storage engine preflight passed') < sigterm
    assert log.index('frontend preflight passed') < sigterm
    events = manager.events()
    exit_index = events.index(f'worker-exit {first["pid"]}')
    assert events.index('prep storage-engine') < exit_index
    assert events.index('prep frontend') < exit_index


def test_stop_ends_the_worker(manager):
    first = manager.start_serving()

    code, result = manager.control('stop')

    assert code == 200 and result.get('ok') is True, (code, result)
    assert result.get('wasRunning') is True
    wait_for(lambda: manager.status().get('observed') == 'stopped', 'observed stopped')
    wait_for(lambda: not pid_alive(first['pid']), 'the worker to exit')
    current = manager.status()
    assert current['desired'] == 'stopped'
    assert current['running'] is False
    assert f'worker pid={first["pid"]} SIGTERM sent' in manager.log_text()


def test_crashed_worker_is_respawned(manager):
    first = manager.start_serving()

    os.kill(first['pid'], signal.SIGKILL)

    current = manager.wait_serving(other_than=first['pid'])
    assert current['pid'] != first['pid']
    assert 'worker failure 1/' in manager.log_text()


def test_health_reports_the_manager_identity(manager):
    code, health = manager.get('/health')

    assert code == 200
    assert health['ok'] is True
    assert health['version']
    assert isinstance(health['protocolVersion'], int)
    assert health['managerPid'] == manager.process.pid
    assert str(manager.project) in health['projects']
    assert health['sourceFingerprint']


def test_foreign_projects_and_unknown_routes_are_refused(manager, tmp_path):
    foreign = tmp_path / 'foreign'
    foreign.mkdir()

    code, payload = manager.get(f'/status?projectPath={foreign}')
    assert code == 403 and payload['ok'] is False
    code, payload = manager.post('/start', {'projectPath': str(foreign), 'source': 'parity'})
    assert code == 403 and payload['ok'] is False
    code, payload = manager.get('/no-such-route')
    assert code == 404 and payload['ok'] is False
    code, payload = manager.post('/no-such-route', {'projectPath': str(manager.project)})
    assert code == 404 and payload['ok'] is False


def test_a_worker_served_over_https_becomes_ready(manager, tmp_path):
    """TOFU_TLS=1: server.py serves HTTPS and records it in data/.last_serve_mode."""
    from lib.server.server_tls import ensure_tls_certificates
    certfile, keyfile = ensure_tls_certificates(data_root=str(tmp_path / 'tls'), bind_host='127.0.0.1',
                                                environ={})
    (manager.project / 'worker-tls').write_text(f'{certfile} {keyfile}')

    code, started = manager.control('start')

    assert code == 200 and started.get('ok') is True, (code, started)
    current = wait_for(lambda: (status := manager.status(probe=True)).get('ready') is True and status,
                       'an HTTPS worker to be ready')
    assert current['observed'] == 'running'
    assert current['probeScheme'] == 'https' and current['liveness'] is True
    assert current['pid'] == manager.lock_pid()
    assert (manager.project / 'data/.last_serve_mode').read_text() == 'https'
