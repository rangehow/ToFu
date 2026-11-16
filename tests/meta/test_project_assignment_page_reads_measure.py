"""The retained assignment-page benchmark runs and checks its pages against its oracle.

It reuses the dispatch test fixtures outside pytest, so a fixture signature change
breaks it silently unless something runs it.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.api

ROOT = Path(__file__).resolve().parents[2]


def test_page_benchmark_reports_reads_and_round_trips_for_every_build(tmp_path):
    environment = {**os.environ, 'PYTHONPATH': str(ROOT), 'TMPDIR': str(tmp_path)}
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks/project_assignment_page_reads.py'), '--repository', str(ROOT),
         '--backend', 'sqlite', '--rows', '3', '--projection-bytes', '4096', '--builds', '2'],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=240)
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout[completed.stdout.index('{'):])
    builds = report['builds']
    assert [build['build'] for build in builds] == [0, 1, 'one-turn-changed']
    assert report['warm_median_ms'] >= 0
    for build in builds:
        assert build['rpcs'] >= build['round_trips'] >= 1
        assert sum(build['operations'].values()) == build['rpcs']
