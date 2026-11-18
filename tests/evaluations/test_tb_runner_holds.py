"""Runner holds and attempt accounting around the sandbox lease wait (EVAL-MATRIX-2, 2026-11-13 evening).

- A stop file (or --finish-by) is honored at every point before a VM starts, including after a long lease wait:
  the attempt is held without a VM and is not spent.
- An attempt a stopped runner left in its lease wait (a session id, no VM, no lease in the ledger) is reset on
  resume instead of being released as resume_uncertain, so a runner restart never spends an attempt.
- --finish-by holds an attempt whose maximum wall time (start timeout + the trial budget the runner enforces) would
  end after it; it is a per-launch constraint, never frozen into a resumed run.
- An operator can invalidate a finished attempt that measured an outage: it keeps its records and is re-run.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from evaluations.terminal_bench import gateway, harness, runner

pytestmark = pytest.mark.unit


class FakeLeases:
    """The runner's view of the sandbox-857 ledger: grants per a script, remembers what it was asked."""

    def __init__(self, grants=None, held_sessions=()):
        self.grants = list(grants or [])
        self.held_sessions = set(held_sessions)
        self.calls = []
        self.enabled = True

    def acquire(self, session_id, label, max_alive_min):
        self.calls.append(("acquire", session_id))
        granted = self.grants.pop(0) if self.grants else True
        if granted:
            self.held_sessions.add(session_id)
        return granted

    def release(self, session_id, outcome):
        self.calls.append(("release", session_id, outcome))
        self.held_sessions.discard(session_id)

    def failure(self, session_id, error, kind):
        self.calls.append(("failure", session_id, kind))

    def held(self, session_id):
        return session_id in self.held_sessions


class FakeSession:
    ingress_auth = "refused"  # gateway.SandboxSession.start's tokenless-call evidence
    started = []

    def __init__(self, endpoint, token, session_id=None):
        self.session_id = session_id or f"sid-{len(FakeSession.started)}-{id(self) % 10000}"

    def start(self, body):
        FakeSession.started.append(self.session_id)
        return {}

    def stop(self):
        return "stopped"


def make_runner(tmp_path: Path, *, stop_file=None, finish_by=None, leases=None, agent_timeout=900.0) -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(f'[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = {agent_timeout}\n'
                                        '[environment]\ndocker_image = "a/b:1"\ncpus = 1\nmemory_mb = 2048\n')
    (task_dir / "instruction.md").write_text("x\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    optional = {"finish_by": finish_by} if finish_by is not None else {}
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"], arms=["codex"],
        protocol="fhe-v1", model="kimi-k3", relay_url="http://r:1", files_url="http://r:1/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://p:1", max_alive_min=600, max_attempts=3, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="a1", stop_file=str(stop_file) if stop_file else None, **optional)
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.stop_requested = threading.Event()
    live.lock = threading.Lock()
    live.token = "t"
    live.leases = leases or FakeLeases()
    live.log = lambda *args: None
    runner.write_json(live.config.work_dir / "run.json", {"tasks": ["regex-log"], "arms": ["codex"], "max_attempts": 3})
    return live


@pytest.fixture(autouse=True)
def fake_sandbox(monkeypatch):
    FakeSession.started = []
    monkeypatch.setattr(runner, "SandboxSession", FakeSession)
    monkeypatch.setattr(runner, "LEASE_RETRY_SEC", 0.01)


def stub_spec(live, monkeypatch, budget_sec=3600.0):
    monkeypatch.setattr(live, "spec_for", lambda task, arm, attempt: {"trial_id": f"{arm}-{task}-{attempt}"})
    monkeypatch.setattr(runner, "trial_budget_sec", lambda spec: budget_sec)


