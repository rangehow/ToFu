"""EVAL-MATRIX-1: per-model relay pins and per-arm faces for the model x harness matrix."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations import pi_contract
from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit


def test_pins_set_and_omit_dotted_paths_and_report_what_they_replaced():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    body = {"temperature": 0.2, "thinking": {"type": "disabled", "display": "summarized"}, "max_tokens": 10}
    replaced = relay.apply_pins(body, {"omit": ["temperature", "top_p", "output_config.effort"],
                                       "set": {"thinking.type": "enabled", "output_config.effort": "medium",
                                               "max_tokens": 32768}})
    assert body == {"thinking": {"type": "enabled", "display": "summarized"},
                    "output_config": {"effort": "medium"}, "max_tokens": 32768}
    assert replaced == {"temperature": 0.2, "thinking.type": "disabled", "output_config.effort": None,
                        "max_tokens": 10}
    assert relay.apply_pins(body, None) == {}


def test_a_pin_file_names_only_known_faces_and_rules(tmp_path):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"chat": {"set": {"reasoning_effort": "high"}}, "responses": {"omit": ["temperature"]}}))
    assert relay.load_pins(good)["chat"]["set"] == {"reasoning_effort": "high"}
    assert relay.load_pins(None) == {}
    for bad in ({"gemini": {"set": {}}}, {"chat": {"replace": {}}}):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(SystemExit):
            relay.load_pins(path)


def relay_args(tmp_path: Path, pins: dict) -> SimpleNamespace:
    pin_file = tmp_path / "pins.json"
    pin_file.write_text(json.dumps(pins))
    return SimpleNamespace(origin="unused", provider="unused", key_suffixes="4861", files_dir=None,
                           log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30,
                           model="deepseek-v4-flash", temperature=None, top_p=None, max_tokens=None,
                           reasoning_effort=None, anthropic_model=None, responses_model=None,
                           responses_max_output_tokens=None, max_rpm=7, first_body_dir=None,
                           anthropic_body_dir=None, preserve_pi_effort_markers=False, stop_file=None,
                           pin_file=str(pin_file))


def test_every_arm_reaches_the_deployment_with_the_pinned_reasoning_and_its_task_recorded(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    received = []

    async def upstream(request):
        received.append(await request.json())
        return web.json_response({"id": "c", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    pins = {"chat": {"set": {"thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": 32768},
                     "omit": ["max_completion_tokens", "temperature"]}}
    monkeypatch.setattr(relay, "load_keys", lambda *_: [("4861", "secret-x")])
    live = relay.Relay(relay_args(tmp_path, pins))

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                for arm, request in (("opencode-r1", {"model": "deepseek-v4-flash", "temperature": 0.7,
                                                      "max_completion_tokens": 4096}),
                                     ("pi-r1", {"model": "deepseek-flash", "reasoning_effort": "max",
                                                "thinking": {"type": "disabled"}})):
                    reply = await client.post(f"/arm/{arm}/task/regex-log/v1/chat/completions",
                                              json={**request, "messages": [{"role": "user", "content": "hi"}]})
                    assert reply.status == 200

    asyncio.run(exercise())
    for body in received:
        assert body["model"] == "deepseek-v4-flash"
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
        assert body["max_tokens"] == 32768 and "temperature" not in body and "max_completion_tokens" not in body
    rows = [json.loads(line) for line in Path(live.args.log).read_text().splitlines()]
    calls = [row for row in rows if row.get("kind") == "chat"]
    assert [(row["arm"], row["task"]) for row in calls] == [("opencode-r1", "regex-log"), ("pi-r1", "regex-log")]
    assert calls[0]["pinned"]["temperature"] == 0.7 and calls[1]["pinned"]["reasoning_effort"] == "max"
    assert calls[1]["sent"]["thinking"] == {"type": "enabled"}
    assert "secret" not in Path(live.args.log).read_text()


def test_the_chat_face_records_each_trials_injected_context_and_first_body(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(request):
        return web.json_response({"id": "c", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("4861", "secret-x")])
    args = relay_args(tmp_path, {})
    args.first_body_dir = str(tmp_path / "first")
    live = relay.Relay(args)
    system = ("<!-- tofu-context:platform_static:start -->You are a coding agent."
              "<!-- tofu-context:platform_static:end --> Current date: 2026-11-12")

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                for turn in ("fix it", "and test it"):
                    reply = await client.post("/arm/qwen-code-r1/task/regex-log/v1/chat/completions", json={
                        "model": "x", "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": turn}],
                        "tools": [{"type": "function", "function": {"name": "run_shell"}}]})
                    assert reply.status == 200

    asyncio.run(exercise())
    rows = [json.loads(line) for line in Path(args.log).read_text().splitlines() if '"chat"' in line]
    assert all(row["context"]["tofu_blocks"] == ["platform_static"] for row in rows)
    assert all(row["context"]["date_markers"] == ["Current date:"] for row in rows)
    [archived] = list((tmp_path / "first").iterdir())
    assert archived.name == "qwen-code-r1__regex-log__chat.json" and rows[0]["first_body"] == archived.name
    first = json.loads(archived.read_text())
    assert first["body"]["messages"][1]["content"] == "fix it"
    assert first["body"]["tools"][0]["function"]["name"] == "run_shell"
    assert "secret" not in archived.read_text() and "first_body" not in rows[1]


FRIDAY_RPM_TEXT = "App:**6672在模型:gpt-6-sol每分钟请求次数超过限制"


def in_stream_relay(tmp_path, monkeypatch, *, face_upstream, stream_body: bytes, path: str, request: dict):
    """Relay one streamed request whose upstream answers HTTP 200 with ``stream_body``."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(_request):
        return web.Response(body=stream_body, status=200, content_type="text/event-stream")

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-x"), ("4861", "secret-y")])
    args = relay_args(tmp_path, {})
    args.anthropic_model, args.responses_model, args.max_rpm = "m", "m", 8
    args.stop_file = str(tmp_path / "STOP")
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

