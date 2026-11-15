"""Benchmark provenance reads repository state without locking its index.

Run manifests record whether the measured checkout is dirty. That checkout is
the one people work in, the probes run under kill timeouts, and plain
``git status`` takes ``index.lock`` to rewrite a stale index: a probe killed
mid-scan leaves a lock that blocks every Git writer there.
"""
import os

import pytest

from evaluations.swebench import harbor_runner
from lib import benchmark_contract
from tests.support.git_index_probe import stat_dirty_checkout

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('writer_holds_lock', [False, True])
def test_environment_snapshot_reads_status_without_the_index_lock(tmp_path, writer_holds_lock):
    probe = stat_dirty_checkout(tmp_path / 'checkout', writer_holds_lock=writer_holds_lock)

    snapshot = benchmark_contract.environment_snapshot(cwd=str(probe.root))

    assert snapshot['gitDirty'] is False and len(snapshot['gitCommit']) == 40
    assert probe.unchanged()
    assert probe.lock.exists() is writer_holds_lock


def test_clean_runner_gate_reads_status_without_the_index_lock(tmp_path, monkeypatch):
    probe = stat_dirty_checkout(tmp_path / 'checkout')
    monkeypatch.chdir(probe.root)
    monkeypatch.setattr(harbor_runner, '_child_environment_without', lambda names: dict(os.environ))

    assert harbor_runner._git_dirty() is False
    assert probe.unchanged() and not probe.lock.exists()
