"""benchmarks/startup_recovery_batch.py stays runnable (docs/HARNESS_PERFORMANCE.md)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]


def test_the_recovery_benchmark_measures_restart_paused_work_on_a_real_authority(tmp_path):
    environment = {name: value for name, value in os.environ.items()
                   if name not in ('TOFU_PROJECT_PATH', 'TOFUDB_CONNECTION_FILE', 'PORT', 'TOFU_PROCESS_ROLE')}
    environment.update(PYTHONPATH=str(ROOT), TMPDIR=str(tmp_path))
    completed = subprocess.run(
        [sys.executable, 'benchmarks/startup_recovery_batch.py', str(tmp_path / 'scratch'),
         '--items', '4', '--projects', '2', '--runs', '2', '--backend', 'sqlite'],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{"')]
    summary = lines[-1]
    assert summary['items'] == 4 and len(summary['runs']) == 2
    assert all(line['recovered'] == 0 for line in lines[:-1]), 'paused work stays active'
    assert Path(summary['checkout']) == ROOT
    assert not any((tmp_path / 'scratch').iterdir())