IN_STREAM_RATE_LIMITS = pytest.mark.parametrize("face_upstream, path, payload, stream_body", [
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


@IN_STREAM_RATE_LIMITS
def test_a_rate_limit_inside_a_200_stream_counts_as_an_upstream_429(tmp_path, monkeypatch, face_upstream, path,
                                                                    payload, stream_body):
    """FRIDAY also delivers its per-minute 429 inside HTTP 200 SSE streams (Codex saw it as
    'stream disconnected before completion' and failed its turn, unseen by the relay)."""
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
    assert live.cool_until[live.key_names.index(call["key"])] - __import__("time").monotonic() > 59
    assert "secret" not in Path(live.args.log).read_text()


@IN_STREAM_RATE_LIMITS
def test_a_client_that_left_early_still_has_its_call_and_a_late_rate_limit_recorded(
        tmp_path, monkeypatch, face_upstream, path, payload, stream_body):
    """Pi closes its connection once it has the answer; the usage chunk or an in-stream
    429 can arrive after that. The relay still reads it, logs the call with its usage
    and honours the 429; losing it undercounts calls and cost and skips stop-on-429.
    """
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    import socket

    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from evaluations.terminal_bench import relay

    usage = {"prompt_tokens": 5, "completion_tokens": 2}
    late = (b'data: {"choices":[],"usage":' + json.dumps(usage).encode() + b'}\n\n'
            if face_upstream == "UPSTREAM" else b"") + stream_body
    client_left = asyncio.Event()

    async def upstream(request):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            await response.write(b": opened\n\n")
            await client_left.wait()
            await asyncio.sleep(0.2)  # the relay's side of the closed connection is torn down by now
            await response.write(late)
            for _ in range(250):  # until the relay stops reading
                await asyncio.sleep(0.02)
                await response.write(b": still here\n\n")
        except ConnectionError:
            pass
        return response

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("4861", "secret-x")])
    args = relay_args(tmp_path, {})
    args.anthropic_model, args.responses_model = "m", "m"
    args.stop_file = str(tmp_path / "STOP")
    live = relay.Relay(args)
    log = Path(args.log)

    def calls():
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [row for row in rows if row.get("kind") in ("responses", "messages", "chat")]

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/up", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, face_upstream, str(server.make_url("/up")))
            # Served like relay.main serves it: TestServer cancels a handler whose client
            # left, while the deployed relay's handler keeps reading until a write fails.
            runner = web.AppRunner(relay.build_app(live))
            await runner.setup()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            await web.SockSite(runner, listener).start()
            try:
                async with ClientSession() as session:
                    reply = await session.post(f"http://127.0.0.1:{listener.getsockname()[1]}{path}",
                                               json={**payload, "stream": True})
                    assert reply.status == 200
                    await reply.content.readany()
                    reply.close()
                    client_left.set()
                    for _ in range(400):
                        if calls():
                            return
                        await asyncio.sleep(0.025)
            finally:
                await runner.cleanup()

    asyncio.run(exercise())
    [call] = calls()
    assert call["client_gone"] and (call["task"], call["status"]) == ("regex-log", 200)
    assert call["stream_429"] is True
    assert json.loads((tmp_path / "STOP").read_text())["reason"] == "upstream_429"
    if face_upstream == "UPSTREAM":
        assert call["usage"] == usage


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


