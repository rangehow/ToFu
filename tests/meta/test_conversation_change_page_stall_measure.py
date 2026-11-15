"""Self-check of benchmarks/conversation_change_page_stall.py on a tiny fixture."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_one_arm_replays_the_fixture_and_reports_its_projection_work(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks/conversation_change_page_stall.py"),
         "--work-dir", str(tmp_path / "work"), "--arm", f"current={ROOT}",
         "--turns", "3", "--rounds", "2", "--pairs", "1", "--streams", "2",
         "--busy", "0", "--resident-objects", "0", "--json"],
        capture_output=True, text=True, timeout=600, cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    report = json.loads(completed.stdout)
    (summary,) = report["summary"]
    (full,) = report["reports"]

    # One Turn-pair upsert plus one attempt event per projection revision.
    assert report["events"] == 3 * (1 + 2 * 4)
    assert full["source"] == str(ROOT)
    assert full["loop"]["frames_per_stream"] == report["events"]
    assert len(full["loop"]["frame_data_sha256"]) == 64
    # This tree reads no patch content and derives each settled Turn once.
    assert summary["catch_up_visits_per_page"] == 0
    assert summary["steady_visits_per_event"] == 0
    assert summary["derivations"] == 3
    assert summary["loop_thread_starts"] == 0


def test_one_arm_sweeps_the_whole_translated_history_without_derivations(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks/conversation_change_page_stall.py"),
         "--work-dir", str(tmp_path / "work"), "--arm", f"current={ROOT}",
         "--turns", "3", "--rounds", "2", "--pairs", "1", "--resident-objects", "0",
         "--sweeps", "2", "--json"],
        capture_output=True, text=True, timeout=600, cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    report = json.loads(completed.stdout)
    (summary,) = report["summary"]
    (full,) = report["reports"]

    # Every user and reply row is scanned; every reply is already translated.
    assert summary["rows_per_sweep"] == report["events"] == 6
    assert full["sweep"]["spawned"] == [] and full["sweep"]["respawned"] == 0
    assert summary["derivations_per_sweep"] == 0
    assert len(summary["gc_collections_per_sweep"]) == 2
