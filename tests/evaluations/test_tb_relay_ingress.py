"""Relay ingress: the run's relay key on every route, and a typed non-retryable answer for a disabled key.

Executable form of the 2026-11-13 exposure fixes (relay_ingress.py): every relay bound 0.0.0.0 with no
inbound auth, so any host reaching it could spend the keys it holds; and after FRIDAY disabled AppId
…0733 at 16:50 the relays answered 502 "no live key", which Tofu's dispatch retries without end.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import io
import json
import os
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit

RUN_KEY = "k" * 20 + "-run-key-for-tests_" + "z" * 4
DISABLED_BODY = {"status": 401, "message": "当前 App ID 已被禁用，请前往Friday控制台: https://friday.internal.example.com/budget/"
                                            "serviceManage 查看详情或联系管理员。", "data": None}
# Every route the relay serves, plus FRONTIER-HARNESS-2's held route (Amendment 7) and an unknown path.
ROUTES = [
    ("POST", "/arm/tofu-a/task/t1/v1/chat/completions"), ("POST", "/v1/chat/completions"),
    ("POST", "/arm/tofu-a/task/t1/anthropic/v1/messages"),
    ("POST", "/arm/tofu-a/task/t1/anthropic/v1/messages/count_tokens"),
    ("POST", "/arm/codex-a/task/t1/v1/responses"), ("GET", "/arm/tofu-a/v1/models"),
    ("GET", "/probe"), ("GET", "/arm/tofu-a/probe"), ("GET", "/stats"), ("GET", "/files/tool.tar.gz"),
    ("GET", "/arm/tofu-a/task/t1/held"), ("HEAD", "/"),
]
BODY = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}], "input": "hi", "max_tokens": 8}


def write_key(path: Path, key: str = RUN_KEY, mode: int = 0o600) -> Path:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(key + "\n")
    os.chmod(path, mode)
    return path


def relay_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    files = tmp_path / "files"
    files.mkdir(exist_ok=True)
    (files / "tool.tar.gz").write_bytes(b"tool")
    values = dict(origin="unused", provider="unused", key_suffixes="0733", files_dir=str(files),
                  log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model="kimi-k3",
                  temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model="kimi-k3",
                  responses_model="kimi-k3", responses_max_output_tokens=None, max_rpm=None, max_tpm=None,
                  budget_file=None, first_body_dir=None, anthropic_body_dir=None, preserve_pi_effort_markers=False,
                  stop_file=str(tmp_path / "STOP"), ingress_key_file=None)
    values.update(overrides)
    return SimpleNamespace(**values)


async def with_relay(monkeypatch, live, upstream_status, upstream_body, exercise):
    """Run ``exercise(client)`` against ``live`` with every upstream face answered by one fake."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    calls = []

    async def upstream(request):
        calls.append((request.path, request.headers.get("Authorization")))
        # Raw UTF-8 JSON, as FRIDAY sends it (its 401 text reaches the relay logs unescaped).
        return web.Response(text=json.dumps(upstream_body, ensure_ascii=False), status=upstream_status,
                            content_type="application/json")

    upstream_app = web.Application()
    for path in ("/chat", "/messages", "/messages/count_tokens", "/responses"):
        upstream_app.router.add_post(path, upstream)
    async with TestServer(upstream_app) as server:
        monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
        monkeypatch.setattr(relay, "ANTHROPIC_UPSTREAM", str(server.make_url("/messages")))
        monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/responses")))
        async with TestClient(TestServer(relay.build_app(live))) as client:
            return await exercise(client), calls


def keyed_relay(tmp_path, monkeypatch, keys=(("0733", "upstream-secret-a"),)):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    monkeypatch.setattr(relay, "load_keys", lambda *_: list(keys))
    key_file = write_key(tmp_path / "relay.key")
    return relay.Relay(relay_args(tmp_path, key_suffixes=",".join(name for name, _ in keys),
                                  ingress_key_file=str(key_file)))


def log_text(tmp_path: Path) -> str:
    path = tmp_path / "requests.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer relay-held"}, {"x-api-key": "relay-held"},
    {"Authorization": "Bearer " + RUN_KEY[:-1]}, {"Authorization": "Basic " + RUN_KEY},
], ids=["none", "placeholder-bearer", "placeholder-x-api-key", "wrong-key", "not-bearer"])
def test_a_keyed_relay_refuses_every_route_without_the_run_key(tmp_path, monkeypatch, headers):
    live = keyed_relay(tmp_path, monkeypatch)

    async def exercise(client):
        statuses = {}
        for method, path in ROUTES:
            reply = await client.request(method, path, json=BODY if method == "POST" else None, headers=headers)
            statuses[(method, path)] = reply.status
            if method != "HEAD":
                body = await reply.json()
                assert body["error"]["type"] == "relay_unauthorized"
                assert reply.headers["x-should-retry"] == "false"
        return statuses

    statuses, calls = asyncio.run(with_relay(monkeypatch, live, 200, {"id": "x"}, exercise))
    assert statuses == {route: 401 for route in ROUTES}
    assert calls == [], "a refused request never reaches upstream"
    refusals = [json.loads(line) for line in log_text(tmp_path).splitlines()]
    assert [row["kind"] for row in refusals] == ["ingress_refused"] * len(ROUTES)
    assert {row["path"] for row in refusals} == {path for _, path in ROUTES}
    assert RUN_KEY not in log_text(tmp_path) and RUN_KEY[:-1] not in log_text(tmp_path)