def matrix_runner(tmp_path: Path, model: str) -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\n')
    (task_dir / "instruction.md").write_text("Fix the regex.\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"],
        arms=["opencode", "pi", "codex", "claude-code"], protocol="matrix-tb21", model=model,
        relay_url="http://r:1", files_url="http://r:1/files", artifacts_manifest=tmp_path / "a.json",
        endpoint="https://g/system/857", image="rootfs://x", branch="b", proxy="http://p:1", max_alive_min=300,
        max_attempts=3, context_tokens=harness.MATRIX_MODELS[model]["context_tokens"], poll_interval_sec=1,
        run_label="r1", source_guard=False, allow_hosts=["pypi.org"])
    live.protocol = harness.PROTOCOLS["matrix-tb21"]
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    live.artifacts = {name: harness.Artifact(name, f"{name}.tar.gz", "0" * 64, f"{name} pinned")
                      for name in ("opencode", "pi", "codex", "claude-code", "tofu")}
    return live


def test_glm_53_flash_runs_every_core_arm_under_its_logical_name(tmp_path):
    """The matrix names glm-5.3-flash by its logical id (Pi's catalog refuses the wire id); Claude Code uses
    FRIDAY's Anthropic face, Codex Responses, Pi its catalog face, and every arm is told the same window."""
    live = matrix_runner(tmp_path, "glm-5.3-flash")
    task = live.tasks["regex-log"]
    assert [harness.arm_face(arm, "glm-5.3-flash") for arm in ("claude-code", "codex", "opencode")] == [
        "anthropic", "responses", "chat"]
    for arm in ("opencode", "pi", "codex", "claude-code", "tofu"):
        assert live.agent_for(task, arm)["program"]
    assert live.config.context_tokens == 1_000_000