def test_a_stop_file_written_during_the_lease_wait_holds_the_attempt_without_a_vm_or_a_spent_attempt(tmp_path,
                                                                                                    monkeypatch):
    stop = tmp_path / "STOP"
    leases = FakeLeases(grants=[False, False, False])
    live = make_runner(tmp_path, stop_file=stop, leases=leases)
    stub_spec(live, monkeypatch)
    real_acquire = leases.acquire

    def acquire(session_id, label, max_alive_min):
        if len([c for c in leases.calls if c[0] == "acquire"]) == 1:
            stop.write_text("{}")  # the relay's first 429 (or an operator hold) lands while this attempt waits
        return real_acquire(session_id, label, max_alive_min)

    monkeypatch.setattr(leases, "acquire", acquire)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    assert FakeSession.started == [] and not (attempt / "state.json").exists(), "not spent: the attempt starts later"
    assert [c[0] for c in leases.calls if c[0] == "acquire"] == ["acquire", "acquire"]


def test_a_hold_that_lands_after_the_lease_grant_releases_the_lease_before_any_vm_starts(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    leases = FakeLeases(grants=[True])
    live = make_runner(tmp_path, stop_file=stop, leases=leases)
    stub_spec(live, monkeypatch)
    real_acquire = leases.acquire

    def acquire(session_id, label, max_alive_min):
        granted = real_acquire(session_id, label, max_alive_min)
        stop.write_text("{}")
        return granted

    monkeypatch.setattr(leases, "acquire", acquire)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert FakeSession.started == []
    assert ("release", leases.calls[0][1], "never_started") in leases.calls and not leases.held_sessions


def test_the_maximum_wall_time_counts_the_start_and_submit_timeouts_and_the_enforced_budget():
    assert runner.max_wall_sec(3600.0) == 2 * gateway.START_TIMEOUT_SEC + 3600.0


def test_finish_by_holds_an_attempt_whose_maximum_wall_time_ends_after_it_and_starts_one_that_fits(tmp_path,
                                                                                                  monkeypatch):
    budget = 2 * 3600.0
    late = make_runner(tmp_path / "late", finish_by=time.time() + 2 * gateway.START_TIMEOUT_SEC + budget - 60)
    stub_spec(late, monkeypatch, budget)
    assert late.run_trial("regex-log", "codex") == {"phase": "held"}
    assert FakeSession.started == [] and not late.leases.calls
    fits = make_runner(tmp_path / "fits", finish_by=time.time() + 2 * gateway.START_TIMEOUT_SEC + budget + 600)
    stub_spec(fits, monkeypatch, budget)
    started = []
    monkeypatch.setattr(fits, "run_attempt",
                        lambda task, arm, attempt, directory, state: started.append(attempt) or
                        dict(state, phase="scored", reward=1.0, session_id="s", stopped=True))
    assert fits.run_trial("regex-log", "codex")["phase"] == "scored" and started == [1]


def test_finish_by_is_checked_again_before_the_start_call_after_a_long_lease_wait(tmp_path, monkeypatch):
    budget = 3600.0
    leases = FakeLeases(grants=[False, True])
    live = make_runner(tmp_path, leases=leases,
                       finish_by=time.time() + 2 * gateway.START_TIMEOUT_SEC + budget + 120)
    stub_spec(live, monkeypatch, budget)
    clock = [time.time()]
    monkeypatch.setattr(runner.time, "time", lambda: clock[0])
    real_acquire = leases.acquire

    def acquire(session_id, label, max_alive_min):
        granted = real_acquire(session_id, label, max_alive_min)
        clock[0] += 300  # the wait for this grant outlasted the finish-by's slack
        return granted

    monkeypatch.setattr(leases, "acquire", acquire)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert FakeSession.started == [] and not leases.held_sessions


def test_an_attempt_left_in_its_lease_wait_is_reset_on_resume_not_spent(tmp_path, monkeypatch):
    leases = FakeLeases(held_sessions=())
    live = make_runner(tmp_path, leases=leases)
    stub_spec(live, monkeypatch)
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "starting", "session_id": "waited", "created_at": "x"})
    runner.write_json(attempt / "spec.json", {"trial_id": "codex-regex-log-1", "relay": "http://old-relay:1"})
    assert not live.holds_session("regex-log", "codex"), "no VM: not a resumed in-flight trial"
    started = []
    monkeypatch.setattr(live, "run_attempt",
                        lambda task, arm, n, directory, state: started.append((n, state.get("session_id"))) or
                        dict(state, phase="scored", reward=0.0, session_id="fresh", stopped=True))
    assert live.run_trial("regex-log", "codex")["phase"] == "scored"
    assert started == [(1, None)], "attempt 1 runs from scratch; nothing was spent"
    assert not (attempt / "spec.json").exists(), "its spec is rebuilt from the run's current config (relay moves)"
    assert len(list(attempt.glob("spec.reset-*.json"))) == 1 and len(list(attempt.glob("state.reset-*.json"))) == 1


