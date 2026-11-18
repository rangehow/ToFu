"""PREREG-2 Amendment 7 (v1.1): the relay's rate-limit holds do not consume agent budget.

The relay times every request's whole pass through its gates (key cool-down, request window, token window, the
queue on their locks included), logs it per call (``held_s``) and serves a per-trial running total, holds in
progress included. The agent deadline (vm_trial) and the runner's trial deadline move by exactly that total.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.terminal_bench import harness, runner, vm_trial

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------------------------------------ relay
def relay_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(origin="unused", provider="unused", key_suffixes="6672", files_dir=None,
                  log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model="kimi-k3",
                  temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model=None,
                  responses_model="kimi-k3", responses_max_output_tokens=None, max_rpm=None,
                  first_body_dir=None, anthropic_body_dir=None, preserve_pi_effort_markers=False,
                  stop_file=str(tmp_path / "STOP"))
    values.update(overrides)
    return SimpleNamespace(**values)


def ticking_clock():
    """A fake monotonic clock whose sleeps advance it one second per scheduler turn, so others run meanwhile."""
    state = {"now": 1000.0}

    async def sleep(seconds):
        remaining = seconds
        while remaining > 0:
            await asyncio.sleep(0)
            step = min(1.0, remaining)
            state["now"] += step
            remaining -= step

    return state, (lambda: state["now"]), sleep


def test_a_request_queued_behind_another_wait_is_held_for_both_and_logs_only_its_own_pause(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-a")])
    live = relay.Relay(relay_args(tmp_path))
    state, clock, sleep = ticking_clock()
    live.clock = clock
    live.window = relay.RequestWindow(1, clock=clock, sleep=sleep)

    async def through_gates(record):
        gate = live.enter_gates(record)
        try:
            record["rpm_wait_s"] = await live.window.acquire()
        finally:
            live.leave_gates(gate, record)

    first, second, third = ({"arm": "codex-a", "task": "regex-log"} for _ in range(3))
    asyncio.run(through_gates(first))

    async def together():
        await asyncio.gather(through_gates(second), through_gates(third))

    asyncio.run(together())
    assert (first["held_s"], second["held_s"], third["held_s"]) == (0.0, 60.0, 120.0)
    assert third["rpm_wait_s"] == 60.0, "the pause sum misses the minute spent queued on the window's lock"
    assert live.held_seconds(("codex-a", "regex-log")) == (180.0, 0)
    assert live.held_seconds(("tofu-a", "regex-log")) == (0.0, 0)


def test_the_held_total_counts_a_hold_in_progress_and_survives_a_relay_restart(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    log = tmp_path / "requests.jsonl"
    log.write_text("\n".join([
        json.dumps({"kind": "responses", "arm": "codex-a", "task": "regex-log", "held_s": 12.5}),
        json.dumps({"kind": "messages", "arm": "tofu-a", "task": "regex-log", "held_s": 3.0}),
        json.dumps({"kind": "responses", "arm": "codex-a", "task": "regex-log", "held_s": 0.25}),
        json.dumps({"kind": "rate_limited", "arm": "codex-a", "task": "regex-log", "pause_s": 90}),
        "not json"]) + "\n")
    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-a")])
    live = relay.Relay(relay_args(tmp_path))
    state, clock, _ = ticking_clock()
    live.clock = clock
    live.holding[object()] = (("codex-a", "regex-log"), clock() - 7.0)

    async def read(path):
        async with TestClient(TestServer(relay.build_app(live))) as client:
            reply = await client.get(path)
            return reply.status, await reply.json()

    status, body = asyncio.run(read("/arm/codex-a/task/regex-log/held"))
    assert status == 200 and body == {"arm": "codex-a", "task": "regex-log", "held_s": 19.75, "holding": 1}
    status, body = asyncio.run(read("/arm/tofu-a/task/other-task/held"))
    assert body["held_s"] == 0.0 and body["holding"] == 0


def test_dispatch_logs_the_hold_and_serves_the_same_total(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(_request):
        return web.json_response({"id": "r", "status": "completed", "model": "kimi-k3",
                                  "usage": {"input_tokens": 5, "output_tokens": 2}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-a")])
    live = relay.Relay(relay_args(tmp_path))
    state, clock, sleep = ticking_clock()
    live.clock = clock
    live.window = relay.RequestWindow(1, clock=clock, sleep=sleep)
    live.window.starts.append(clock() - 15.0)

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/responses", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/v1/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                reply = await client.post("/arm/codex-a/task/regex-log/v1/responses",
                                          json={"model": "kimi-k3", "input": "hi", "stream": False})
                assert reply.status == 200
                held = await client.get("/arm/codex-a/task/regex-log/held")
                return await held.json()

    body = asyncio.run(exercise())
    [call] = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()
              if json.loads(line).get("kind") == "responses"]
    assert call["held_s"] == 45.0 and call["rpm_wait_s"] == 45.0
    assert body["held_s"] == 45.0 and body["holding"] == 0
    restarted = relay.Relay(relay_args(tmp_path))
    assert restarted.held_seconds(("codex-a", "regex-log")) == (45.0, 0)


# ------------------------------------------------------------------------------------------------ vm_trial
@pytest.fixture
def local_docker(tmp_path, monkeypatch):
    """``docker exec`` stand-in whose command, like a container's, runs outside the client's tree."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\nshift\n"
        'while [ "$1" = -e ] || [ "$1" = -w ]; do shift 2; done\nshift\n'
        'pid=$("$@" < /dev/null > /dev/null 2>&1 & echo $!)\n'
        'while kill -0 "$pid" 2> /dev/null; do sleep 0.1; done\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(vm_trial, "KILL_GRACE_SEC", 1)
    return tmp_path


