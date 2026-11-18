"""The native runner's per-attempt start deadline (--start-deadline): hold what cannot finish in time."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit

NOW = 1_794_570_000.0
SPEC = {"trial_id": "t", "agent": {"deadline_grace_sec": 60}, "agent_timeout_sec": 900,
        "verifier_timeout_sec": 900, "verifier_attempts": 1}
BUDGET = 900 + 60 + 900 + runner.TRIAL_OVERHEAD_SEC


def deadline_runner(tmp_path: Path, monkeypatch, start_deadline: float | None,
                    start_allowance_sec: float | None = None) -> tuple[runner.Runner, list]:
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
        poll_interval_sec=1, run_label="cal", start_deadline=start_deadline,
        start_allowance_sec=start_allowance_sec)
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.stop_requested = threading.Event()
    live.lock = threading.Lock()
    live.log = lambda *args: None
    live.spec_for = lambda task, arm, attempt: dict(SPEC)
    started = []

    def run_attempt(task, arm, attempt, directory, state):
        started.append(attempt)
        return dict(state, phase="scored", reward=1.0, session_id=state.get("session_id", "s1"), stopped=True)

    monkeypatch.setattr(live, "run_attempt", run_attempt)
    monkeypatch.setattr(runner.time, "time", lambda: NOW)
    return live, started


def test_an_attempt_that_cannot_end_by_the_deadline_is_held_and_leaves_no_trace(tmp_path, monkeypatch):
    live, started = deadline_runner(tmp_path, monkeypatch, NOW + runner.START_TIMEOUT_SEC + BUDGET - 1)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert started == [] and not (live.trial_dir("regex-log", "codex") / "attempt-1" / "state.json").exists()


def test_an_attempt_that_ends_exactly_at_the_deadline_starts(tmp_path, monkeypatch):
    live, started = deadline_runner(tmp_path, monkeypatch, NOW + runner.START_TIMEOUT_SEC + BUDGET)
    assert live.run_trial("regex-log", "codex")["phase"] == "scored" and started == [1]


def test_without_a_deadline_nothing_is_held(tmp_path, monkeypatch):
    live, started = deadline_runner(tmp_path, monkeypatch, None)
    assert live.run_trial("regex-log", "codex")["phase"] == "scored" and started == [1]


def test_a_resumed_running_attempt_is_never_held_by_the_deadline(tmp_path, monkeypatch):
    live, started = deadline_runner(tmp_path, monkeypatch, NOW - 1)
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "running", "session_id": "s0", "exec_id": "e0"})
    assert live.run_trial("regex-log", "codex")["phase"] == "scored" and started == [1]


def test_the_deadline_uses_the_attempts_frozen_spec_when_there_is_one(tmp_path, monkeypatch):
    live, started = deadline_runner(tmp_path, monkeypatch, NOW + runner.START_TIMEOUT_SEC + BUDGET)
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(attempt / "spec.json", dict(SPEC, agent_timeout_sec=5400))
    assert live.run_trial("regex-log", "codex") == {"phase": "held"} and started == []


def test_the_cli_parses_the_deadline_for_this_invocation_only(tmp_path, monkeypatch):
    from evaluations.terminal_bench import __main__ as cli
    work = tmp_path / "work"
    work.mkdir()
    frozen = {"tasks_dir": str(tmp_path), "tasks": ["regex-log"], "arms": ["codex"], "protocol": "fhe-v1",
              "model": "kimi-k3", "relay_url": "http://r:1", "files_url": "http://r:1/files",
              "artifacts_manifest": str(tmp_path / "a.json"), "endpoint": "https://g/system/857", "image": "x",
              "branch": "b", "proxy": "http://p:1", "max_alive_min": 240, "max_attempts": 2,
              "context_tokens": 1, "poll_interval_sec": 1, "run_label": "cal", "stop_file": None}
    (work / "run.json").write_text(json.dumps(frozen))
    seen = {}

    class FakeRunner:
        def __init__(self, config):
            seen["config"] = config

        def run(self, concurrency):
            return {"arms": {}}

    monkeypatch.setattr(cli, "require_system_ca_bundle", lambda artifacts: None)
    monkeypatch.setattr(cli, "load_artifacts", lambda path: {})
    monkeypatch.setattr(runner, "Runner", FakeRunner)
    assert cli.main(["run", "--work-dir", str(work), "--concurrency", "2", "--max-attempts", "2",
                     "--start-deadline", "2026-11-13T21:55:00+08:00"]) == 0
    assert seen["config"].start_deadline == datetime.fromisoformat("2026-11-13T21:55:00+08:00").timestamp()
    assert "start_deadline" not in json.loads((work / "run.json").read_text())
    assert cli.main(["run", "--work-dir", str(work), "--concurrency", "2", "--max-attempts", "2"]) == 0
    assert seen["config"].start_deadline is None


def test_an_observed_pre_agent_allowance_replaces_the_start_budget_and_the_fixed_overhead(tmp_path, monkeypatch):
    fits = NOW + 480 + BUDGET - runner.TRIAL_OVERHEAD_SEC
    live, started = deadline_runner(tmp_path, monkeypatch, fits - 1, start_allowance_sec=480)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"} and started == []
    live, started = deadline_runner(tmp_path / "b", monkeypatch, fits, start_allowance_sec=480)
    assert live.run_trial("regex-log", "codex")["phase"] == "scored" and started == [1]


def test_the_allowance_needs_a_deadline_and_reaches_the_runner(tmp_path, monkeypatch):
    from evaluations.terminal_bench import __main__ as cli
    work = tmp_path / "work"
    work.mkdir()
    frozen = {"tasks_dir": str(tmp_path), "tasks": ["regex-log"], "arms": ["codex"], "protocol": "fhe-v1",
              "model": "kimi-k3", "relay_url": "http://r:1", "files_url": "http://r:1/files",
              "artifacts_manifest": str(tmp_path / "a.json"), "endpoint": "https://g/system/857", "image": "x",
              "branch": "b", "proxy": "http://p:1", "max_alive_min": 240, "max_attempts": 2,
              "context_tokens": 1, "poll_interval_sec": 1, "run_label": "cal", "stop_file": None}
    (work / "run.json").write_text(json.dumps(frozen))
    seen = {}

    class FakeRunner:
        def __init__(self, config):
            seen["config"] = config

        def run(self, concurrency):
            return {"arms": {}}

    monkeypatch.setattr(cli, "require_system_ca_bundle", lambda artifacts: None)
    monkeypatch.setattr(cli, "load_artifacts", lambda path: {})
    monkeypatch.setattr(runner, "Runner", FakeRunner)
    with pytest.raises(SystemExit, match="needs --start-deadline"):
        cli.main(["run", "--work-dir", str(work), "--concurrency", "2", "--start-allowance-sec", "480"])
    assert cli.main(["run", "--work-dir", str(work), "--concurrency", "2", "--start-deadline",
                     "2026-11-13T21:55:00+08:00", "--start-allowance-sec", "480"]) == 0
    assert seen["config"].start_allowance_sec == 480.0
    assert "start_allowance_sec" not in json.loads((work / "run.json").read_text())