def test_an_attempt_whose_lease_is_open_stays_uncertain_and_is_released(tmp_path, monkeypatch):
    leases = FakeLeases(held_sessions={"maybe-started"})
    live = make_runner(tmp_path, leases=leases)
    stub_spec(live, monkeypatch)
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(attempt / "spec.json", {"trial_id": "codex-regex-log-1"})
    runner.write_json(attempt / "state.json", {"phase": "starting", "session_id": "maybe-started"})
    assert live.holds_session("regex-log", "codex"), "the start may have created a VM: resume it at once"
    monkeypatch.setattr(live, "run_attempt", runner.Runner.run_attempt.__get__(live))
    monkeypatch.setattr(live, "release", lambda directory, state, session=None:
                        state.update(stopped=True, stop_outcome="stopped"))
    out = live.run_trial("regex-log", "codex")
    first = json.loads((attempt / "state.json").read_text())
    assert first["phase"] == "resume_uncertain" and first["finished"] is True and first["stopped"] is True
    assert out["phase"] in runner.MEASURED or out["phase"] in ("held", "unmeasured", "start_failed", "submit_error")


def test_an_invalidated_measured_attempt_is_re_run_and_the_summary_follows(tmp_path, monkeypatch):
    live = make_runner(tmp_path)
    started = []
    monkeypatch.setattr(live, "run_attempt",
                        lambda task, arm, n, directory, state: started.append(n) or
                        dict(state, phase="scored", reward=1.0, session_id=f"s{n}", stopped=True))
    first = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(first / "state.json", {"phase": "scored", "reward": 0.0, "finished": True,
                                              "session_id": "s0", "stopped": True})
    assert live.run_trial("regex-log", "codex")["reward"] == 0.0 and started == []
    verdict = runner.invalidate(first, rule="D-0733", evidence=["relay retired 0733 at 16:51:03"], decided_by="test")
    assert verdict["phase_at_invalidation"] == "scored" and verdict["reward_at_invalidation"] == 0.0
    counts = runner.summarize(live.config.work_dir)["arms"]["codex"]
    assert counts["measured"] == 0 and counts["in_progress"] == 1
    assert live.run_trial("regex-log", "codex")["reward"] == 1.0 and started == [2]
    counts = runner.summarize(live.config.work_dir)["arms"]["codex"]
    assert counts["measured"] == 1 and counts["passed"] == 1
    assert json.loads((first / "state.json").read_text())["reward"] == 0.0, "the attempt keeps its records"


def test_invalidate_refuses_an_attempt_that_may_still_run_or_is_already_invalidated(tmp_path):
    attempt = tmp_path / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "running", "session_id": "s1"})
    with pytest.raises(ValueError, match="not finished"):
        runner.invalidate(attempt, rule="D", evidence=["x"], decided_by="t")
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "session_id": "s1"})
    with pytest.raises(ValueError, match="stop not confirmed"):
        runner.invalidate(attempt, rule="D", evidence=["x"], decided_by="t")
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "session_id": "s1",
                                               "stopped": True})
    runner.invalidate(attempt, rule="D", evidence=["x"], decided_by="t")
    with pytest.raises(ValueError, match="already invalidated"):
        runner.invalidate(attempt, rule="D", evidence=["y"], decided_by="t")
    assert runner.invalidation(attempt)["evidence"] == ["x"]


def test_the_invalidate_command_writes_the_verdict(tmp_path):
    from evaluations.terminal_bench.__main__ import main
    attempt = tmp_path / "work" / "trials" / "codex" / "regex-log" / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "scored", "finished": True, "stopped": True, "session_id": "s"})
    assert main(["invalidate", "--work-dir", str(tmp_path / "work"), "--arm", "codex", "--task", "regex-log",
                 "--attempt", "1", "--rule", "D", "--evidence", "a", "--evidence", "b", "--decided-by", "t"]) == 0
    assert runner.invalidation(attempt)["evidence"] == ["a", "b"]