def run_program(local_docker, program, timeout_sec, extension):
    seconds = str(100000 + uuid.uuid4().int % 800000)
    marker = "tbn-test-" + uuid.uuid4().hex
    try:
        return vm_trial.exec_with_deadline("trial", program.format(n=seconds), {}, marker, timeout_sec,
                                           local_docker / "stdout", local_docker / "stderr", extension=extension)
    finally:
        for pid, (_, argv, _) in vm_trial._proc_table().items():
            if argv == ["sleep", seconds]:
                os.kill(pid, signal.SIGKILL)


def test_an_agent_that_needs_its_budget_plus_the_held_seconds_finishes(local_docker):
    outcome = run_program(local_docker, "sleep 2", 1, lambda: (4.0, False))
    assert outcome["exit_code"] == 0 and outcome["timed_out"] is False and outcome["extension_s"] == 4.0


def test_the_agent_is_stopped_at_its_budget_plus_the_held_seconds(local_docker):
    reads = []

    def extension():
        reads.append(time.time())
        return 1.5, False

    outcome = run_program(local_docker, "exec sleep {n}", 1, extension)
    assert outcome["timed_out"] is True and outcome["extension_s"] == 1.5
    assert 2.5 <= outcome["stopped_at_s"] < 2.8
    assert len(reads) == 2, "one read moves the deadline, the next one finds no new hold"


def test_a_hold_in_progress_keeps_moving_the_deadline_until_it_ends(local_docker):
    began = time.time()

    def extension():  # one hold that began with the agent and lasts 2 s
        held = min(time.time() - began, 2.0)
        return held, held < 2.0

    outcome = run_program(local_docker, "exec sleep {n}", 0.5, extension)
    assert outcome["timed_out"] is True and outcome["extension_s"] == pytest.approx(2.0)
    assert 2.5 <= outcome["stopped_at_s"] < 2.5 + vm_trial.RELAY_HELD_POLL_SEC + 0.3


def test_without_an_extension_the_deadline_is_the_budget(local_docker):
    outcome = run_program(local_docker, "exec sleep {n}", 1, None)
    assert outcome["timed_out"] is True and "extension_s" not in outcome and 1.0 <= outcome["stopped_at_s"] < 1.3


