"""The real ``/reload`` re-exec in place, against both managers.

Covers the re-exec cases of test_supervisor.py and
test_supervisor_reexec_children.py: an accepted reload replaces the manager's
image in place (same PID, new generation fingerprint) while the serving worker
and the manager-bound storage host stay up, the new image still controls that
host, and a worker the old image spawned is settled and reaped by the new one.

Each manager runs from a private copy of its generation closure
(``supervisor_protocol.SUPERVISOR_SOURCE_FILES`` plus ``serverctl_pkg``), so
the test changes that copy on disk, never the shipped tree.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal

import pytest

from supervisor_protocol import SUPERVISOR_SOURCE_FILES, supervisor_source_fingerprint
from tests.server.rust_manager_parity.conftest import REPO, pid_alive, wait_for

pytestmark = pytest.mark.api


@pytest.fixture
def native_storage() -> bool:
    return True


@pytest.fixture
def source_root(tmp_path) -> Path:
    checkout = tmp_path / 'checkout'
    shutil.copytree(REPO / 'serverctl_pkg', checkout / 'serverctl_pkg',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for name in SUPERVISOR_SOURCE_FILES:
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, checkout / name)
    return checkout.resolve()


def reload_to_next_generation(manager, checkout: Path) -> dict:
    policy = checkout / 'runtime_guards.py'
    policy.write_text(policy.read_text(encoding='utf-8') + '\n# next generation\n', encoding='utf-8')
    expected = supervisor_source_fingerprint(str(checkout))
    before = manager.get('/health')[1]
    assert before['sourceFingerprint'] != expected

    code, accepted = manager.post('/reload', {'projectPath': str(manager.project),
                                              'expectedFingerprint': expected, 'source': 'parity'})

    assert code == 202 and accepted['reloading'] is True, (code, accepted)

    def reloaded():
        try:
            status, health = manager.get('/health', timeout=5)
        except OSError:
            return None
        return status == 200 and health.get('sourceFingerprint') == expected and health
    after = wait_for(reloaded, 'the next generation to answer /health')
    assert after['managerPid'] == manager.process.pid
    assert manager.process.poll() is None, 'the manager must re-exec in place, never exit'
    assert after['startedAt'] >= before['startedAt']
    return after


def test_reload_reexecs_in_place_and_keeps_the_worker_and_storage_host(manager, source_root):
    first = manager.start_serving()
    host = wait_for(lambda: manager.storage_record().get('state') == 'ready' and manager.storage_record(),
                    'a ready storage host')

    reload_to_next_generation(manager, source_root)

    current = manager.wait_serving()
    assert current['pid'] == first['pid'] and manager.worker_health_pid() == first['pid']
    assert manager.storage_record()['serviceId'] == host['serviceId'] and pid_alive(host['pid'])
    assert f'host-owner-gone {host["serviceId"]}' not in manager.events()

    # The new image still holds the host's control channel: a restart drains it cleanly.
    code, result = manager.control('restart')
    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    assert f'host-stop {host["serviceId"]} clean' in manager.events()


def test_a_worker_spawned_before_the_reexec_is_settled_and_reaped_after_it(manager, source_root):
    first = manager.start_serving()
    reload_to_next_generation(manager, source_root)

    os.kill(first['pid'], signal.SIGKILL)

    manager.wait_serving(other_than=first['pid'])
    wait_for(lambda: not Path(f'/proc/{first["pid"]}').exists(), 'the inherited worker to be reaped')
    exit_record = manager.state()['lastWorkerProcessExit']
    assert exit_record['pid'] == first['pid']
    assert exit_record['signal'] == signal.SIGKILL
    assert manager.state()['lastExitCause'] == 'fatal_signal'
