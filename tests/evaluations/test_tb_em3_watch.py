"""EVAL-MATRIX-3 attention watch: a silent trial inside its task's declared time budget is progressing, not stuck.

2026-11-14 09:11: the watch flagged grok-4.6/a1 (no attempt outcome and no model call for 45+ min). Its one in-flight
trial, install-windows-3.11, waited silently on a long QEMU install and ended by itself at its task's agent timeout
(scored 1.0 at 09:18). A trial's budget is its task.toml's agent + verifier timeouts plus the environment build timeout,
counted from submission; past it, the runner is stuck.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

TOOL = Path("/path/to/your/project"
            "tools/em3_watch.py")
spec = importlib.util.spec_from_file_location("em3_watch", TOOL)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

pytestmark = pytest.mark.unit


def make_run(tmp_path: Path, submitted_minutes_ago: float, agent_s: float, finished: bool = False) -> Path:
    tasks = tmp_path / "tasks" / "install-windows-3.11"
    tasks.mkdir(parents=True)
    (tasks / "task.toml").write_text(f"[verifier]\ntimeout_sec = 600.0\n[agent]\ntimeout_sec = {agent_s}\n"
                                     "[environment]\nbuild_timeout_sec = 600.0\n")
    work = tmp_path / "runs" / "grok-4.6" / "a1"
    attempt = work / "trials" / "opencode" / "install-windows-3.11" / "attempt-1"
    attempt.mkdir(parents=True)
    (work / "run.json").write_text(json.dumps({"tasks_dir": str(tmp_path / "tasks")}))
    submitted = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60 * submitted_minutes_ago))
    (attempt / "state.json").write_text(json.dumps({"submitted_at": submitted, "finished": finished}))
    return work


def test_a_silent_trial_inside_its_task_budget_is_progressing(tmp_path):
    assert watch.within_task_budget(make_run(tmp_path, submitted_minutes_ago=56, agent_s=3600))


def test_a_trial_past_its_task_budget_is_stuck(tmp_path):
    assert not watch.within_task_budget(make_run(tmp_path, submitted_minutes_ago=56, agent_s=1200))


def test_finished_attempts_do_not_count_as_in_flight(tmp_path):
    assert not watch.within_task_budget(make_run(tmp_path, submitted_minutes_ago=5, agent_s=3600, finished=True))