def test_the_run_key_opens_every_face_as_bearer_or_x_api_key(tmp_path, monkeypatch):
    live = keyed_relay(tmp_path, monkeypatch)
    bearer, api_key = {"Authorization": "Bearer " + RUN_KEY}, {"x-api-key": RUN_KEY}

    async def exercise(client):
        statuses = {}
        for headers in (bearer, api_key):
            for method, path in ROUTES[:-1]:
                reply = await client.request(method, path, json=BODY if method == "POST" else None, headers=headers)
                statuses[(method, path, tuple(headers))] = reply.status
        stats = await (await client.get("/stats", headers=bearer)).json()
        return statuses, stats

    (statuses, stats), calls = asyncio.run(with_relay(
        monkeypatch, live, 200, {"id": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}, exercise))
    assert set(statuses.values()) == {200}, statuses
    assert len(calls) == 2 * 5, "each of the five model routes reached upstream once per credential form"
    assert {authorization for _, authorization in calls} == {"Bearer upstream-secret-a"}
    assert stats["keys"] == ["0733"] and stats["disabled"] == [] and RUN_KEY not in json.dumps(stats)
    assert RUN_KEY not in log_text(tmp_path)


def test_a_relay_beyond_loopback_needs_its_run_key_file(tmp_path):
    from evaluations.terminal_bench import relay_ingress

    with pytest.raises(SystemExit, match="--ingress-key-file"):
        relay_ingress.require_ingress_key("0.0.0.0", None)
    relay_ingress.require_ingress_key("127.0.0.1", None)
    relay_ingress.require_ingress_key("0.0.0.0", str(tmp_path / "relay.key"))
    with pytest.raises(SystemExit, match="owner only"):
        relay_ingress.read_key_file(write_key(tmp_path / "shared.key", mode=0o644))
    with pytest.raises(SystemExit, match="not a relay key"):
        relay_ingress.read_key_file(write_key(tmp_path / "placeholder.key", key="relay-held"))
    with pytest.raises(SystemExit, match="no relay key file"):
        relay_ingress.read_key_file(tmp_path / "absent.key")


def test_the_runner_creates_the_run_key_once_at_mode_0600(tmp_path, capsys):
    from evaluations.terminal_bench import relay_ingress
    from evaluations.terminal_bench.__main__ import main

    path = tmp_path / "keys" / "relay.key"
    assert main(["relay-key", str(path)]) == 0
    first = relay_ingress.read_key_file(path)
    assert path.stat().st_mode & 0o777 == 0o600 and len(first) >= 40
    assert main(["relay-key", str(path)]) == 0 and relay_ingress.read_key_file(path) == first
    assert first not in capsys.readouterr().out


@pytest.mark.parametrize("face, path", [
    ("openai", "/arm/tofu-a/task/t1/v1/chat/completions"), ("openai", "/arm/codex-a/task/t1/v1/responses"),
    ("anthropic", "/arm/tofu-a/task/t1/anthropic/v1/messages"),
])
def test_a_disabled_appid_retires_its_key_and_every_call_gets_a_typed_non_retryable_403(
        tmp_path, monkeypatch, face, path):
    live = keyed_relay(tmp_path, monkeypatch)
    bearer = {"Authorization": "Bearer " + RUN_KEY}

    async def exercise(client):
        replies = []
        for _ in range(3):
            reply = await client.post(path, json=BODY, headers=bearer)
            replies.append((reply.status, reply.headers.get("x-should-retry"), await reply.json()))
        stats = await (await client.get("/stats", headers=bearer)).json()
        return replies, stats

    (replies, stats), calls = asyncio.run(with_relay(monkeypatch, live, 401, DISABLED_BODY, exercise))
    assert len(calls) == 1, "the disabled key is asked once; later calls never reach upstream"
    for status, retry, body in replies:
        assert (status, retry) == (403, "false")
        error = body["error"]
        assert error["type"] == "relay_key_disabled" and "0733" in error["message"]
        assert (body.get("type") == "error") == (face == "anthropic")
    assert stats["dead"] == ["0733"] and stats["disabled"] == ["0733"]
    rows = [json.loads(line) for line in log_text(tmp_path).splitlines()]
    [retired] = [row for row in rows if row["kind"] == "key_retired"]
    assert retired["status"] == 401 and retired["disabled"] is True and retired["key"] == "0733"
    assert [row["status"] for row in rows if row["kind"] in ("chat", "messages", "responses")] == [403, 403, 403]
    assert json.loads((tmp_path / "STOP").read_text())["reason"] == "key_retired_401"
    assert "upstream-secret-a" not in log_text(tmp_path) and RUN_KEY not in log_text(tmp_path)


