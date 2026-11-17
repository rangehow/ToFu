"""benchmarks/agent_round_overhead.py stays runnable and reports what it claims.

A three-round scripted session must finish, report one harness gap per round
and prove the request prefix (tools and system prompt) byte-stable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.api

ROOT = Path(__file__).resolve().parents[2]


def test_round_overhead_benchmark_reports_one_gap_per_round(tmp_path):
    report = tmp_path / 'report.json'
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks' / 'agent_round_overhead.py'),
         '--rounds', '3', '--json', str(report)],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
        env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert completed.returncode == 0, completed.stderr[-2000:]
    data = json.loads(report.read_text(encoding='utf-8'))
    assert Path(data['lib']).resolve() == (ROOT / 'lib' / '__init__.py').resolve()
    assert data['requests'] == 4
    assert data['round_gap']['n'] == 3 and len(data['gaps_ms']) == 3
    assert all(gap > 0 for gap in data['gaps_ms'])
    assert data['prefix']['tools_stable'] and data['prefix']['system_stable']
    assert 'read_files' in data['prefix']['tool_names']
