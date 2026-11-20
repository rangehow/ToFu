"""Native storage parity: the resident host follows the API worker's lifecycle."""
from __future__ import annotations

import json
import sys

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for

pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]


@pytest.fixture
def native_storage() -> bool:
    return True


def _host_record(manager) -> dict:
    try:
        return json.loads((manager.project / 'data/native-storage-service.json').read_text())
    except (OSError, ValueError):
        return {}


def _ready_host(manager, *, other_than: str | None = None) -> dict:
    return wait_for(lambda: (_host_record(manager).get('state') == 'ready'
                             and _host_record(manager).get('serviceId') != other_than
                             and _host_record(manager)),
                    'a ready native storage host')


def test_start_launches_a_ready_storage_host(manager):
    manager.start_serving()

    record = _ready_host(manager)

    assert pid_alive(record['pid'])
    assert record['plan']['generation'] == 1
    assert f'host-ready {record["serviceId"]}' in manager.events()


def test_restart_replaces_the_storage_host_after_the_old_worker_stopped(manager):
    first = manager.start_serving()
    old_host = _ready_host(manager)

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    new_host = _ready_host(manager, other_than=old_host['serviceId'])
    assert new_host['plan']['generation'] > old_host['plan']['generation']
    wait_for(lambda: not pid_alive(old_host['pid']), 'the old storage host to exit')
    events = manager.events()
    worker_exit = events.index(f'worker-exit {first["pid"]}')
    host_stop = events.index(f'host-stop {old_host["serviceId"]} clean')
    host_ready = events.index(f'host-ready {new_host["serviceId"]}')
    assert worker_exit < host_stop < host_ready
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'spawned'
                       and manager.last_restart(), 'lastRestart spawned')
    assert not restart.get('uncleanCloses')


def test_unconfirmed_storage_close_still_ends_serving(manager):
    first = manager.start_serving()
    old_host = _ready_host(manager)
    (manager.project / 'probe-stop-mode').write_text('stale')

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    _ready_host(manager, other_than=old_host['serviceId'])
    restart = wait_for(lambda: manager.last_restart().get('outcome') == 'spawned'
                       and manager.last_restart(), 'lastRestart spawned')
    assert any(close['service'] == 'native storage'
               and 'clean close was not confirmed' in close['detail']
               for close in restart['uncleanCloses']), restart


def test_a_resident_host_owned_elsewhere_refuses_before_the_old_server_stops(manager):
    first = manager.start_serving()
    _ready_host(manager)
    record_path = manager.project / 'data/native-storage-service.json'
    original = record_path.read_bytes()
    record = json.loads(original)
    record['host'] = 'another-host.invalid'
    record_path.write_text(json.dumps(record))
    since = manager.log_size()
    try:
        code, result = manager.control('restart')

        assert code == 409 and result.get('ok') is False, (code, result)
        assert 'native storage service belongs to another host' in result.get('message', ''), result
        assert pid_alive(first['pid']) and manager.worker_health_pid() == first['pid']
        refused = wait_for(lambda: manager.last_restart().get('outcome') == 'refused'
                           and manager.last_restart(), 'a recorded refusal')
        assert refused['stage'] == 'admission', refused
        log = manager.log_text()[since:]
        assert 'worker restart refused at admission stage' in log
        assert f'worker pid={first["pid"]} SIGTERM sent' not in log
    finally:
        record_path.write_bytes(original)