def test_a_disabled_key_is_skipped_while_another_key_still_serves(tmp_path, monkeypatch):
    live = keyed_relay(tmp_path, monkeypatch, keys=(("0733", "upstream-secret-a"), ("6672", "upstream-secret-b")))
    bearer = {"Authorization": "Bearer " + RUN_KEY}
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(request):
        if request.headers["Authorization"].endswith("secret-a"):
            return web.json_response(DISABLED_BODY, status=401)  # \u-escaped JSON
        return web.json_response({"id": "ok", "usage": {"prompt_tokens": 1}})

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                statuses = [(await client.post("/arm/tofu-a/task/t1/v1/chat/completions", json=BODY,
                                               headers=bearer)).status for _ in range(4)]
                return statuses, await (await client.get("/stats", headers=bearer)).json()

    statuses, stats = asyncio.run(exercise())
    assert statuses == [200, 200, 200, 200] and stats["disabled"] == ["0733"] and stats["dead"] == ["0733"]


def test_a_retirement_without_the_disable_signature_is_typed_as_retired(tmp_path, monkeypatch):
    live = keyed_relay(tmp_path, monkeypatch)

    async def exercise(client):
        reply = await client.post("/arm/tofu-a/task/t1/v1/chat/completions", json=BODY,
                                  headers={"Authorization": "Bearer " + RUN_KEY})
        return reply.status, (await reply.json())["error"]["type"]

    result, _ = asyncio.run(with_relay(monkeypatch, live, 402, {"error": {"message": "payment required"}}, exercise))
    assert result == (403, "relay_key_retired")


def spec_runner(tmp_path: Path, relay_key_file) -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\ncpus = 1\nmemory_mb = 2048\n')
    (task_dir / "instruction.md").write_text("x\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"], arms=["codex"],
        protocol="fhe-v1", model="kimi-k3", relay_url="http://10.9.8.7:18732", files_url="http://10.9.8.7:18732/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://p:1", max_alive_min=150, max_attempts=2, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="cal", relay_key_file=str(relay_key_file) if relay_key_file else None)
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.stop_requested = threading.Event()
    live.log = lambda *args: None
    live.vm_trial = b"# vm_trial"
    live.save = lambda directory, state: None
    return live


class _Session:
    def __init__(self):
        self.files = None

    def exec(self, argv, files=None, env=None):
        self.files = files
        return {"exec_id": "e1"}


def test_the_runner_uploads_the_run_key_where_agents_hold_the_placeholder(tmp_path, monkeypatch):
    live = spec_runner(tmp_path, write_key(tmp_path / "relay.key"))
    monkeypatch.setattr(harness, "bundle_task", lambda task: b"bundle")
    spec = {"trial_key": "regex-log-codex-a1", "task_name": "regex-log", "task_bundle": "/var/lib/tb/x.tar.gz",
            "agent": {"env": {"TOFU_RELAY_KEY": "relay-held",
                              "TOFU_AGENT_MODEL_ROUTING": json.dumps({"credential_secrets": {"s": "relay-held"}})},
                      "log_files": {"opencode.json": json.dumps(harness.opencode_config(
                          model_base_url="http://10.9.8.7:18732/arm/o/v1", model="kimi-k3", context_tokens=1,
                          output_tokens=1))},
                      "program": "qwen --openai-api-key relay-held",
                      "artifacts": [{"url": "http://10.9.8.7:18732/files/a.tar.gz"}]}}
    persisted = json.dumps(spec)
    session = _Session()
    state = live.submit(session, spec, tmp_path, {"phase": "started"}, "regex-log/codex#1")
    assert state["phase"] == "running"
    uploaded = next(content for name, content in session.files.items() if name.endswith("/spec.json"))
    sent = json.loads(uploaded)
    assert "relay-held" not in uploaded.decode()
    assert sent["agent"]["env"]["TOFU_RELAY_KEY"] == RUN_KEY and sent["agent"]["program"].endswith(RUN_KEY)
    assert json.loads(sent["agent"]["env"]["TOFU_AGENT_MODEL_ROUTING"])["credential_secrets"]["s"] == RUN_KEY
    assert json.loads(sent["agent"]["log_files"]["opencode.json"])["provider"]["benchmark"]["options"]["apiKey"] \
        == RUN_KEY
    assert sent["relay_ingress"] == {"origin": "http://10.9.8.7:18732", "key": RUN_KEY}
    assert json.dumps(spec) == persisted, "the spec the runner persists keeps the placeholder"


