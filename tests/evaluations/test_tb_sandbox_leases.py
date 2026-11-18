"""The TB runner's sandbox-857 lease hooks (Tofu-wide ledger, coordinator 58356dc1).

With TOFU_857_LEASES (the ledger's leases.py CLI) and TOFU_857_LANE set, every VM start is admitted by the ledger first
(a refusal waits, never starts), a failed or slow start is reported, and a session's lease is released when it stops.
Unset, the runner makes no ledger call.
"""
import json
import sys
import threading

import pytest

from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit

FAKE_CLI = """
import json, sys
from pathlib import Path
here = Path(__file__).parent
with (here / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
codes = json.loads((here / "codes.json").read_text())
if sys.argv[1] == "acquire" and codes:
    code = codes.pop(0)
    (here / "codes.json").write_text(json.dumps(codes))
    sys.exit(code)
"""


class FakeSession:
    ingress_auth = "refused"  # gateway.SandboxSession.start's tokenless-call evidence
    starts = []

    def __init__(self, endpoint, token, session_id=None):
        self.session_id = session_id or f"sid-{len(FakeSession.starts)}-{id(self)}"

    def start(self, body):
        FakeSession.starts.append(self.session_id)
        behaviour = FakeSession.plan.pop(0) if FakeSession.plan else "ok"
        if behaviour == "fail":
            raise RuntimeError("NodeResourceExhausted: no node available")
        return {}

    def stop(self):
        return "stopped"


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    cli = tmp_path / "leases.py"
    cli.write_text(FAKE_CLI)
    (tmp_path / "codes.json").write_text("[]")
    monkeypatch.setenv("TOFU_857_LEASES", str(cli))
    monkeypatch.setenv("TOFU_857_LANE", "EVAL-MATRIX-2")
    monkeypatch.setattr(runner, "SandboxSession", FakeSession)
    monkeypatch.setattr(runner, "LEASE_RETRY_SEC", 0.01)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    FakeSession.starts, FakeSession.plan = [], []

    def calls():
        path = tmp_path / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def set_codes(codes):
        (tmp_path / "codes.json").write_text(json.dumps(codes))

    return calls, set_codes


def bare_runner(tmp_path):
    live = object.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path, tasks=[], arms=["opencode"], protocol="matrix-tb21",
        model="glm-5.2", relay_url="http://r:1", files_url="http://r:1/files", artifacts_manifest=tmp_path / "a.json",
        endpoint="https://g/system/857", image="i", branch="b", proxy="http://p:1", max_alive_min=240,
        max_attempts=3, context_tokens=1, poll_interval_sec=1, run_label="a1")
    live.protocol = harness.PROTOCOLS["matrix-tb21"]
    live.token = "t"
    live.lock = threading.Lock()
    live.stop_requested = threading.Event()
    live.leases = runner.SandboxLeases.from_env()
    return live


def kinds(calls):
    return [call[0] for call in calls]


def flag(call, name):
    for item in call:
        if item.startswith(f"--{name}="):
            return item.split("=", 1)[1]
    return call[call.index(f"--{name}") + 1]


def test_a_refused_lease_waits_and_the_vm_starts_only_after_the_grant(tmp_path, ledger):
    calls, set_codes = ledger
    set_codes([3, 3, 0])
    live = bare_runner(tmp_path)
    session = live.start_session(FakeSession("e", "t"), tmp_path / "attempt-1", {}, "task/opencode#1")
    assert session is not None and FakeSession.starts == [session.session_id]
    assert kinds(calls()) == ["acquire", "acquire", "acquire"]
    first = calls()[0]
    assert flag(first, "lane") == "EVAL-MATRIX-2" and flag(first, "max-alive-min") == "240"
    assert flag(first, "session") == session.session_id


def test_a_failed_start_is_reported_released_and_the_retry_acquires_again(tmp_path, ledger):
    calls, _ = ledger
    FakeSession.plan = ["fail", "ok"]
    live = bare_runner(tmp_path)
    session = live.start_session(FakeSession("e", "t"), tmp_path / "attempt-1", {}, "task/opencode#1")
    failed, retried = FakeSession.starts
    assert session.session_id == retried != failed
    assert kinds(calls()) == ["acquire", "failure", "release", "acquire"]
    failure, released = calls()[1], calls()[2]
    assert flag(failure, "session") == failed and flag(failure, "kind") == "start_error"
    assert "NodeResourceExhausted" in flag(failure, "error")
    assert flag(released, "session") == failed and flag(released, "outcome") == "start_failed"


def test_a_slow_start_is_reported_and_keeps_its_lease(tmp_path, ledger, monkeypatch):
    calls, _ = ledger
    monkeypatch.setattr(runner, "SLOW_START_SEC", -1.0)
    live = bare_runner(tmp_path)
    session = live.start_session(FakeSession("e", "t"), tmp_path / "attempt-1", {}, "task/opencode#1")
    assert kinds(calls()) == ["acquire", "failure"]
    assert flag(calls()[1], "kind") == "slow_start" and flag(calls()[1], "session") == session.session_id


def test_release_after_the_stop_drops_the_lease(tmp_path, ledger):
    calls, _ = ledger
    live = bare_runner(tmp_path)
    state = {"session_id": "sid-x", "phase": "scored"}
    live.release(tmp_path / "attempt-1", state, FakeSession("e", "t", session_id="sid-x"))
    assert state["stopped"] is True
    assert calls() == [["release", "--session", "sid-x", "--outcome", "stopped"]]


def test_without_a_ledger_the_runner_makes_no_ledger_call(tmp_path, ledger, monkeypatch):
    calls, _ = ledger
    monkeypatch.delenv("TOFU_857_LEASES")
    live = bare_runner(tmp_path)
    assert live.start_session(FakeSession("e", "t"), tmp_path / "attempt-1", {}, "task/opencode#1") is not None
    live.release(tmp_path / "attempt-1", {"session_id": "sid-y"}, FakeSession("e", "t", session_id="sid-y"))
    assert calls() == []


def test_a_stop_request_while_waiting_for_a_lease_starts_nothing(tmp_path, ledger, monkeypatch):
    calls, set_codes = ledger
    set_codes([3] * 50)
    live = bare_runner(tmp_path)
    monkeypatch.setattr(runner, "LEASE_RETRY_SEC", 0.05)
    threading.Timer(0.2, live.stop_requested.set).start()
    state = {}
    assert live.start_session(FakeSession("e", "t"), tmp_path / "attempt-1", state, "task/opencode#1") is None
    assert state["phase"] == "interrupted" and FakeSession.starts == []
    assert set(kinds(calls())) == {"acquire"}
