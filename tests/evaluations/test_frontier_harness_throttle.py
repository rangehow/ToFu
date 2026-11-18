"""PREREG throttle controls: stop on the first upstream 429, hold new trials, account 429s per trial."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit


def relay_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(origin="unused", provider="unused", key_suffixes="0733,4861", files_dir=None,
                  log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model="kimi-k3",
                  temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model=None,
                  responses_model="kimi-k3", responses_max_output_tokens=None, max_rpm=None,
                  first_body_dir=None, anthropic_body_dir=None, preserve_pi_effort_markers=False,
                  stop_file=str(tmp_path / "STOP"))
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_first_upstream_429_writes_the_stop_file_once_and_the_call_still_completes(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    seen = []

    async def upstream(request):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return web.json_response({"error": {"message": "rate limited"}}, status=429)
        return web.json_response({"id": "r", "status": "completed", "model": "kimi-k3",
                                  "usage": {"input_tokens": 5, "output_tokens": 2}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("0733", "secret-a"), ("4861", "secret-b")])
    live = relay.Relay(relay_args(tmp_path, max_rpm=10))

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/responses", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/v1/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                reply = await client.post("/arm/codex-a/task/regex-log/v1/responses",
                                          json={"model": "kimi-k3", "input": "hi", "stream": False})
                return reply.status

    assert asyncio.run(exercise()) == 200
    stop = json.loads((tmp_path / "STOP").read_text())
    assert stop["reason"] == "upstream_429" and stop["arm"] == "codex-a" and stop["task"] == "regex-log"
    assert stop["key"] in {"0733", "4861"}
    rows = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    call = next(row for row in rows if row.get("kind") == "responses")
    assert call["status"] == 200 and [retry["status"] for retry in call["retries"]] == [429]
    assert sum(1 for row in rows if row.get("kind") == "stop_file_written") == 1
    # eval-quota rule: the throttled key rests past the next full minute + 60 s, the RPM budget halves once,
    # and the other key served the retry at once.
    throttled = live.key_names.index(stop["key"])
    assert live.cool_until[throttled] - time.monotonic() > 59
    assert live.cool_until[1 - throttled] <= time.monotonic()
    assert live.window.limit == 5
    [pause] = [row for row in rows if row.get("kind") == "rate_limited"]
    assert 60 < pause["pause_s"] <= 120 and pause["max_rpm_now"] == 5
    live.rate_limited(throttled, {"arm": "codex-a"})
    assert live.window.limit == 5, "a 429 inside a running pause does not halve again"
    live.note_throttle({"arm": "codex-a", "task": "other"}, 0)
    assert json.loads((tmp_path / "STOP").read_text())["task"] == "regex-log"
    assert "secret" not in (tmp_path / "requests.jsonl").read_text() + (tmp_path / "STOP").read_text()


def held_runner(tmp_path: Path, stop_file) -> runner.Runner:
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
        poll_interval_sec=1, run_label="cal", stop_file=str(stop_file) if stop_file else None)
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.stop_requested = __import__("threading").Event()
    live.lock = __import__("threading").Lock()
    live.log = lambda *args: None
    return live


def test_a_present_stop_file_holds_new_attempts_but_resumes_one_in_flight(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    stop.write_text("{}")
    live = held_runner(tmp_path, stop)
    started = []

    def run_attempt(task, arm, attempt, directory, state):
        started.append((arm, attempt, state.get("session_id")))
        return dict(state, phase="scored", reward=1.0)

    monkeypatch.setattr(live, "run_attempt", run_attempt)
    assert live.run_trial("regex-log", "codex") == {"phase": "held"}
    assert started == [] and not (live.trial_dir("regex-log", "codex") / "attempt-1" / "state.json").exists()
    attempt = live.trial_dir("regex-log", "codex") / "attempt-1"
    runner.write_json(attempt / "state.json", {"session_id": "s1", "exec_id": "e1", "phase": "running"})
    assert live.run_trial("regex-log", "codex")["phase"] == "scored"
    assert started == [("codex", 1, "s1")]
    stop.unlink()
    other = held_runner(tmp_path / "second", stop)
    monkeypatch.setattr(other, "run_attempt", run_attempt)
    assert other.run_trial("regex-log", "codex")["phase"] == "scored" and started[-1] == ("codex", 1, None)


def test_export_marks_429_trials_throttled_omits_them_on_request_and_reports_rpm(tmp_path):
    from evaluations.frontier_harness import export

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fhe-task-manifest.json").write_text(json.dumps({"tasks": {
        name: {"id": f"terminal-bench/{name}"} for name in ("regex-log", "sqlite-db-truncate")}}))
    native = tmp_path / "native"
    (native).mkdir()
    (native / "run.json").write_text(json.dumps({
        "run_label": "cal", "arms": ["codex"], "tasks": ["regex-log", "sqlite-db-truncate"],
        "tasks_dir": str(tasks_dir), "allow_hosts": [], "task_identity": {"dataset_commit": "x"}}))
    for task in ("regex-log", "sqlite-db-truncate"):
        attempt = native / "trials" / "codex" / task / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "state.json").write_text(json.dumps({"submitted_epoch": 1000.0, "phase": "scored"}))
        (attempt / "result.json").write_text(json.dumps({
            "outcome": "scored", "reward": 1.0, "finished_at": 1300.0,
            "phases": {"prepare": {"started": 1000.0}, "agent": {"started": 1010.0, "exit_code": 0}}}))
    usage = {"input_tokens": 100, "output_tokens": 10}
    rows = [
        {"kind": "responses", "arm": "codex-cal", "task": "regex-log", "status": 200, "ts": 1100.0, "latency_s": 2.0,
         "key": "0733", "sent_model": "kimi-k3", "attempts": 2, "usage": usage,
         "retries": [{"status": 429, "key": "0733"}], "rpm_wait_s": 1.5},
        {"kind": "responses", "arm": "codex-cal", "task": "regex-log", "status": 502, "ts": 1110.0, "latency_s": 1.0,
         "key": "0733", "sent_model": "kimi-k3", "attempts": 3, "retries": [{"status": 502}, {"status": 502}]},
        {"kind": "responses", "arm": "codex-cal", "task": "sqlite-db-truncate", "status": 200, "ts": 1130.0,
         "latency_s": 1.0, "key": "0733", "sent_model": "kimi-k3", "attempts": 1, "usage": usage},
    ]
    log = tmp_path / "relay.jsonl"
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    repo = tmp_path / "fhe"
    (repo / "skills/frontierharness-eval/scripts").mkdir(parents=True)
    (repo / "skills/frontierharness-eval/scripts/pricing.json").write_text(json.dumps({
        "version": "kimi-k3-2026-08-20", "unit_tokens": 1000000,
        "models": {"k3": {"fresh_input": 3.0, "cache_write": 3.0, "cache_read": 0.3, "output": 15.0}}}))
    summary = export.export(native_dir=native, relay_log=log, fhe_repo=repo, out_root=tmp_path / "all",
                            label="cal", run_id="cal", provider="p", run_normalizer=False)
    throttled = json.loads((tmp_path / "all/codex/trials/regex-log__a1/trial.json").read_text())
    assert throttled["throttled"] is True and throttled["upstream_429"] == 1
    assert throttled["relay_retries"] == 3 and throttled["failed_model_calls"] == 1
    assert throttled["rpm_wait_s"] == 1.5 and throttled["turns"] == 1
    clean = json.loads((tmp_path / "all/codex/trials/sqlite-db-truncate__a1/trial.json").read_text())
    assert clean["throttled"] is False and clean["upstream_429"] == 0
    assert summary["rpm"] == [{"key": "0733", "model": "kimi-k3", "active_minutes": 1, "requests": 6,
                               "max_rpm": 6, "p95_rpm": 6, "mean_rpm_active": 6.0}]
    export.export(native_dir=native, relay_log=log, fhe_repo=repo, out_root=tmp_path / "unthrottled",
                  label="cal", run_id="cal", provider="p", run_normalizer=False, exclude_throttled=True)
    written = sorted(path.parent.name for path in (tmp_path / "unthrottled/codex/trials").glob("*/trial.json"))
    assert written == ["sqlite-db-truncate__a1"]


# Ported from EVAL-MATRIX-1 eval/eval-matrix-20261112 8d2babda01 (tests/evaluations/test_tb_eval_matrix.py),
# with this file's relay_args helper: FRIDAY also delivers its per-minute 429 inside HTTP 200 streams.
FRIDAY_RPM_TEXT = "App:**6672在模型:gpt-6-sol每分钟请求次数超过限制"


def in_stream_relay(tmp_path, monkeypatch, *, face_upstream, stream_body: bytes, path: str, request: dict):
    """Relay one streamed request whose upstream answers HTTP 200 with ``stream_body``."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(_request):
        return web.Response(body=stream_body, status=200, content_type="text/event-stream")

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x"), ("4861", "secret-y")])
    args = relay_args(tmp_path, anthropic_model="m", responses_model="m", max_rpm=8)
    live = relay.Relay(args)

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/up", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, face_upstream, str(server.make_url("/up")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                reply = await client.post(path, json={**request, "stream": True})
                await reply.read()
                return reply.status

    status = asyncio.run(exercise())
    rows = [json.loads(line) for line in Path(args.log).read_text().splitlines()]
    return live, status, rows