def test_without_a_run_key_the_spec_is_uploaded_unchanged(tmp_path, monkeypatch):
    live = spec_runner(tmp_path, None)
    monkeypatch.setattr(harness, "bundle_task", lambda task: b"bundle")
    spec = {"trial_key": "k", "task_name": "regex-log", "task_bundle": "/b", "agent": {"env": {"K": "relay-held"}}}
    session = _Session()
    live.submit(session, spec, tmp_path, {"phase": "started"}, "regex-log/codex#1")
    uploaded = next(content for name, content in session.files.items() if name.endswith("/spec.json"))
    assert json.loads(uploaded) == spec


class _Recorder(http.server.BaseHTTPRequestHandler):
    payload = b""
    seen: list = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


def test_artifact_downloads_carry_the_run_key_to_the_relay_only(tmp_path):
    from evaluations.terminal_bench import vm_trial

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("bin/tool")
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"tool"))
    _Recorder.payload, _Recorder.seen = buffer.getvalue(), []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        digest = hashlib.sha256(_Recorder.payload).hexdigest()
        trial = vm_trial.Trial.__new__(vm_trial.Trial)
        trial.spec = {"cache_dir": str(tmp_path / "cache"), "relay_ingress": {"origin": origin, "key": RUN_KEY}}
        root = trial.fetch_artifact({"url": f"{origin}/files/tool.tar.gz", "sha256": digest})
        assert (root / "bin" / "tool").read_bytes() == b"tool"
        trial.spec = {"cache_dir": str(tmp_path / "other-cache"),
                      "relay_ingress": {"origin": "http://10.9.8.7:18732", "key": RUN_KEY}}
        trial.fetch_artifact({"url": f"{origin}/files/tool.tar.gz", "sha256": digest})
    finally:
        server.shutdown()
    assert _Recorder.seen == [("/files/tool.tar.gz", "Bearer " + RUN_KEY), ("/files/tool.tar.gz", None)]


def test_the_anthropic_bridge_passes_the_clients_run_key_to_a_keyed_relay(tmp_path, monkeypatch):
    live = keyed_relay(tmp_path, monkeypatch)
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import anthropic_bridge, relay

    async def upstream(request):
        return web.json_response({"id": "c1", "model": "kimi-k3", "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                                  "choices": [{"index": 0, "finish_reason": "stop",
                                               "message": {"role": "assistant", "content": "ok"}}]})

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            async with TestServer(relay.build_app(live)) as relay_server:
                bridge = anthropic_bridge.Bridge(SimpleNamespace(
                    relay_url=str(relay_server.make_url("")), log=str(tmp_path / "bridge.jsonl"), read_timeout=30,
                    model="kimi-k3"))
                async with TestClient(TestServer(anthropic_bridge.build_app(bridge))) as client:
                    message = {"model": "kimi-k3", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
                    keyed = await client.post("/arm/claude-code-a/v1/messages", json=message,
                                              headers={"Authorization": "Bearer " + RUN_KEY})
                    bare = await client.post("/arm/claude-code-a/v1/messages", json=message)
                    return keyed.status, bare.status

    assert asyncio.run(exercise()) == (200, 401)
    assert RUN_KEY not in (tmp_path / "bridge.jsonl").read_text() + log_text(tmp_path)


class _Held(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        body = json.dumps({"held_s": 2.5, "holding": False}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_held_reads_carry_the_run_key_in_the_vm_and_on_the_host(tmp_path):
    from evaluations.terminal_bench import vm_trial

    _Held.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Held)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        url = f"{origin}/arm/tofu-cal/task/regex-log/held"
        spec = {"relay_ingress": {"origin": origin, "key": RUN_KEY}}
        hold = vm_trial.RelayHold(url, vm_trial.relay_headers(spec, url))
        assert hold.base == 2.5 and hold() == (0.0, False)
        live = spec_runner(tmp_path, write_key(tmp_path / "relay.key"))
        assert live.relay_held({"agent": {"relay_held_url": url}}) == 2.5
        assert vm_trial.relay_headers(spec, "http://10.9.8.7:1/held") == {}
    finally:
        server.shutdown()
    assert _Held.seen == [("/arm/tofu-cal/task/regex-log/held", "Bearer " + RUN_KEY)] * 3