def test_a_runs_tofu_thinking_sets_only_the_tofu_arms_depth_and_is_frozen_with_the_run(tmp_path, monkeypatch):
    """A Tofu cohort can run at a model's official default depth (Doubao: medium) while every other
    arm keeps the run model's effort; the level is one of Tofu's own depths and is frozen in run.json."""
    from lib.agent_core.run_contract import THINKING_DEPTHS
    from evaluations.terminal_bench import __main__ as cli

    assert set(harness.TOFU_THINKING_DEPTHS) == THINKING_DEPTHS
    live = matrix_runner(tmp_path / "m", "MiniMax-M2.7")
    task = live.tasks["regex-log"]
    pi_program = live.agent_for(task, "pi")["program"]
    assert "--thinking" not in live.agent_for(task, "tofu")["program"]
    live.config.tofu_thinking = "medium"
    assert "--thinking medium --trajectory atif" in live.agent_for(task, "tofu")["program"]
    assert live.agent_for(task, "pi")["program"] == pi_program

    task_dir = tmp_path / "tasks" / "a-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\n')
    (task_dir / "instruction.md").write_text("do it\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    common = ["--concurrency", "1", "--tasks-dir", str(tmp_path / "tasks"), "--arms", "tofu",
              "--protocol", "matrix-tb21", "--model", "Doubao-Seed-2.0-pro", "--relay-url", "http://r:1",
              "--artifacts-manifest", str(tmp_path / "a.json"), "--endpoint", "https://g/system/857",
              "--image", "i", "--proxy", "http://p:1", "--branch", "b"]
    monkeypatch.setattr(cli, "verify_task_identity",
                        lambda tasks_dir, names, pins: {"dataset_commit": "test", "checked": len(names),
                                                        "mismatched": []})
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--work-dir", str(tmp_path / "x"), *common, "--tofu-thinking", "minimal"])
    config = cli.frozen_config(parser.parse_args(["run", "--work-dir", str(tmp_path / "run"), *common,
                                                  "--tofu-thinking", "medium"]))
    assert config["tofu_thinking"] == "medium"
    resumed = parser.parse_args(["run", "--work-dir", str(tmp_path / "run"), "--concurrency", "2",
                                 "--tofu-thinking", "max"])
    assert cli.frozen_config(resumed)["tofu_thinking"] == "medium"


def test_a_resume_refuses_a_relay_other_than_the_frozen_one(tmp_path, monkeypatch):
    """A resumed run keeps its frozen relay_url; a different --relay-url must fail loudly instead of being ignored
    (new attempts would otherwise call the old relay, e.g. a stopped one, and end unmeasured)."""
    from evaluations.terminal_bench import __main__ as cli

    task_dir = tmp_path / "tasks" / "a-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\n')
    (task_dir / "instruction.md").write_text("do it\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    monkeypatch.setattr(cli, "verify_task_identity",
                        lambda tasks_dir, names, pins: {"dataset_commit": "test", "checked": len(names),
                                                        "mismatched": []})
    parser = cli.build_parser()
    work_dir = str(tmp_path / "run")
    first = cli.frozen_config(parser.parse_args([
        "run", "--work-dir", work_dir, "--concurrency", "1", "--tasks-dir", str(tmp_path / "tasks"),
        "--arms", "opencode", "--protocol", "matrix-tb21", "--model", "Doubao-Seed-2.0-pro",
        "--relay-url", "http://r:1/", "--artifacts-manifest", str(tmp_path / "a.json"),
        "--endpoint", "https://g/system/857", "--image", "i", "--proxy", "http://p:1", "--branch", "b"]))
    assert first["relay_url"] == "http://r:1"
    assert first["branch"] == "b"
    same = parser.parse_args(["run", "--work-dir", work_dir, "--concurrency", "2", "--relay-url", "http://r:1"])
    assert cli.frozen_config(same)["relay_url"] == "http://r:1"
    assert cli.frozen_config(parser.parse_args(["run", "--work-dir", work_dir, "--concurrency", "2"]))[
        "relay_url"] == "http://r:1"
    other = parser.parse_args(["run", "--work-dir", work_dir, "--concurrency", "2", "--relay-url", "http://r:2"])
    with pytest.raises(SystemExit) as refused:
        cli.frozen_config(other)
    assert "http://r:1" in str(refused.value) and "http://r:2" in str(refused.value)


