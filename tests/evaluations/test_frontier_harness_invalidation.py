"""Operator invalidation of an attempt that measured an outage (runner verdict, CLI, re-run, summary)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit


def small_runner(tmp_path: Path) -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\ncpus = 1\nmemory_mb = 2048\n')
    (task_dir / "instruction.md").write_text("x\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"], arms=["codex"],
        protocol="fhe-v1", model="kimi-k3", relay_url="http://r:1", files_url="http://r:1/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://p:1", max_alive_min=150, max_attempts=2, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="cal")
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.stop_requested = threading.Event()
    live.lock = threading.Lock()
    live.log = lambda *args: None
    runner.write_json(live.config.work_dir / "run.json", {"tasks": ["regex-log"], "arms": ["codex"],
                                                          "max_attempts": 2})
    return live


def test_an_invalidated_measured_attempt_is_re_run_and_the_summary_follows(tmp_path, monkeypatch):
    live = small_runner(tmp_path)
    started = []

    def run_attempt(task, arm, attempt, directory, state):
        started.append(attempt)
        return dict(state, phase="scored", reward=1.0, session_id=f"s{attempt}", stopped=True)

    monkeypatch.setattr(live, "run_attempt", run_attempt)
    first = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(first / "state.json", {"phase": "scored", "reward": 0.0, "finished": True,
                                              "session_id": "s0", "stopped": True})
    assert live.run_trial("regex-log", "codex")["reward"] == 0.0 and started == []
    verdict = runner.invalidate(first, rule="D-2", evidence=["relay 401 at 16:50:05"], decided_by="test")
    assert verdict["phase_at_invalidation"] == "scored" and verdict["reward_at_invalidation"] == 0.0
    counts = runner.summarize(live.config.work_dir)["arms"]["codex"]
    assert counts["measured"] == 0 and counts["in_progress"] == 1
    assert live.run_trial("regex-log", "codex")["reward"] == 1.0 and started == [2]
    counts = runner.summarize(live.config.work_dir)["arms"]["codex"]
    assert counts["measured"] == 1 and counts["passed"] == 1
    assert json.loads((first / "state.json").read_text())["reward"] == 0.0, "the attempt's records are kept"


def test_invalidate_refuses_an_attempt_that_may_still_run_or_is_already_invalidated(tmp_path):
    attempt = tmp_path / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "running", "session_id": "s1"})
    with pytest.raises(ValueError, match="not finished"):
        runner.invalidate(attempt, rule="D-2", evidence=["x"], decided_by="t")
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "session_id": "s1"})
    with pytest.raises(ValueError, match="stop not confirmed"):
        runner.invalidate(attempt, rule="D-2", evidence=["x"], decided_by="t")
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "session_id": "s1",
                                               "stopped": True})
    runner.invalidate(attempt, rule="D-2", evidence=["x"], decided_by="t")
    with pytest.raises(ValueError, match="already invalidated"):
        runner.invalidate(attempt, rule="D-2", evidence=["y"], decided_by="t")
    assert runner.invalidation(attempt)["evidence"] == ["x"]


def test_the_invalidate_command_writes_the_verdict(tmp_path):
    from evaluations.terminal_bench.__main__ import main
    attempt = tmp_path / "work" / "trials" / "codex" / "regex-log" / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "stopped": True,
                                               "session_id": "s"})
    assert main(["invalidate", "--work-dir", str(tmp_path / "work"), "--arm", "codex", "--task", "regex-log",
                 "--attempt", "1", "--rule", "D-2", "--evidence", "a", "--evidence", "b",
                 "--decided-by", "t"]) == 0
    assert runner.invalidation(attempt)["evidence"] == ["a", "b"]