@pytest.mark.parametrize("face_upstream, path, payload, stream_body", [
    ("RESPONSES_UPSTREAM", "/arm/codex-r1/task/regex-log/v1/responses", {"input": "hi"},
     b'data: {"type":"response.created","response":{"model":"m"}}\n\n'
     b'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"rate_limit_exceeded",'
     b'"message":"' + FRIDAY_RPM_TEXT.encode() + b'"}}}\n\n'),
    ("ANTHROPIC_UPSTREAM", "/arm/claude-code-r1/task/regex-log/anthropic/v1/messages",
     {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
     b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","message":"'
     + FRIDAY_RPM_TEXT.encode() + b'"}}\n\n'),
    ("UPSTREAM", "/arm/opencode-r1/task/regex-log/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]},
     b'data: {"status":429,"message":"' + FRIDAY_RPM_TEXT.encode()
     + b'","data":null,"ext":{"error":{"source":"AIGC","stage":"validation"}}}\n\ndata: [DONE]\n\n'),
])
def test_a_rate_limit_inside_a_200_stream_counts_as_an_upstream_429(tmp_path, monkeypatch, face_upstream, path,
                                                                    payload, stream_body):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    live, status, rows = in_stream_relay(tmp_path, monkeypatch, face_upstream=face_upstream,
                                         stream_body=stream_body, path=path, request=payload)
    assert status == 200, "the client still receives the stream as sent"
    [call] = [row for row in rows if row.get("kind") in ("responses", "messages", "chat")]
    assert call["stream_429"] is True and call["status"] == 200
    stop = json.loads((tmp_path / "STOP").read_text())
    assert stop["reason"] == "upstream_429" and stop["task"] == "regex-log" and stop["key"] == call["key"]
    [pause] = [row for row in rows if row.get("kind") == "rate_limited"]
    assert pause["key"] == call["key"] and pause["max_rpm_now"] == 4
    assert live.cool_until[live.key_names.index(call["key"])] - time.monotonic() > 59
    assert "secret" not in Path(live.args.log).read_text()


def test_model_text_about_rate_limits_is_not_a_rate_limit(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    content = json.dumps({"choices": [{"delta": {"content": 'raise Error("rate_limit_exceeded: ' + FRIDAY_RPM_TEXT + '")'}}]},
                         ensure_ascii=False)
    live, status, rows = in_stream_relay(
        tmp_path, monkeypatch, face_upstream="UPSTREAM", path="/arm/opencode-r1/task/regex-log/v1/chat/completions",
        request={"messages": [{"role": "user", "content": "hi"}]},
        stream_body=b"data: " + content.encode() + b"\n\ndata: [DONE]\n\n")
    [call] = [row for row in rows if row.get("kind") == "chat"]
    assert status == 200 and not call.get("stream_429")
    assert not (tmp_path / "STOP").exists() and live.window.limit == 8


def test_export_counts_in_stream_rate_limits_as_throttled():
    from evaluations.frontier_harness import export

    facts = export.throttle_facts([{"kind": "responses", "status": 200, "stream_429": True},
                                   {"kind": "responses", "status": 200}])
    assert facts["upstream_429"] == 1 and facts["throttled"] is True and facts["failed_model_calls"] == 0


def test_export_attributes_a_record_without_a_task_by_arm_and_window():
    from evaluations.frontier_harness import export

    window = (1000.0, 2000.0)
    records = [
        {"kind": "chat", "arm": "goose-a", "task": "", "status": 200, "ts": 1100.0},
        {"kind": "chat", "arm": "goose-a", "status": 200, "ts": 1200.0},
        {"kind": "chat", "arm": "goose-a", "task": "regex-log", "status": 200, "ts": 1300.0},
        {"kind": "chat", "arm": "goose-a", "task": "other-task", "status": 200, "ts": 1400.0},
        {"kind": "chat", "arm": "codex-a", "task": "regex-log", "status": 200, "ts": 1500.0},
        {"kind": "chat", "arm": "goose-a", "task": "regex-log", "status": 200, "ts": 2500.0},
    ]
    picked = export.attempt_records(records, "goose-a", "regex-log", window)
    assert [record["ts"] for record in picked] == [1100.0, 1200.0, 1300.0]


class _GatewayHTTPError(Exception):
    """pssdk raises its errors ``from`` the requests HTTPError; only the response status matters here."""


def _gateway_error(status: int, text: str) -> Exception:
    cause = _GatewayHTTPError(f"{status} Client Error")
    cause.response = SimpleNamespace(status_code=status, json=lambda: {"error": text})
    error = RuntimeError(f"SandboxUndefinedError: {text}")
    error.__cause__ = cause
    return error


class _Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self):
        return 1_794_470_000.0 + self.now

    def strftime(self, *args):
        return time.strftime(*args)


class _Session:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def exec(self, argv, files=None, env=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def submit_with(tmp_path, monkeypatch, outcomes):
    live = held_runner(tmp_path, None)
    live.vm_trial = b"# vm_trial"
    live.save = lambda directory, state: None
    clock = _Clock()
    monkeypatch.setattr(runner, "time", clock)
    session = _Session(outcomes)
    task = live.tasks["regex-log"]
    spec = {"trial_key": "regex-log-codex-a1", "task_name": "regex-log", "task_bundle": "/var/lib/tb/x.tar.gz"}
    state = live.submit(session, spec, tmp_path, {"phase": "started"}, "regex-log/codex#1")
    return state, session, clock


def test_submit_waits_for_a_vm_ingress_that_is_not_ready_yet(tmp_path, monkeypatch):
    port_closed = _gateway_error(404, "下游服务端口已关闭: connection timed out after 2000 ms: /10.1.2.3:11976")
    route_missing = _gateway_error(404, "<title>404 Not Found</title>")
    state, session, clock = submit_with(tmp_path, monkeypatch, [port_closed, route_missing, {"exec_id": "e1"}])
    assert state["phase"] == "running" and state["exec_id"] == "e1"
    assert session.calls == 3 and state["ingress_not_ready"] == 2 and clock.sleeps == [1, 1]


def test_submit_does_not_repeat_an_exec_that_may_have_run(tmp_path, monkeypatch):
    for error in (RuntimeError("SandboxClientTimeoutError: read timed out"), _gateway_error(500, "backend error")):
        state, session, clock = submit_with(tmp_path / str(id(error)), monkeypatch, [error, {"exec_id": "e1"}])
        assert state["phase"] == "submit_error" and session.calls == 1 and clock.sleeps == []


def test_submit_gives_up_when_the_ingress_stays_unready_for_the_start_budget(tmp_path, monkeypatch):
    from evaluations.terminal_bench.gateway import START_TIMEOUT_SEC

    errors = [_gateway_error(404, "下游服务端口已关闭") for _ in range(START_TIMEOUT_SEC + 5)]
    state, session, clock = submit_with(tmp_path, monkeypatch, errors)
    assert state["phase"] == "submit_error" and "下游服务端口已关闭" in state["error"]
    assert sum(clock.sleeps) < START_TIMEOUT_SEC and session.calls == len(clock.sleeps) + 1


def fake_time():
    state = {"now": 1000.0}

    async def sleep(seconds):
        state["now"] += seconds

    return state, (lambda: state["now"]), sleep


def test_the_token_window_holds_a_start_back_until_the_trailing_minute_has_room_under_cap_tpm():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay
    state, clock, sleep = fake_time()
    window = relay.TokenWindow(530_000, clock=clock, sleep=sleep)

    async def starts():
        assert await window.acquire(300_000) == 0.0
        state["now"] += 20
        assert await window.acquire(200_000) == 0.0
        state["now"] += 10
        return await window.acquire(100_000)  # 600k would pass 530k: held until the first start leaves

    assert asyncio.run(starts()) == 30.0
    assert state["now"] == 1060.0 and window.in_window() == 300_000


def test_an_estimate_above_the_whole_token_budget_starts_alone_in_an_empty_window():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay
    state, clock, sleep = fake_time()
    window = relay.TokenWindow(100, clock=clock, sleep=sleep)

    async def starts():
        return await window.acquire(500), await window.acquire(1)

    assert asyncio.run(starts()) == (0.0, 60.0)


def test_token_estimates_err_high_per_face():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay
    # Median bytes per input token in the relay logs: Tofu on Messages 3.88, Codex on Responses 4.29.
    assert relay.estimated_input_tokens("messages", 3880) >= 1000 * 1.2
    assert relay.estimated_input_tokens("responses", 4290) >= 1000 * 1.15
    assert relay.estimated_input_tokens("count_tokens", 31) == relay.estimated_input_tokens("messages", 31)
    assert relay.estimated_input_tokens("unknown", 310) >= relay.estimated_input_tokens("responses", 310)


def test_a_tpm_429_halves_the_token_budget_and_any_other_429_the_request_budget(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay
    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x")])
    budget = tmp_path / "budget.json"
    live = relay.Relay(relay_args(tmp_path, key_suffixes="6672", max_rpm=8, max_tpm=530_000,
                                  budget_file=str(budget)))
    assert {k: v for k, v in json.loads(budget.read_text()).items() if k.startswith("max")} == \
        {"max_rpm": 8, "max_tpm": 530_000}
    live.rate_limited(0, {"arm": "codex-k2"}, "App:**6672在模型:kimi-k3的tpm超过限制")
    assert (live.window.limit, live.tokens.limit) == (8, 265_000)
    live.rate_limited(0, {"arm": "codex-k2"}, "App:**6672在模型:kimi-k3的tpm超过限制")
    assert live.tokens.limit == 265_000, "a 429 inside a running pause does not halve again"
    live.cool_until[0] = 0.0
    live.rate_limited(0, {"arm": "codex-k2"}, "App:**6672在模型:kimi-k3每分钟请求次数超过限制")
    assert (live.window.limit, live.tokens.limit) == (4, 265_000)
    live.cool_until[0] = 0.0
    live.rate_limited(0, {"arm": "codex-k2"}, "engine overloaded")
    assert (live.window.limit, live.tokens.limit) == (2, 265_000)
    stored = json.loads(budget.read_text())
    assert (stored["max_rpm"], stored["max_tpm"]) == (2, 265_000)
    rows = [json.loads(line) for line in Path(live.args.log).read_text().splitlines()]
    assert [row["halved"] for row in rows if row.get("kind") == "rate_limited"] == ["tpm", "rpm", "rpm"]


def test_without_a_token_budget_a_tpm_429_halves_the_request_budget(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay
    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x")])
    live = relay.Relay(relay_args(tmp_path, key_suffixes="6672", max_rpm=8))
    live.rate_limited(0, {"arm": "a"}, "App:**6672在模型:kimi-k3的tpm超过限制")
    assert (live.window.limit, live.tokens.limit) == (4, None)


def test_the_budget_file_is_authoritative_for_a_running_and_a_successor_relay(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    import os
    from evaluations.terminal_bench import relay
    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x")])
    budget = tmp_path / "budget.json"
    live = relay.Relay(relay_args(tmp_path, key_suffixes="6672", max_rpm=8, max_tpm=530_000,
                                  budget_file=str(budget)))
    budget.write_text(json.dumps({"max_rpm": 8, "max_tpm": 265_000, "written_by": "guard"}))
    later = time.time() + 5
    os.utime(budget, (later, later))
    live.refresh_budgets()
    assert (live.window.limit, live.tokens.limit) == (8, 265_000)
    successor = relay.Relay(relay_args(tmp_path, key_suffixes="6672", max_rpm=16, max_tpm=530_000,
                                       budget_file=str(budget), assume_full_window=True))
    assert (successor.window.limit, successor.tokens.limit) == (8, 265_000)
    assert successor.window.in_window() == 8 and successor.tokens.in_window() == 265_000


def test_dispatch_paces_requests_by_their_estimated_input_tokens(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(_request):
        return web.json_response({"id": "r", "status": "completed", "model": "kimi-k3",
                                  "usage": {"input_tokens": 500, "output_tokens": 2}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x")])
    live = relay.Relay(relay_args(tmp_path, key_suffixes="6672", max_tpm=1000))
    state, clock, sleep = fake_time()
    live.tokens = relay.TokenWindow(1000, clock=clock, sleep=sleep)
    body = {"model": "kimi-k3", "input": "x" * 2000, "stream": False}

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/responses", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/v1/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                for _ in range(2):
                    reply = await client.post("/arm/codex-k2/task/regex-log/v1/responses", json=body)
                    assert reply.status == 200

    asyncio.run(exercise())
    calls = [json.loads(line) for line in Path(live.args.log).read_text().splitlines()
             if json.loads(line).get("kind") == "responses"]
    expected = relay.estimated_input_tokens("responses", len(json.dumps(body)))
    assert [call["tpm_estimate"] for call in calls] == [expected, expected] and 2 * expected > 1000
    assert "tpm_wait_s" not in calls[0] and calls[1]["tpm_wait_s"] == 60.0


def test_a_key_retirement_writes_the_stop_file_and_later_requests_never_reach_upstream(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    calls = []

    async def upstream(_request):
        calls.append(1)
        return web.json_response({"status": 401, "message": "当前 App ID 已被禁用"}, status=401)

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("0733", "secret-a")])
    live = relay.Relay(relay_args(tmp_path, key_suffixes="0733"))
    body = {"model": "kimi-k3", "input": "hi", "stream": False}

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/responses", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/v1/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                first = await client.post("/arm/tofu-k1/task/httpx/v1/responses", json=body)
                second = await client.post("/arm/tofu-k1/task/katex/v1/responses", json=body)
                return first.status, second.status

    first, second = asyncio.run(exercise())
    # Typed and non-retryable (relay_ingress): a 502 here made Tofu's dispatch retry about 90 times a minute.
    assert first == 403 and second == 403 and len(calls) == 1
    stop = json.loads((tmp_path / "STOP").read_text())
    assert stop["reason"] == "key_retired_401" and stop["key"] == "0733" and stop["task"] == "httpx"
    rows = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert [row["kind"] for row in rows if row["kind"] in ("key_retired", "stop_file_written")] == \
        ["key_retired", "stop_file_written"]
    assert "secret" not in (tmp_path / "requests.jsonl").read_text() + (tmp_path / "STOP").read_text()