def test_a_new_run_needs_an_ingress_branch_and_a_resume_keeps_the_frozen_one(tmp_path, monkeypatch):
    """With no TOFU_TB_INGRESS_BRANCH declared, freezing a new run requires --branch; resuming a frozen run does not
    (its run.json already names the branch every attempt uses)."""
    from evaluations.terminal_bench import __main__ as cli

    monkeypatch.setattr(cli, "INGRESS_BRANCH", "")
    parser = cli.build_parser()
    new = parser.parse_args(["run", "--work-dir", str(tmp_path / "new"), "--concurrency", "1",
                             "--tasks-dir", str(tmp_path), "--relay-url", "http://r:1",
                             "--artifacts-manifest", str(tmp_path / "a.json"), "--endpoint", "https://g/system/857",
                             "--image", "i", "--proxy", "http://p:1"])
    with pytest.raises(SystemExit, match="--branch"):
        cli.frozen_config(new)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "run.json").write_text(json.dumps({"relay_url": "http://r:1", "branch": "frozen"}))
    resumed = parser.parse_args(["run", "--work-dir", str(tmp_path / "old"), "--concurrency", "1"])
    assert cli.frozen_config(resumed)["branch"] == "frozen"


def test_each_arm_uses_its_own_face_of_one_deployment_with_the_models_effort_and_cap(tmp_path):
    minimax = matrix_runner(tmp_path / "m", "MiniMax-M2.7")
    task = minimax.tasks["regex-log"]
    assert minimax.model_base_url(task, "opencode") == "http://r:1/arm/opencode-r1/task/regex-log/v1"
    # Pi's built-in MiniMax entry speaks Anthropic Messages.
    assert minimax.model_base_url(task, "pi") == "http://r:1/arm/pi-r1/task/regex-log/anthropic"
    pi = minimax.agent_for(task, "pi")
    assert "--provider minimax --model MiniMax-M2.7 --thinking medium" in pi["program"]
    assert json.loads(pi["log_files"][pi_contract.GUEST_MODELS_FILE])["providers"]["minimax"][
        "modelOverrides"]["MiniMax-M2.7"] == {"contextWindow": 204800, "maxTokens": 32768}

    deepseek = matrix_runner(tmp_path / "d", "deepseek-v4-flash")
    pi = deepseek.agent_for(deepseek.tasks["regex-log"], "pi")
    assert "--provider deepseek --model deepseek-flash --thinking high" in pi["program"]
    assert deepseek.model_base_url(deepseek.tasks["regex-log"], "pi").endswith("/task/regex-log/v1")

    glm = matrix_runner(tmp_path / "z", "glm-5.2")
    pi = glm.agent_for(glm.tasks["regex-log"], "pi")
    assert "--provider zai --model glm-5.2 --thinking medium" in pi["program"]

    # Pi's built-in xAI entry speaks Responses; Codex gets grok's default effort; Claude Code the Anthropic face.
    grok = matrix_runner(tmp_path / "x", "grok-4.6")
    grok_task = grok.tasks["regex-log"]
    assert "--provider xai --model grok-4.6 --thinking medium" in grok.agent_for(grok_task, "pi")["program"]
    assert grok.model_base_url(grok_task, "pi") == "http://r:1/arm/pi-r1/task/regex-log/v1"
    assert 'model_reasoning_effort = "medium"' in grok.agent_for(grok_task, "codex")["log_files"]["codex-config.toml"]
    assert grok.model_base_url(grok_task, "claude-code").endswith("/task/regex-log/anthropic")
    assert json.loads(grok.agent_for(grok_task, "opencode")["log_files"]["opencode.json"])["provider"]["benchmark"][
        "models"]["grok-4.6"]["limit"] == {"context": 500000, "output": 32768}

    # Kimi K2.7 Code: no effort ladder, so Pi gets its own default level and Codex none.
    kimi27 = matrix_runner(tmp_path / "k27", "kimi-k2.7-code-highspeed")
    k27_task = kimi27.tasks["regex-log"]
    assert ("--provider moonshotai --model kimi-k2.7-code-highspeed --thinking medium"
            in kimi27.agent_for(k27_task, "pi")["program"])
    assert "model_reasoning_effort" not in kimi27.agent_for(k27_task, "codex")["log_files"]["codex-config.toml"]
    assert json.loads(kimi27.agent_for(k27_task, "opencode")["log_files"]["opencode.json"])["provider"][
        "benchmark"]["models"]["kimi-k2.7-code-highspeed"]["limit"] == {"context": 262144, "output": 32768}

    doubao = matrix_runner(tmp_path / "b", "Doubao-Seed-2.0-pro")
    opencode = doubao.agent_for(doubao.tasks["regex-log"], "opencode")
    limits = json.loads(opencode["log_files"]["opencode.json"])["provider"]["benchmark"]["models"]
    assert limits == {"Doubao-Seed-2.0-pro": {"name": "Doubao-Seed-2.0-pro",
                                              "limit": {"context": 128000, "output": 16384}}}

    gpt = matrix_runner(tmp_path / "g", "gpt-6.1-sol")
    codex = gpt.agent_for(gpt.tasks["regex-log"], "codex")
    assert 'model_reasoning_effort = "medium"' in codex["log_files"]["codex-config.toml"]
    assert gpt.model_base_url(gpt.tasks["regex-log"], "codex") == "http://r:1/arm/codex-r1/task/regex-log/v1"

    opus = matrix_runner(tmp_path / "o", "claude-opus-5.5")
    claude = opus.agent_for(opus.tasks["regex-log"], "claude-code")
    assert claude["env"]["ANTHROPIC_BASE_URL"] == "http://r:1/arm/claude-code-r1/task/regex-log/anthropic"
    assert claude["effort"] == "medium" and "--effort medium" in claude["program"]
    assert claude["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"

    # FRIDAY serves these models on its Anthropic face too: Claude Code calls it directly.
    claude = minimax.agent_for(task, "claude-code")
    assert claude["env"]["ANTHROPIC_BASE_URL"] == "http://r:1/arm/claude-code-r1/task/regex-log/anthropic"
    kimi = matrix_runner(tmp_path / "k", "kimi-k3")
    assert kimi.agent_for(kimi.tasks["regex-log"], "claude-code")["effort"] == "max"
    assert harness.arm_face("claude-code", "Doubao-Seed-2.0-pro") == "anthropic"
    assert harness.arm_face("claude-code", "qwen3.5-plus") == "anthropic"
    assert harness.arm_face("claude-code", "kimi-k3.1-pro") == "bridge", "no Anthropic face: the bridge"
    # Kimi's "max" is not a Codex config level: the relay pin carries it instead.
    codex = kimi.agent_for(kimi.tasks["regex-log"], "codex")
    assert "model_reasoning_effort" not in codex["log_files"]["codex-config.toml"]
    assert kimi.model_base_url(kimi.tasks["regex-log"], "opencode").endswith("/task/regex-log/v1")
    # Third-party (HARNESS-ADAPTERS-1) arms speak chat; matrix relays attribute it to the trial.
    assert minimax.model_base_url(task, "oh-my-pi") == "http://r:1/arm/oh-my-pi-r1/task/regex-log/v1"


def test_the_fhe_protocol_keeps_codex_on_its_own_default_effort(tmp_path):
    assert "model_reasoning_effort" not in harness.codex_config(model_base_url="http://r/v1", model="kimi-k3")
    assert harness.run_effort(harness.PROTOCOLS["fhe-v1"], "kimi-k3") is None
    assert harness.run_effort(harness.PROTOCOLS["vendor-6h"], "glm-5.3-flash") == "max"


def test_a_resumed_run_polls_every_attempt_holding_a_session_whatever_the_concurrency(tmp_path, monkeypatch):
    """The ingress kills a VM nobody polls for its inactivity timeout (gateway.DEFAULT_MAX_IDLE_MIN), so
    in-flight attempts are resumed at once (a lower concurrency on resume used to leave some unpolled
    until a worker freed up); they still count against --concurrency, so a new trial starts only once
    fewer than that many are active."""
    import threading

    live = matrix_runner(tmp_path, "MiniMax-M2.7")
    for name in ("regex-log", "sqlite-db-truncate", "openssl-selfsigned-cert"):
        task_dir = tmp_path / "tasks" / name
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 900.0\n'
                                            '[environment]\ndocker_image = "a/b:1"\n')
        (task_dir / "instruction.md").write_text("x\n")
        (task_dir / "tests" / "test.sh").write_text("true\n")
        live.tasks[name] = harness.load_task(task_dir)
    live.config.arms = ["opencode"]
    live.config.max_alive_min = 1080
    live.stop_requested = threading.Event()
    live.log = lambda *args: None
    for name in ("regex-log", "sqlite-db-truncate"):
        runner.write_json(live.trial_dir(name, "opencode") / "attempt-1" / "state.json",
                          {"phase": "running", "session_id": f"s-{name}", "exec_id": f"e-{name}"})
    started, finished, both_in_flight = [], [], threading.Barrier(2, timeout=5)

    def fake_trial(task, arm):
        started.append(task)
        if task != "openssl-selfsigned-cert":
            both_in_flight.wait()  # breaks (BrokenBarrierError) if the two were serialized
        else:
            assert len(finished) == 2, "a new trial started while the resumed ones filled the slot"
        finished.append(task)
        return {"phase": "scored"}

    live.run_trial = fake_trial
    monkeypatch.setattr(runner, "summarize", lambda work_dir: {})
    live.run(concurrency=1)
    assert sorted(started[:2]) == ["regex-log", "sqlite-db-truncate"] and started[2] == "openssl-selfsigned-cert"
    assert finished[-1] == "openssl-selfsigned-cert"


