"""HEAD's storage engine is certified in the background, ahead of the restart.

Executable specification of docs/NATIVE_STORAGE_LIFECYCLE.md, "Certifying
ahead of the restart":

- ``pending_certification`` names HEAD's engine only when a restart would run
  the gate (TofuDB, not deployed, no record, inputs committed), and reuses the
  engine fingerprint while the committed engine inputs are unchanged;
- ``start_background`` launches one detached gate run and refuses a second
  while it holds the gate lock; its outcome lands in ``background.json``;
- a restart that arrives during a run waits for it and reuses its record
  instead of starting a second gate.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import pytest

from serverctl_pkg import engine_certification as certification
from serverctl_pkg import engine_precertify as precertify
from serverctl_pkg import native_engine as engines
from tests.server.test_engine_certification_gate import (  # noqa: F401  (project is a fixture)
    _gate, _git, _ir_only_change_after_deployment, certify, project, subprocess_head)
from tests.server.test_native_engine_update import binary

pytestmark = pytest.mark.unit
WORKTREE = Path(__file__).resolve().parents[2]


@pytest.fixture
def landed(project, monkeypatch):  # noqa: F811
    """An engine-input change landed after the deployed engine: HEAD needs the gate."""
    _ir_only_change_after_deployment(project, monkeypatch)
    return project


def _publish_heads_record(root: Path) -> None:
    _gate(root, [])(root, '', None, None)


def test_a_landed_engine_change_is_pending(landed):
    pending = precertify.pending_certification(landed)
    assert pending == {'commit': subprocess_head(landed), 'sourceSha256': engines.source_fingerprint(landed),
                       'contractSha256': engines.storage_contract_sha256(landed)}


def test_nothing_is_pending_once_heads_record_exists(landed):
    _publish_heads_record(landed)
    assert precertify.pending_certification(landed) is None


def test_nothing_is_pending_when_the_deployed_engine_is_heads(landed, monkeypatch):
    monkeypatch.setenv('TOFUDB_BIN', str(binary(landed / engines.MANAGED_ENGINE_ROOT / 'current/tofu-db',
                                                engines.source_fingerprint(landed))))
    assert precertify.pending_certification(landed) is None


def test_nothing_is_pending_while_engine_inputs_are_uncommitted(landed):
    (landed / 'contracts/tofudb_ir_v1.json').write_text('{"turn.get": "uncommitted"}')
    assert precertify.pending_certification(landed) is None


def test_nothing_is_pending_without_tofudb(landed):
    (landed / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=sqlite\n')
    assert precertify.pending_certification(landed) is None


def test_heads_fingerprint_is_computed_once_per_committed_engine_inputs(landed, monkeypatch):
    calls = []
    real = engines.source_fingerprint
    monkeypatch.setattr(engines, 'source_fingerprint', lambda root: calls.append(root) or real(root))
    first = precertify.pending_certification(landed)
    (landed / 'README.md').write_text('a non-engine landing')
    _git(landed, 'add', 'README.md')
    _git(landed, 'commit', '-q', '-m', 'docs only')
    second = precertify.pending_certification(landed)
    assert len(calls) == 1
    assert second == {**first, 'commit': subprocess_head(landed)}
    (landed / 'packages/tofu-db/src/lib.rs').write_text('another engine change')
    assert precertify.pending_certification(landed) is None  # dirty, even with a cached identity
    _git(landed, 'commit', '-q', '-am', 'engine change')
    third = precertify.pending_certification(landed)
    assert len(calls) == 2
    assert third['sourceSha256'] == real(landed) != first['sourceSha256']


_SHIM = '''
import os, time
from pathlib import Path
from serverctl_pkg import native_engine

def _gate(root, commit, fixture, work_root):
    control = Path(os.environ['PRECERTIFY_TEST_GATE'])
    (control / 'started').write_text(commit)
    while not (control / 'release').exists():
        time.sleep(0.05)
    return 0

native_engine._run_engine_gate = _gate
'''


def _wait_for(predicate, what: str, seconds: float = 60):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        time.sleep(0.05)


def test_one_background_run_certifies_head_and_a_second_is_refused(landed, tmp_path, monkeypatch):
    # The child is real; only its gate is replaced, through a test-only sitecustomize on PYTHONPATH.
    shim = tmp_path / 'shim'
    shim.mkdir()
    (shim / 'sitecustomize.py').write_text(_SHIM)
    control = tmp_path / 'gate-control'
    control.mkdir()
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(shim), str(WORKTREE)]))
    monkeypatch.setenv('PRECERTIFY_TEST_GATE', str(control))

    first = precertify.start_background(landed, sys.executable)
    assert first['started'] is True, first
    assert first['commit'] == subprocess_head(landed)
    _wait_for(lambda: (control / 'started').exists(), 'the background gate to start')
    assert (control / 'started').read_text() == subprocess_head(landed)
    status = engines.background_certification_status(landed)
    assert status['ok'] is None and status['pid'] == first['pid'] and status['commit'] == first['commit']

    second = precertify.start_background(landed, sys.executable)
    assert second['started'] is False
    assert 'in progress' in second['reason'] and second['status']['pid'] == first['pid']

    _publish_heads_record(landed)  # what scripts/engine_certify.py publishes when its checks pass
    (control / 'release').write_text('')
    _wait_for(lambda: (engines.background_certification_status(landed) or {}).get('ok') is not None,
              'the background run to finish')
    status = engines.background_certification_status(landed)
    assert status['ok'] is True and status['finishedAt'], status
    log = (landed / precertify.LOG_FILE).read_text()
    assert 'start: certifying engine' in log and 'end: ok=True' in log
    third = precertify.start_background(landed, sys.executable)
    assert third == {'started': False, 'reason': "HEAD's storage engine needs no certification"}


def test_start_background_reports_instead_of_raising(landed, monkeypatch):
    def broken(_root):
        raise RuntimeError('git exploded')
    monkeypatch.setattr(precertify, 'pending_certification', broken)
    result = precertify.start_background(landed, sys.executable)
    assert result['started'] is False and 'git exploded' in result['reason']
    assert 'git exploded' in (landed / precertify.LOG_FILE).read_text()


def _hold_gate_lock(root: Path, commit: str):
    lock = root / engines.CERTIFICATION_GATE_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open('a+')
    fcntl.flock(handle, fcntl.LOCK_EX)
    (root / engines.BACKGROUND_CERTIFICATION_STATUS).write_text(json.dumps(
        {'commit': commit, 'startedAt': '2026-11-17T01:02:03+00:00', 'pid': 4242, 'ok': None}))
    return handle


def test_a_restart_waits_for_the_running_gate_and_reuses_its_record(landed, monkeypatch):
    monkeypatch.setattr(engines, '_run_engine_gate', lambda *_a: pytest.fail('a second gate never runs'))
    messages = []
    monkeypatch.setattr(engines, '_report_progress', lambda message, **_k: messages.append(message))
    held = _hold_gate_lock(landed, subprocess_head(landed))
    outcome = []
    restart = threading.Thread(target=lambda: outcome.append(engines.certify_head_before_start(landed)))
    restart.start()
    _wait_for(lambda: any(m.startswith('Waiting for the engine gate run') for m in messages), 'the wait line')
    [waiting] = [m for m in messages if m.startswith('Waiting')]
    assert subprocess_head(landed)[:12] in waiting and '2026-11-17T01:02:03+00:00' in waiting
    assert restart.is_alive()
    _publish_heads_record(landed)
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    restart.join(60)
    assert outcome == [None]
    assert messages[-1].startswith('Using certified engine')


def test_restart_path_cost_with_and_without_a_background_record(landed, monkeypatch, capsys):
    """Measurement: a restart after a finished background run versus the lazy gate (a fake sleeping N s)."""
    lazy_gate_seconds = 3
    gate = _gate(landed, [])

    def slow_gate(*args):
        time.sleep(lazy_gate_seconds)
        return gate(*args)

    monkeypatch.setattr(engines, '_run_engine_gate', slow_gate)
    started = time.monotonic()
    engines.certify_head_before_start(landed)
    lazy = time.monotonic() - started
    certification_root = certification.certifications_root(landed)
    for record in certification.record_directories(landed, engines.storage_contract_sha256(landed)):
        shutil.rmtree(record.parent)
    assert precertify.pending_certification(landed) is not None
    _publish_heads_record(landed)  # the background run finished first
    monkeypatch.setattr(engines, '_run_engine_gate', lambda *_a: pytest.fail('no gate on the restart path'))
    started = time.monotonic()
    engines.certify_head_before_start(landed)
    precertified = time.monotonic() - started
    with capsys.disabled():
        print(f'\nrestart-path certify_head_before_start: lazy gate (fake, {lazy_gate_seconds}s) {lazy:.2f}s; '
              f'after a background run {precertified:.2f}s ({certification_root})')
    assert lazy >= lazy_gate_seconds > precertified