def test_finish_by_is_a_per_launch_constraint_never_frozen_into_a_resumed_run(tmp_path):
    from evaluations.terminal_bench import __main__ as cli
    work = tmp_path / "run"
    work.mkdir()
    (work / "run.json").write_text(json.dumps({"relay_url": "http://r:1", "max_attempts": 3, "finish_by": 1.0}))
    parser = cli.build_parser()
    with_flag = cli.frozen_config(parser.parse_args(["run", "--work-dir", str(work), "--concurrency", "1",
                                                     "--finish-by", "2026-11-13T21:55:00"]))
    assert with_flag["finish_by"] == pytest.approx(time.mktime(time.strptime("2026-11-13 21:55:00",
                                                                             "%Y-%m-%d %H:%M:%S")))
    without = cli.frozen_config(parser.parse_args(["run", "--work-dir", str(work), "--concurrency", "1"]))
    assert without["finish_by"] is None


# EVAL-MATRIX-3, 2026-11-14 (coordinator steer 0dd843ac): from 22:26 on 11-13, unkeyed runners kept a relay that had
# switched to keyed ingress; every attempt ended at install with the relay's 401 and no model call, and each spent
# one of its trial's three attempts (2,704 attempts, ~900 trials, by 06:05).
REFUSED = {"phase": "infra_error", "session_id": "s", "stopped": True,
           "infra_error": {"phase": "install", "message": "artifact download failed: HTTP Error 401: Unauthorized"}}


def scripted_attempts(live, monkeypatch, outcomes):
    """run_attempt ends with the scripted states in order, after leaving a result like a collected attempt."""
    calls = []

    def run_attempt(task, arm, attempt, directory, state):
        calls.append(attempt)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text("{}")
        return dict(outcomes[len(calls) - 1])

    monkeypatch.setattr(live, "run_attempt", run_attempt)
    return calls


def test_three_ingress_refused_attempts_in_a_row_hold_the_runner_and_spend_nothing(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    live = make_runner(tmp_path, stop_file=stop)
    stub_spec(live, monkeypatch)
    calls = scripted_attempts(live, monkeypatch, [REFUSED] * 6)
    for _ in range(3):
        assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert json.loads(stop.read_text())["reason"] == "broken_route"
    # A broken route starts no further VM, neither in this runner nor in a relaunch that finds the stop file.
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert calls == [1, 1, 1]
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    assert not (attempt / "state.json").exists()
    assert len(list(attempt.glob("state.ingress-refused-*.json"))) == 3
    assert len(list(attempt.glob("result.ingress-refused-*.json"))) == 3


def test_a_broken_route_holds_even_without_a_stop_file(tmp_path, monkeypatch):
    live = make_runner(tmp_path)
    stub_spec(live, monkeypatch)
    calls = scripted_attempts(live, monkeypatch, [REFUSED] * 6)
    for _ in range(4):
        assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert calls == [1, 1, 1]
    assert live.broken_route


def test_a_measured_attempt_ends_the_refusal_streak_and_the_refusals_spent_nothing(tmp_path, monkeypatch):
    live = make_runner(tmp_path)
    stub_spec(live, monkeypatch)
    scored = {"phase": "scored", "reward": 1.0, "session_id": "s", "stopped": True}
    calls = scripted_attempts(live, monkeypatch, [REFUSED, REFUSED, scored])
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert live.run_trial("regex-log", "codex")["phase"] == "scored"
    assert calls == [1, 1, 1]
    assert live.refused_in_a_row == 0 and live.broken_route is None


def test_other_install_failures_are_still_spent_attempts(tmp_path, monkeypatch):
    live = make_runner(tmp_path)
    stub_spec(live, monkeypatch)
    missing = dict(REFUSED, infra_error={"phase": "install",
                                         "message": "artifact download failed: HTTP Error 404: Not Found"})
    calls = scripted_attempts(live, monkeypatch, [missing] * 3)
    assert live.run_trial("regex-log", "codex") == {"phase": "unmeasured"}
    assert calls == [1, 2, 3]
    assert live.broken_route is None