def test_upstream_headers_reach_every_face_and_credential_names_are_refused(tmp_path, monkeypatch):
    """FRIDAY's namespaced routes (tokrouter, kunlun) need ``x-context-provider``; without it the
    tokrouter GPT route answers a hollow HTTP 200 (empty message, zero tokens; seen 2026-11-12)."""
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    seen = []

    async def upstream(request):
        seen.append((request.path, request.headers.get("x-context-provider"), request.headers.get("Authorization")))
        if request.path == "/responses":
            return web.json_response({"id": "r", "status": "completed", "output": [],
                                      "usage": {"input_tokens": 1, "output_tokens": 1}})
        if request.path == "/messages":
            return web.json_response({"id": "m", "type": "message", "content": [], "usage": {}})
        return web.json_response({"id": "c", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("0733", "secret-x")])
    args = relay_args(tmp_path, {})
    args.anthropic_model, args.responses_model = "m", "tokrouter.gpt-6.1-sol.distill-m17"
    args.key_suffixes = "0733"
    args.upstream_header = ["x-context-provider=tokrouter"]
    live = relay.Relay(args)

    async def exercise():
        upstream_app = web.Application()
        for path in ("/chat", "/messages", "/responses"):
            upstream_app.router.add_post(path, upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            monkeypatch.setattr(relay, "ANTHROPIC_UPSTREAM", str(server.make_url("/messages")))
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                for path, payload in (("/arm/codex-r1/task/t/v1/responses", {"input": "hi"}),
                                      ("/arm/opencode-r1/task/t/v1/chat/completions",
                                       {"messages": [{"role": "user", "content": "hi"}]}),
                                      ("/arm/claude-code-r1/task/t/anthropic/v1/messages",
                                       {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})):
                    assert (await client.post(path, json=payload)).status == 200

    asyncio.run(exercise())
    assert sorted(path for path, _, _ in seen) == ["/chat", "/messages", "/responses"]
    assert all(provider == "tokrouter" and auth == "Bearer secret-x" for _, provider, auth in seen)
    for bad in (["Authorization=Bearer x"], ["x-api-key=y"], ["no-equals-sign"]):
        args.upstream_header = bad
        with pytest.raises(SystemExit):
            relay.Relay(args)
