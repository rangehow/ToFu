"""The resident execution host under both managers.

Covers the manager side of test_execution_composition.py,
test_execution_service.py, test_execution_shutdown_failure.py and
test_execution_preparation.py: composition and dependency order, retention
across API restarts, replacement and failed-close retirement, recovery of a
departed executor charged to the failure budget, refusal before stop, and the
interpreter that prepares and runs the host. The hosts are the
``fake_project`` doubles speaking the real resident protocol.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for

pytestmark = pytest.mark.api


@pytest.fixture
def native_storage() -> bool:
    return True


@pytest.fixture
def execution() -> bool:
    return True


def ready(manager, which: str) -> dict:
    read = manager.storage_record if which == 'storage' else manager.execution_record
    return wait_for(lambda: read().get('state') == 'ready' and read(), f'a ready {which} host')


def environment_of(manager, kind: str) -> dict:
    return json.loads((manager.project / f'env-{kind}.json').read_text())


def position(events: list[str], prefix: str) -> int:
    return next(index for index, line in enumerate(events) if line.startswith(prefix))


def test_start_composes_storage_then_execution_and_an_api_role_worker(manager):
    manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')
    events = manager.events()
    assert position(events, 'host-plan') < position(events, 'exec-plan')
    assert position(events, 'exec-ready') < position(events, 'worker-start')
    host = environment_of(manager, 'execution')
    assert host['TOFU_PROCESS_ROLE'] == 'execution'
    assert host['TOFUDB_TRANSPORT'] == 'daemon'
    assert host['TOFU_STORAGE_CONNECTION_FILE'] == str(manager.project / 'data/native-storage-connection.json')
    worker = environment_of(manager, 'worker')
    assert worker['TOFU_PROCESS_ROLE'] == 'api'
    assert worker['TOFU_STORAGE_CONNECTION_FILE'] == host['TOFU_STORAGE_CONNECTION_FILE']
    assert storage['managerPid'] == execution['managerPid'] == manager.process.pid


def test_an_api_restart_keeps_unchanged_hosts_and_a_new_plan_replaces_only_execution(manager):
    (manager.project / 'storage-plan-fixed').write_text('')
    first = manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')

    code, result = manager.control('restart')
    assert code == 200 and result.get('ok') is True, (code, result)
    second = manager.wait_serving(other_than=first['pid'])
    assert manager.storage_record()['serviceId'] == storage['serviceId']
    assert manager.execution_record()['serviceId'] == execution['serviceId']

    (manager.project / 'exec-digest').write_text('2')
    code, result = manager.control('restart')
    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=second['pid'])
    replaced = ready(manager, 'execution')
    assert replaced['serviceId'] != execution['serviceId'] and replaced['plan']['digest'] == '2'
    assert manager.storage_record()['serviceId'] == storage['serviceId']
    assert f'exec-stop {execution["serviceId"]} clean' in manager.events()

    code, stopped = manager.control('stop')
    assert code == 200 and stopped.get('ok') is True, (code, stopped)
    events = manager.events()
    assert (events.index(f'exec-stop {replaced["serviceId"]} clean')
            < events.index(f'host-stop {storage["serviceId"]} clean'))
    assert manager.execution_record() == {} and manager.storage_record() == {}


def test_a_storage_upgrade_joins_execution_before_releasing_storage(manager):
    first = manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    new_storage, new_execution = ready(manager, 'storage'), ready(manager, 'execution')
    assert new_storage['serviceId'] != storage['serviceId']
    assert new_execution['serviceId'] != execution['serviceId']
    events = manager.events()
    assert (events.index(f'exec-stop {execution["serviceId"]} clean')
            < events.index(f'host-stop {storage["serviceId"]} clean')
            < events.index(f'host-ready {new_storage["serviceId"]}')
            < events.index(f'exec-ready {new_execution["serviceId"]}'))


@pytest.mark.parametrize('mode', ['timeout', 'stale'])
def test_an_execution_host_whose_close_failed_is_still_replaced(manager, mode):
    (manager.project / 'storage-plan-fixed').write_text('')
    first = manager.start_serving()
    execution = ready(manager, 'execution')
    (manager.project / 'exec-stop-mode').write_text(mode)
    (manager.project / 'exec-digest').write_text('2')

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    current = manager.wait_serving(other_than=first['pid'])
    replaced = ready(manager, 'execution')
    assert replaced['serviceId'] != execution['serviceId']
    closes = current['lastRestart'].get('uncleanCloses') or []
    assert any(close['service'] == 'execution' for close in closes), closes


def test_a_departed_executor_is_recovered_without_retiring_the_api(manager):
    first = manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')

    os.kill(execution['pid'], signal.SIGKILL)

    recovered = wait_for(lambda: (record := manager.execution_record()).get('state') == 'ready'
                         and record.get('serviceId') != execution['serviceId'] and record,
                         'a recovered execution host')
    current = manager.wait_serving()
    assert current['pid'] == first['pid'] and manager.worker_health_pid() == first['pid']
    assert current['recentFailureCount'] == 1
    assert manager.storage_record()['serviceId'] == storage['serviceId']
    assert recovered['plan'] == execution['plan']


def test_recovery_refuses_changed_code_until_an_explicit_restart(manager):
    first = manager.start_serving()
    execution = ready(manager, 'execution')
    (manager.project / 'exec-digest').write_text('2')

    os.kill(execution['pid'], signal.SIGKILL)

    wait_for(lambda: (manager.state().get('executionRecovery') or {}).get('requiresRestart'),
             'recovery to require an explicit restart')
    assert 'changed' in manager.state()['lastError']
    assert manager.status(probe=True)['ready'] is False
    assert manager.lock_pid() == first['pid']
    code, result = manager.control('restart')
    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    assert ready(manager, 'execution')['plan']['digest'] == '2'


def test_an_execution_preparation_refusal_keeps_the_serving_generation(manager):
    """A storage boundary violation found by execution preparation refuses before stop."""
    first = manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')
    (manager.project / 'exec-plan-fail').write_text(
        'storage cutover refused: lib/invalid_owner.py opens sqlite3 outside the storage boundary')

    code, result = manager.control('restart')

    assert result.get('ok') is False, (code, result)
    assert 'storage cutover refused' in result['message']
    current = manager.wait_serving()
    assert current['pid'] == first['pid']
    assert current['lastRestart']['outcome'] == 'refused'
    assert manager.storage_record()['serviceId'] == storage['serviceId']
    assert manager.execution_record()['serviceId'] == execution['serviceId']
    assert not any(line.startswith('worker-exit') for line in manager.events())


def _interpreter(project: Path) -> Path:
    wrapper = project / 'installed-python'
    wrapper.write_text(f'#!/bin/sh\nTOFU_PARITY_INTERPRETER="$0" exec {sys.executable} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def test_the_installed_interpreter_prepares_and_runs_the_execution_host(manager):
    wrapper = _interpreter(manager.project)
    (manager.project / '.tofu_env.json').write_text(json.dumps({'python': str(wrapper)}))

    manager.start_serving()
    execution = ready(manager, 'execution')

    calls = (manager.project / 'interpreter-calls.log').read_text().splitlines()
    assert f'execution --plan {wrapper}' in calls
    assert f'execution --service-id {wrapper}' in calls
    assert execution['plan']['python'] == str(wrapper)
    assert environment_of(manager, 'execution')['TOFU_PARITY_INTERPRETER'] == str(wrapper)
    assert all(not line.startswith('storage') or line.endswith(' -') for line in calls)


def test_an_unusable_marker_keeps_the_manager_interpreter(manager):
    (manager.project / '.tofu_env.json').write_text(json.dumps({'python': str(manager.project / 'missing')}))

    manager.start_serving()
    ready(manager, 'execution')

    calls = (manager.project / 'interpreter-calls.log').read_text().splitlines()
    assert 'execution --plan -' in calls and 'execution --service-id -' in calls


@pytest.mark.parametrize('setting', ['TOFUDB_TRANSPORT=embedded', 'TOFU_PROCESS_ROLE=api'])
def test_explicit_topology_keeps_its_own_execution_owner(manager, setting):
    (manager.project / '.env').write_text(setting + '\n')

    manager.start_serving()
    ready(manager, 'storage')

    assert manager.execution_record() == {}
    assert not any(line.startswith('exec-') for line in manager.events())


def test_stop_joins_execution_then_storage_and_the_hosts_exit(manager):
    manager.start_serving()
    storage, execution = ready(manager, 'storage'), ready(manager, 'execution')

    code, stopped = manager.control('stop')

    assert code == 200 and stopped.get('ok') is True, (code, stopped)
    assert not pid_alive(storage['pid']) and not pid_alive(execution['pid'])
    events = manager.events()
    assert events.index(f'exec-stop {execution["serviceId"]} clean') < events.index(
        f'host-stop {storage["serviceId"]} clean')