class HeldServer:
    """A relay stand-in serving ``held_s``/``holding`` from a list (an exception entry fails that read)."""

    def __init__(self, replies):
        self.replies, self.hits = list(replies), 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                reply = outer.replies.pop(0) if outer.replies else outer.last
                outer.last = reply
                if isinstance(reply, Exception):
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps({"held_s": reply[0], "holding": reply[1]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.last = (0.0, 0)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/arm/codex-a/task/regex-log/held"

    def close(self):
        self.server.shutdown()


def test_relay_hold_subtracts_earlier_attempts_and_keeps_its_last_value_when_a_read_fails(monkeypatch):
    monkeypatch.setattr(vm_trial, "RELAY_HELD_RETRY_SEC", 0)
    server = HeldServer([(40.0, 0), (52.5, 1), RuntimeError(), RuntimeError(), RuntimeError(), (41.0, 0)])
    try:
        hold = vm_trial.RelayHold(server.url)
        assert hold.base == 40.0
        assert hold() == (12.5, True)
        assert hold() == (12.5, True), "three failed reads keep the last extension"
        assert hold() == (12.5, False), "a lower total (a restart lost a hold in progress) never shrinks it"
        assert (hold.reads, hold.errors) == (6, 3)
    finally:
        server.close()


def test_relay_hold_refuses_to_start_without_a_baseline(monkeypatch):
    monkeypatch.setattr(vm_trial, "RELAY_HELD_RETRY_SEC", 0)
    with pytest.raises(OSError):
        vm_trial.RelayHold("http://127.0.0.1:9/arm/codex-a/task/regex-log/held")


# ------------------------------------------------------------------------------------------------ runner
def hold_runner(tmp_path: Path, protocol: str) -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\ncpus = 1\nmemory_mb = 2048\n')
    (task_dir / "instruction.md").write_text("x\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"], arms=["tofu", "codex"],
        protocol=protocol, model="kimi-k3", relay_url="http://r:1", files_url="http://r:1/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://p:1", max_alive_min=480, max_attempts=2, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="v11-a1")
    live.protocol = harness.PROTOCOLS[protocol]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    artifact = harness.Artifact(name="x", filename="x.tar.gz", sha256="0" * 64, version="v")
    live.artifacts = {"tofu": artifact, "codex": artifact}
    return live


def test_v11_hold_specs_name_the_trials_relay_total_and_stop_both_arms_at_the_runner_deadline(tmp_path):
    live = hold_runner(tmp_path, "fhe-v11-hold")
    task = live.tasks["regex-log"]
    tofu, codex = live.spec_for(task, "tofu", 1), live.spec_for(task, "codex", 1)
    assert tofu["agent"]["relay_held_url"] == "http://r:1/arm/tofu-v11-a1/task/regex-log/held"
    assert codex["agent"]["relay_held_url"] == "http://r:1/arm/codex-v11-a1/task/regex-log/held"
    assert tofu["agent_timeout_sec"] == codex["agent_timeout_sec"] == 900
    assert tofu["agent"]["deadline_grace_sec"] == codex["agent"]["deadline_grace_sec"] == 0
    assert "--timeout-s 28800" in tofu["agent"]["program"], "Tofu's own timer sits at the VM lifetime"


def test_v11_specs_without_the_amendment_are_unchanged(tmp_path):
    live = hold_runner(tmp_path, "fhe-v11")
    tofu = live.spec_for(live.tasks["regex-log"], "tofu", 1)
    assert "relay_held_url" not in tofu["agent"] and tofu["agent"]["deadline_grace_sec"] == 120
    assert "--timeout-s 900" in tofu["agent"]["program"]


def test_the_runner_trial_deadline_moves_by_the_relay_total(tmp_path, monkeypatch):
    live = hold_runner(tmp_path, "fhe-v11-hold")
    spec = live.spec_for(live.tasks["regex-log"], "codex", 1)
    budget = runner.trial_budget_sec(spec)
    clock = {"now": 5000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(live, "relay_held", lambda spec: 300.0)
    live.stop_requested = threading.Event()
    killed = []
    session = SimpleNamespace(status=lambda exec_id: {"state": "running"},
                              kill=lambda exec_id, force: killed.append(clock["now"]))
    state = {"exec_id": "e1", "submitted_epoch": 5000.0}
    live.save = lambda directory, state: None
    result = live.wait(session, spec, tmp_path, state, "t")
    assert result["phase"] == "deadline" and killed == [pytest.approx(5000.0 + budget + 300.0, abs=1.5)]
