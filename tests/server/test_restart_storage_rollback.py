"""A restart whose new storage generation never serves restores service by itself.

2026-11-17 21:49 (A6): the old worker had been stopped, the new generation's
first storage start failed ("sidecar health preflight did not pass") and the
host published ``failed; explicit restart required``: 20.8 minutes without
service until the owner restarted again. These specs drive the real manager,
a real API worker and a real resident storage host whose start fails for a
chosen engine, then check that the manager rolls the engine back to the last
known-good one and serves again with no manual action, recording the outcome
in server-manager.log and in the restart outcome Settings reads.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import shutil
import sys
import time

import pytest

from serverctl_pkg import native_storage_service, resident_service as resident
from serverctl_pkg.native_storage_service import NativeStorageService
from tests.server import test_server_manager as existing
from tests.support.owned_processes import retire_processes_under

sm = existing.sm
_scrub_ambient_server_env = existing._scrub_ambient_server_env
ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]

# The API worker is ready only while its storage host is (lib readiness: storage gates /api/ready).
_WORKER = existing._WORKER.replace(
    "            {'ok': True, 'ready': True, 'state': 'ready',\n"
    "             'storage': {'ready': True, 'state': 'ready'}}\n"
    "            if self.path == '/api/ready' else",
    "            _readiness()\n"
    "            if self.path == '/api/ready' else",
).replace(
    'class Handler(BaseHTTPRequestHandler):',
    "def _readiness():\n"
    "    try:\n"
    "        state = json.load(open(os.path.join(os.getcwd(), 'data', 'native-storage-service.json')))['state']\n"
    "    except Exception:\n"
    "        state = 'unknown'\n"
    "    ready = state == 'ready'\n"
    "    return {'ok': ready, 'ready': ready, 'state': 'ready' if ready else 'starting', 'pid': os.getpid(),\n"
    "            'storage': {'ready': ready, 'state': state}}\n"
    'class Handler(BaseHTTPRequestHandler):',
)
assert '_readiness()' in _WORKER


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GenerationStorage(NativeStorageService):
    """The real resident owner and known-good policy; the host program and engine bytes are test doubles."""
    host_module = 'tests.support.resident_storage_generation_probe'

    def __init__(self, project: str, python: str):
        super().__init__(project, python)
        self.engine = self.project / 'engine' / 'tofu-db'
        self.deployments: list[str] = []

    def host_matches(self, record: dict) -> bool:
        return resident.host_matches(record, host_module=self.host_module)

    def process_environment(self, environment):
        return {**super().process_environment(environment), 'PYTHONPATH': str(ROOT)}

    def prepare(self, environment):
        return {'backend': 'probe', 'binary': str(self.engine), 'binarySha256': _sha256(self.engine)}

    def engine_contract(self, plan):
        return 'contract-1'

    def current_contract(self):
        return 'contract-1'

    def deploy_engine(self, copy, dest, contract, note):
        self.deployments.append(note)
        shutil.copyfile(copy, dest)

    def owns_lease(self, lease):
        return False

    def stop(self, timeout=None, **options):
        return super().stop(30.0 if timeout is None else timeout, **options)


def _land_engine(storage: GenerationStorage, content: bytes) -> str:
    storage.engine.parent.mkdir(parents=True, exist_ok=True)
    storage.engine.write_bytes(content)
    storage.engine.chmod(0o755)
    return _sha256(storage.engine)


@pytest.fixture
def harness(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='server_manager')
    port = existing._free_port()
    project = existing._project(tmp_path, _WORKER)
    monkeypatch.setenv('PORT', str(port))
    monkeypatch.setenv('TOFU_HEARTBEAT_DIR', str(tmp_path / 'heartbeat'))
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(project))
    manager = sm.LifecycleManager(str(project), sys.executable, monitor_interval=0.2)
    storage = GenerationStorage(str(project), sys.executable)
    manager._native_storage = storage
    monkeypatch.setattr(native_storage_service, 'host_matches', storage.host_matches)
    try:
        yield manager, project, storage
    finally:
        (project / 'bad-engines').write_text('')
        stopped = {}
        try:
            stopped = manager.stop(source='spec-teardown')
        finally:
            manager.close()
            leftovers = retire_processes_under(tmp_path)
        assert leftovers == [], f'processes outlived the test: {leftovers}; stop: {stopped}'


def serving(manager, *, other_than=None, timeout=60.0):
    deadline = time.monotonic() + timeout
    current = {}
    while time.monotonic() < deadline:
        manager.reconcile()
        current = manager.status(probe_health=True)
        if (current.get('running') and current.get('observed') == 'running'
                and current.get('pid') != other_than):
            return current
        time.sleep(0.1)
    raise AssertionError(f'no serving worker within {timeout:.0f}s: {current}')


def test_a_new_engine_whose_first_start_fails_after_stop_is_rolled_back_and_service_restored(
        harness, monkeypatch, caplog):
    manager, project, storage = harness
    good = _land_engine(storage, b'#!/bin/sh\necho tofu-db good\n')
    assert manager.start(source='spec')['ok'] is True
    first = serving(manager)
    assert manager._state['knownGood']['binarySha256'] == good

    # The new engine passes every pre-shutdown check (as 4827a86c1739 did at
    # 21:48) and fails only its first start on the live authority after stop.
    monkeypatch.setattr(storage, 'probe_generation', lambda plan, env: None)
    bad = _land_engine(storage, b'#!/bin/sh\necho tofu-db bad\n')
    (project / 'bad-engines').write_text(bad)

    result = manager.restart(source='spec')

    assert result['ok'] is True, result
    current = serving(manager, other_than=first['pid'], timeout=90.0)
    assert _sha256(storage.engine) == good
    assert storage.record()['plan']['binarySha256'] == good and storage.record()['state'] == 'ready'
    assert storage.deployments == [f'automatic rollback from {bad[:12]}, whose first start never served']
    recovery = current['lastRestart']['recovery']
    assert recovery['rolledBack'] is True and recovery['restoredAt'] >= recovery['at']
    assert 'sidecar health preflight did not pass' in recovery['cause']
    assert current['observed'] == 'running'
    log = caplog.text
    assert 'never became ready' in log and 'restoring service automatically (recovery 1/' in log
    assert f'rolled back from {bad[:12]} to the last known-good {good[:12]}' in log
    assert 'service restored after automatic storage recovery' in log
    assert 'explicit restart required' not in log.split('restoring service automatically')[-1]

    # Settings reads the same outcome (routes/api_v1/update.py last_restart_recovery).
    import lib.lifecycle_approval as la
    shown = la.latest_restart_recovery()
    assert shown['rolledBack'] is True and good[:12] in shown['action']


def test_without_a_known_good_engine_the_failed_host_is_replaced_and_bounded(harness, monkeypatch, caplog):
    """No rollback target: the manager still replaces host and worker, under its failure budget."""
    manager, project, storage = harness
    engine = _land_engine(storage, b'#!/bin/sh\necho tofu-db only\n')
    (project / 'bad-engines').write_text(engine)
    monkeypatch.setattr(manager, 'max_failures', 2)

    assert manager.start(source='spec')['ok'] is True
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and 'recovery exhausted' not in str(manager._state.get('lastError')):
        manager.reconcile()
        time.sleep(0.1)
    assert 'automatic storage recovery exhausted after 2 attempts' in manager._state['lastError']
    assert caplog.text.count('restoring service automatically') == 2
    assert storage.deployments == []

    # An explicit restart resets the budget; with the cause gone it serves.
    (project / 'bad-engines').write_text('')
    assert manager.restart(source='spec')['ok'] is True
    assert serving(manager)['lastRestart']['outcome'] == 'spawned'
    assert manager._state['storageRecoveries'] == []


def test_a_new_engine_that_cannot_start_on_a_disposable_authority_refuses_before_stop(harness):
    """Stop before stopping: the new engine is started and probed before the old worker is drained."""
    manager, project, storage = harness
    _land_engine(storage, b'#!/bin/sh\necho tofu-db good\n')
    (project / 'scripts').symlink_to(ROOT / 'scripts')
    assert manager.start(source='spec')['ok'] is True
    first = serving(manager)
    _land_engine(storage, b'#!/bin/sh\necho not an engine >&2\nexit 3\n')

    result = manager.restart(source='spec')

    assert result['ok'] is False
    assert 'did not start and answer on a disposable authority before shutdown' in result['message']
    current = serving(manager)
    assert current['pid'] == first['pid']
    assert current['lastRestart']['outcome'] == 'refused'
    assert current['lastRestart']['stage'] == 'storage probe'
