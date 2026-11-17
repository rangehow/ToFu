"""Executable specification of benchmarks/startup_idle_dispatch.py's isolation.

The benchmark starts real managers and hosts; these checks cover only the
environment it hands them, which is what keeps a run away from a live Tofu.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'benchmarks'))

import startup_idle_dispatch as bench  # noqa: E402

pytestmark = pytest.mark.unit


def test_instance_environment_carries_no_live_identity(monkeypatch, tmp_path):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    for name, value in {'TOFU_PROJECT_PATH': '/live/checkout', 'TOFU_PROCESS_ROLE': 'execution',
                        'TOFU_STORAGE_CONNECTION_FILE': '/live/connection.json',
                        'PORT': '15000', 'TOFU_SUPERVISOR_PORT': '15001'}.items():
        monkeypatch.setenv(name, value)
    instance = bench.Instance('sqlite')
    try:
        env = instance.env
        assert env['TOFU_PROJECT_PATH'] == str(bench.ROOT)
        assert not {'TOFU_PROCESS_ROLE', 'TOFU_STORAGE_CONNECTION_FILE'} & set(env)
        assert {int(env['PORT']), int(env['TOFU_SUPERVISOR_PORT'])}.isdisjoint(bench.LIVE_PORTS)
        for private in ('TOFU_HEARTBEAT_DIR', 'TMPDIR'):
            assert Path(env[private]).is_relative_to(instance.scratch)
    finally:
        shutil.rmtree(instance.scratch, ignore_errors=True)


def test_a_source_changing_restart_refuses_a_git_checkout():
    """--land-source appends to application source; only an exported copy may be edited."""
    assert (bench.ROOT / '.git').exists()
    with pytest.raises(SystemExit, match='exported copy'):
        bench.restart(object(), land_source=True)


def test_summary_reports_median_and_samples():
    assert bench._summary([3.0, 1.0, 2.0]) == {
        'median': 2.0, 'min': 1.0, 'max': 3.0, 'samples': [3.0, 1.0, 2.0]}
