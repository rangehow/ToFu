"""Known-good storage engine rollback, against both managers (test_restart_storage_rollback.py).

2026-11-17 21:49 (A6): a new engine passed every pre-shutdown check, then
failed its first start after the old worker stopped. The manager must roll
the engine back to the last known-good one and serve again by itself.

The managers run the real known-good policy (``NativeStorageService``
retain/restore, ``native_engine.retention_refusal`` and the
``deploy_tofudb_engine`` deployment owner) against a project whose engine is a
script that identifies as tofu-db and carries a deployment receipt binding it
to the project's storage contract; only the resident host is a double.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from serverctl_pkg.native_engine import storage_contract_sha256
from tests.server.rust_manager_parity.conftest import wait_for

pytestmark = pytest.mark.api

ENGINE = 'packages/tofu-db/target/release/tofu-db'
CARGO_TOML = '''[package]
name = "tofu-db"
version = "0.0.0"
edition = "2021"

[package.metadata.tofu]
external-inputs = ["contracts/storage_v2.json"]
'''
# A disposable-authority start: refuses the engines listed in probe-refuses.
DRIFT_SCRIPT = r'''import argparse, hashlib, json, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--binary')
parser.add_argument('--json', action='store_true')
args = parser.parse_args()
digest = hashlib.sha256(Path(args.binary).read_bytes()).hexdigest()
refused = Path('probe-refuses')
if refused.exists() and digest in refused.read_text().split():
    print('tofu-db exited 3 before its health handshake', file=sys.stderr)
    raise SystemExit(3)
print(json.dumps({'ok': True, 'binarySha256': digest}))
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def land_engine(project: Path, label: str) -> str:
    """Install an engine whose receipt binds it to this source's storage contract."""
    engine = project / ENGINE
    engine.parent.mkdir(parents=True, exist_ok=True)
    if engine.exists():
        engine.chmod(0o755)
        engine.unlink()
    engine.write_text(f'#!/bin/sh\necho tofu-db {label}\n')
    engine.chmod(0o755)
    digest = _sha256(engine)
    (engine.parent / 'build.json').write_text(json.dumps({
        'binary_sha256': digest, 'storage_contract_sha256': storage_contract_sha256(project)}))
    return digest


@pytest.fixture
def native_storage() -> bool:
    return True


@pytest.fixture
def manager_environment() -> dict[str, str]:
    return {'TOFU_MANAGER_MAX_FAILURES': '2'}


@pytest.fixture
def prepare_project():
    def prepare(project: Path) -> None:
        (project / 'packages/tofu-db').mkdir(parents=True)
        (project / 'packages/tofu-db/Cargo.toml').write_text(CARGO_TOML)
        for name in ('storage_v2', 'tofudb_ir_v1'):
            (project / f'contracts/{name}.json').write_text('{"parity": true}\n')
        (project / 'scripts').mkdir()
        (project / 'scripts/check_tofudb_engine_drift.py').write_text(DRIFT_SCRIPT)
        (project / 'storage-engine').write_text(ENGINE)
        (project / 'worker-ready-follows-storage').write_text('')
        land_engine(project, 'good')
    return prepare


def test_a_new_engine_whose_first_start_fails_after_stop_is_rolled_back_and_service_restored(manager):
    project = manager.project
    good = _sha256(project / ENGINE)
    first = manager.start_serving()
    known = wait_for(lambda: manager.state().get('knownGood', {}).get('binarySha256') == good
                     and manager.state()['knownGood'], 'the ready engine retained as known-good')
    assert Path(known['copy']).is_file()
    bad = land_engine(project, 'bad')
    (project / 'bad-engines').write_text(bad)
    since = manager.log_size()

    code, result = manager.control('restart')

    assert code == 200 and result.get('ok') is True, (code, result)
    current = manager.wait_serving(other_than=first['pid'], timeout=90)
    assert _sha256(project / ENGINE) == good
    record = manager.storage_record()
    assert record['plan']['binarySha256'] == good and record['state'] == 'ready'
    recovery = wait_for(lambda: manager.last_restart().get('recovery', {}).get('restoredAt')
                        and manager.last_restart()['recovery'], 'the recorded restoration')
    assert recovery['rolledBack'] is True and recovery['restoredAt'] >= recovery['at']
    assert 'sidecar health preflight did not pass' in recovery['cause']
    assert current['observed'] == 'running'
    log = manager.log_text()[since:]
    assert 'never became ready' in log and 'restoring service automatically (recovery 1/' in log
    assert f'rolled back from {bad[:12]} to the last known-good {good[:12]}' in log
    assert 'service restored after automatic storage recovery' in log
    assert 'explicit restart required' not in log.split('restoring service automatically')[-1]


def test_without_a_known_good_engine_the_failed_host_is_replaced_and_bounded(manager):
    """No rollback target: the manager replaces host and worker under its failure budget."""
    project = manager.project
    (project / 'bad-engines').write_text(_sha256(project / ENGINE))

    code, started = manager.control('start')

    assert code == 200 and started.get('ok') is True, (code, started)
    wait_for(lambda: 'automatic storage recovery exhausted' in str(manager.state().get('lastError')),
             'the bounded automatic recovery to give up', timeout=120)
    assert 'after 2 attempts' in manager.state()['lastError']
    assert 'explicit restart required' in manager.state()['lastError']
    assert manager.log_text().count('restoring service automatically') == 2
    assert 'rolled back from' not in manager.log_text()

    # An explicit restart resets the budget; with the cause gone it serves.
    (project / 'bad-engines').write_text('')
    code, restarted = manager.control('restart')
    assert code == 200 and restarted.get('ok') is True, (code, restarted)
    assert manager.wait_serving()['lastRestart']['outcome'] == 'spawned'
    assert manager.state()['storageRecoveries'] == []


def test_a_new_engine_that_cannot_start_on_a_disposable_authority_refuses_before_stop(manager):
    project = manager.project
    first = manager.start_serving()
    bad = land_engine(project, 'unstartable')
    (project / 'probe-refuses').write_text(bad)

    code, result = manager.control('restart')

    assert result.get('ok') is False, (code, result)
    assert 'did not start and answer on a disposable authority before shutdown' in result['message']
    current = manager.wait_serving()
    assert current['pid'] == first['pid']
    assert current['lastRestart']['outcome'] == 'refused'
    assert current['lastRestart']['stage'] == 'storage probe'
    assert not any(line.startswith('worker-exit') for line in manager.events())
