"""The SQLite write-pattern probe stays runnable and keeps its measured fact.

SQLite writes every WAL frame as two calls (24-byte header, then the page);
docs/STORAGE.md's write-call decision rests on that, and on DolphinFS each
call is a round trip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'benchmarks' / 'sqlite_write_pattern.py'


def _probe(env: dict, root: Path, *args: str) -> dict:
    result = subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), *args],
                            env=env, capture_output=True, text=True, timeout=120, check=True)
    return json.loads(result.stdout)


def test_wal_frames_cost_two_write_calls_each_and_scratch_is_removed(tmp_path):
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('a C compiler is required to build the faultfs shim')
    shim = tmp_path / 'faultfs.so'
    subprocess.run([compiler, '-O2', '-shared', '-fPIC', '-o', str(shim),
                    str(ROOT / 'benchmarks' / 'agent_storage' / 'faultfs.c'), '-ldl'], check=True)
    root = tmp_path / 'medium'
    env = {**os.environ, 'LD_PRELOAD': str(shim), 'FAULTFS_ROOT': str(root), 'FAULTFS_COUNT_IO': '1'}
    report = _probe(env, root, '--variant', 'nockpt', '--jobs', '40', '--turns', '10', '--events', '1000')

    assert report['commits'] == 40 and report['barriers'] >= 40
    frames = report['wal_frames_at_end']
    assert 2 * frames <= report['writes'] <= 2 * frames + 16
    assert os.listdir(root) == []


def test_frame_probe_compares_split_and_coalesced_commits(tmp_path):
    report = _probe(dict(os.environ), tmp_path, '--variant', 'frames', '--frames', '3', '--jobs', '4')

    assert report['split_calls'] == 6 and report['coalesced_calls'] == 1
    assert report['split_ms_p50'] > 0 and report['coalesced_ms_p50'] > 0
    assert os.listdir(tmp_path) == []
