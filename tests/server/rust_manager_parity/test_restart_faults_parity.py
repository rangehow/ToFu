"""Faults after the restart commit point, against both managers (test_restart_always_serves.py).

Once a restart has stopped the old worker it always ends with a serving
worker. The quota fault is the kernel's own ``EDQUOT``, injected into the
manager process by ``parity_shim.c`` (``LD_PRELOAD``) as the old worker exits;
the over-budget storage host ignores SIGTERM for the manager's whole
``restart_budget.NATIVE_SERVICE_STOP_SECONDS`` (100 s) stop budget.
"""
from __future__ import annotations

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for

pytestmark = pytest.mark.api


@pytest.fixture
def native_storage() -> bool:
    return True


@pytest.fixture
def manager_environment(parity_shim) -> dict[str, str]:
    return {'LD_PRELOAD': parity_shim, 'TOFU_PARITY_QUOTA_DIR': '{project}/data',
            'TOFU_PARITY_QUOTA_TRIGGER': '{project}/quota-full'}


def test_a_quota_hit_after_stop_is_retried_instead_of_aborting_the_restart(manager):
    """2026-11-18 11:15: stop succeeded, then the lock create and the state write both failed."""
    first = manager.start_serving()
    (manager.project / 'quota-after-stop').write_text('')
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    assert result.get('retrying') is True, result
    assert (manager.project / 'quota-full').exists(), 'the old worker never stopped'
    manager.wait_log('Disk quota exceeded', since=since)
    assert manager.status()['observed'] != 'crashloop'
    (manager.project / 'quota-full').unlink()
    current = manager.wait_serving(other_than=first['pid'])
    assert current['lastRestart']['outcome'] == 'spawned'
    assert current['recentFailureCount'] == 1
    assert manager.last_restart()['outcome'] == 'spawned'


def test_storage_host_past_its_stop_budget_is_killed_and_replaced(manager):
    first = manager.start_serving()
    old_host = wait_for(lambda: manager.storage_record().get('state') == 'ready' and manager.storage_record(),
                        'a ready storage host')
    (manager.project / 'probe-stop-mode').write_text('hang')

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    current = manager.wait_serving(other_than=first['pid'], timeout=60)
    assert not pid_alive(old_host['pid'])
    record = wait_for(lambda: manager.storage_record().get('state') == 'ready' and manager.storage_record(),
                      'the replacement storage host to be ready')
    assert record['serviceId'] != old_host['serviceId']
    closes = current['lastRestart']['uncleanCloses']
    assert any('killed' in close['detail'] and close['service'] == 'native storage' for close in closes), closes
    events = manager.events()
    assert events.index(f'host-stop {old_host["serviceId"]} hang') < events.index(f'host-ready {record["serviceId"]}')
