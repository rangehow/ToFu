"""The rollback drill's crash probe kills a real engine inside its packed-filter build.

Opt-in (`slow`): needs a source-matched engine (`TOFU_TEST_TOFUDB_BIN`) and
`TOFU_TEST_CRASH_PROBE_SOURCES`, os.pathsep-separated drill sources (data roots
holding a settled `tofudb-authority/`, such as the gate's load output on a
production-shaped fixture). Each source is hard-link-snapshotted exactly as
the gate snapshots it, so the source itself is never written.

On the compact production store the build finished in about 0.4 s, before
the probe's former fixed 5 s sleep ended, so every engine failed the drill;
the probe now follows the engine's `packed_filter_build_pause` failpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.engine_certification import gate

pytestmark = pytest.mark.slow

REPOSITORY = Path(__file__).resolve().parents[2]
ENGINE = os.environ.get('TOFU_TEST_TOFUDB_BIN')
SOURCES = [Path(raw) for raw in os.environ.get('TOFU_TEST_CRASH_PROBE_SOURCES', '').split(os.pathsep) if raw]


@pytest.mark.skipif(not ENGINE or not SOURCES,
                    reason='needs TOFU_TEST_TOFUDB_BIN and TOFU_TEST_CRASH_PROBE_SOURCES')
@pytest.mark.timeout(1800)  # the probe's own marker waits (600 s each) bound it
@pytest.mark.parametrize('source', SOURCES, ids=[path.name for path in SOURCES])
def test_the_crash_probe_kills_inside_the_packed_filter_build_after_its_three_commits(source, tmp_path):
    root = tmp_path / 'crash'  # hard links: the source must share tmp_path's filesystem
    try:
        gate.snapshot_authority(source / 'tofudb-authority', root / 'tofudb-authority')
        out = root / 'probe-crash.json'
        completed = subprocess.run(
            [sys.executable, REPOSITORY / 'scripts/engine_certification/probes.py', 'crash',
             '--checkout', REPOSITORY, '--engine', ENGINE, '--root', root, '--out', out],
            capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr[-4000:]
        result = json.loads(out.read_text())
        stderr = out.with_suffix('.stderr.jsonl').read_text(errors='replace')
        assert result.get('paused_inside_build') is True, result
        assert result['built_before_kill'] is False
        assert [commit['label'] for commit in result['commits']] == [
            'before-first-table', 'during-build-1', 'during-build-2']
        assert all(commit['receipt'] for commit in result['commits'])
        assert 'step=paused failpoint=packed_filter_build_pause' in stderr
        assert 'step=built' not in stderr
        _, summary = gate.drill_verdict({'crash': {**result, 'rc': 0}}, {})
        assert not [problem for problem in summary['problems'] if 'crash probe' in problem]
    finally:
        shutil.rmtree(root, ignore_errors=True)
