"""FrontierHarness Eval v1 on the native runner: Codex arm, FHE protocol, allowlist, relay faces, export."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.terminal_bench import harness, runner, vm_trial

pytestmark = pytest.mark.unit

KIMI = "kimi-k3"


def fhe_task(root: Path, name: str = "regex-log", cpus: int = 1, memory_mb: int = 2048) -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
        f'[environment]\ndocker_image = "alexgshaw/{name}:20251031"\ncpus = {cpus}\nmemory_mb = {memory_mb}\n'
        'allow_internet = false\n[environment.env]\nPATH = "/app/.venv/bin:/usr/bin:/bin"\n', encoding="utf-8")
    (task / "instruction.md").write_text("Write the regex.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8")
    return task


def artifact(name: str) -> harness.Artifact:
    return harness.Artifact(name=name, filename=f"{name}.tar.gz", sha256="b" * 64, version=f"{name} 1")


def test_codex_arm_runs_like_harbor_against_the_relay_without_a_credential():
    agent = harness.codex_agent(artifact=artifact("codex"), files_url="http://r:1/files",
                                model_base_url="http://r:1/arm/codex-x/task/regex-log/v1", model=KIMI,
                                instruction="Write the regex.\n")
    config = tomllib.loads(agent["log_files"]["codex-config.toml"])
    assert config["model"] == KIMI and config["model_provider"] == "relay"
    assert config["approval_policy"] == "never" and config["sandbox_mode"] == "danger-full-access"
    assert config["web_search"] == "disabled"
    assert config["model_providers"]["relay"] == {
        "name": "model relay", "base_url": "http://r:1/arm/codex-x/task/regex-log/v1",
        "env_key": harness.CODEX_RELAY_KEY_ENV, "wire_api": "responses"}
    # Nothing tunes the harness: no effort, summary, output cap or context window.
    assert not {"model_reasoning_effort", "model_reasoning_summary", "model_context_window",
                "model_max_output_tokens"} & set(config)
    assert agent["env"][harness.CODEX_RELAY_KEY_ENV] == "relay-held"
    assert agent["env"]["CODEX_HOME"] == "/logs/agent/codex-home"
    assert agent["env"]["no_proxy"].startswith("r,")
    program = agent["program"]
    subprocess.run(["bash", "-n", "-c", program], check=True)
    for flag in ("exec --dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                 f"--model {KIMI}", "--json", "--enable unified_exec", '-C "$TB_WORKDIR"'):
        assert flag in program
    assert program.count(" exec ") == 1 and "resume" not in program
    assert agent["artifacts"][0]["container_path"] == harness.CODEX_HOME


def make_runner(tmp_path: Path, model: str = KIMI, protocol: str = "fhe-v1", allow_hosts=None) -> runner.Runner:
    config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path, tasks=["regex-log"], arms=["tofu", "codex"],
        protocol=protocol, model=model, relay_url="http://relay:9", files_url="http://relay:9/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://proxy:8080", max_alive_min=60, max_attempts=1, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="smoke", allow_hosts=allow_hosts)
    live = runner.Runner.__new__(runner.Runner)
    live.config = config
    live.protocol = harness.PROTOCOLS[protocol]
    live.artifacts = {"tofu": artifact("tofu"), "codex": artifact("codex")}
    live.tasks = {"regex-log": harness.load_task(fhe_task(tmp_path))}
    return live


def test_fhe_protocol_gives_each_arm_its_face_task_budgets_and_the_shared_allowlist(tmp_path):
    allow = ["pypi.org", "*.github.com", "github.com", "pypi.org"]
    live = make_runner(tmp_path, allow_hosts=allow)
    task = live.tasks["regex-log"]
    assert live.model_base_url(task, "codex") == "http://relay:9/arm/codex-smoke/task/regex-log/v1"
    assert live.model_base_url(task, "tofu") == "http://relay:9/arm/tofu-smoke/task/regex-log/anthropic"
    specs = {arm: live.spec_for(task, arm, 1) for arm in ("tofu", "codex")}
    for spec in specs.values():
        assert spec["cpus"] == 1 and spec["memory"] == "2048m"
        assert spec["agent_timeout_sec"] == 1200 and spec["verifier_attempts"] == 1
        assert spec["egress"]["allow_hosts"] == ["*.github.com", "github.com", "pypi.org"]
        assert spec["task_env"] == {"PATH": "/app/.venv/bin:/usr/bin:/bin"}
    tofu = specs["tofu"]["agent"]
    assert "--thinking" not in tofu["program"] and "--timeout-s 1200" in tofu["program"]
    envelope = json.loads(tofu["env"]["TOFU_AGENT_MODEL_ROUTING"])
    routing = envelope["model_routing"]
    assert envelope["model"] == {"creator_id": "moonshot", "model_id": KIMI}
    assert routing["connections"][0]["protocol"] == "anthropic"
    assert routing["providers"][0]["provider_id"] == harness.TOFU_RUN_ROUTES[KIMI]["provider_id"]
    assert routing["deployments"][0]["wire_model_id"] == KIMI
    assert routing["deployments"][0]["max_output_tokens"] == 32768
    assert routing["offerings"][0]["context_window"] == 1_000_000
    with pytest.raises(ValueError, match="cpus and memory_mb"):
        harness.build_trial_spec(task=harness.Task(**{**task.__dict__, "cpus": None}), arm="tofu", attempt=1,
                                 agent=tofu, protocol=live.protocol, agent_timeout_sec=1200, egress_upstream=None)
    live.config.source_guard = True
    with pytest.raises(ValueError, match="source-guard"):
        live.agent_for(task, "codex")


def test_existing_protocols_keep_their_resources_and_no_allowlist(tmp_path):
    live = make_runner(tmp_path, model="glm-5.3-flash", protocol="vendor-6h")
    task = live.tasks["regex-log"]
    spec = live.spec_for(task, "tofu", 1)
    assert spec["cpus"] == 4 and spec["memory"] == "16g" and "allow_hosts" not in spec["egress"]
    assert "--thinking max" in spec["agent"]["program"]
    assert live.model_base_url(task, "tofu") == "http://relay:9/arm/tofu-smoke/v1"


def test_egress_allowlist_matches_entries_and_subdomains_never_apexes_of_wildcards(tmp_path):
    proxy = vm_trial.EgressProxy("127.0.0.1", None, ["tbench.ai"], [], tmp_path / "egress.jsonl",
                                 allow_hosts=["github.com", "*.githubusercontent.com", "*.astral.sh", "127.0.0.1"])
    assert proxy.refusal("github.com") is None and proxy.refusal("GitHub.com.") is None
    assert proxy.refusal("objects.githubusercontent.com") is None
    assert proxy.refusal("githubusercontent.com") == "not_allowlisted"
    assert proxy.refusal("api.github.com") == "not_allowlisted"
    assert proxy.refusal("evilgithub.com") == "not_allowlisted"
    assert proxy.refusal("astral.sh") == "not_allowlisted"
    assert proxy.refusal("www.tbench.ai") == "deny_list"
    open_proxy = vm_trial.EgressProxy("127.0.0.1", None, [], [], tmp_path / "open.jsonl")
    assert open_proxy.refusal("anything.example") is None
    host, port = proxy.address.split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
        assert sock.recv(100).startswith(b"HTTP/1.1 403")
    for _ in range(100):
        rows = [json.loads(line) for line in (tmp_path / "egress.jsonl").read_text().splitlines()] \
            if (tmp_path / "egress.jsonl").exists() else []
        if rows:
            break
        threading.Event().wait(0.05)
    assert rows[0]["decision"] == "deny" and rows[0]["rule"] == "not_allowlisted" and rows[0]["host"] == "pypi.org"


def test_request_window_holds_starts_to_the_assigned_rpm():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    now = [100.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    window = relay.RequestWindow(2, clock=lambda: now[0], sleep=fake_sleep)

    async def exercise():
        assert await window.acquire() == 0.0
        now[0] += 10.0
        assert await window.acquire() == 0.0
        return await window.acquire()

    assert asyncio.run(exercise()) == pytest.approx(50.0)
    assert sleeps == [pytest.approx(50.0)] and window.in_window() == 2
    assert asyncio.run(relay.RequestWindow(None).acquire()) == 0.0


def test_injected_context_lists_block_ids_and_date_markers_without_text():
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from evaluations.terminal_bench import relay

    body = {"system": [{"type": "text", "text": "<!-- tofu-context:platform_static:start -->x"},
                       {"type": "text", "text": "<!-- tofu-context:current_date:start -->Current date: 2026"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Solve it."}]}]}
    facts = relay.prompt_context(body)
    assert facts["tofu_blocks"] == ["current_date", "platform_static"]
    assert facts["date_markers"] == ["tofu-context:current_date", "Current date:"]
    codex = {"instructions": "You are Codex.", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "<environment_context><current_date>2026-11-12"
                                                                     "</current_date></environment_context>"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Solve it."}]}]}
    assert relay.prompt_context(codex)["date_markers"] == ["<current_date>"]
    assert "2026" not in json.dumps({key: value for key, value in facts.items() if key != "context_chars"})
    claude_code = {"system": [{"type": "text", "text": "You are Claude Code."}], "messages": [
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\n# currentDate\n"
                                                               "Today's date is 2026-11-12.\n</system-reminder>"},
                                      {"type": "text", "text": "Solve it."}]}]}
    assert relay.prompt_context(claude_code)["date_markers"] == ["# currentDate", "Today's date"]
    opencode = {"messages": [{"role": "system", "content": "<env>\n  Platform: linux\n  Today's date: Thu Nov 12 2026\n"
                                                          "</env>"},
                             {"role": "user", "content": "Solve it."}]}
    assert relay.prompt_context(opencode)["date_markers"] == ["Today's date"]


def test_relay_reveals_only_credentials_with_the_selected_key_suffix(monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    httpx = pytest.importorskip("httpx")
    from evaluations.terminal_bench import relay

    revealed = []

    class Reply:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, path):
            credentials = [{"credential_id": name, "provider_access_id": "a", "enabled": True, "kind": "api_key",
                            "key_hint": "sk-..." + name} for name in ("5409", "0733", "4861")]
            return Reply({"data": {"model_routing": {
                "provider_accesses": [{"provider_access_id": "a", "provider_id": "example-corp"}],
                "credentials": credentials}}})

        def post(self, path):
            name = path.split("/")[-3]
            revealed.append(name)
            return Reply({"data": {"secret": "secret-" + name}})

    monkeypatch.setattr(httpx, "Client", Client)
    assert relay.load_keys("http://origin", "example-corp", ["0733"]) == [("0733", "secret-0733")]
    assert revealed == ["0733"]
    with pytest.raises(SystemExit, match="1111"):
        relay.load_keys("http://origin", "example-corp", ["1111"])
    assert revealed == ["0733"]


def test_relay_responses_face_pins_the_model_streams_unchanged_and_records_usage(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    captured = []
    usage = {"input_tokens": 12000, "input_tokens_details": {"cached_tokens": 9000}, "output_tokens": 300,
             "output_tokens_details": {"reasoning_tokens": 120}}
    stream = (
        'event: response.created\ndata: {"type":"response.created","response":{"model":"kimi-k3",'
        '"max_output_tokens":32768,"status":"in_progress"}}\n\n'
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": {
            "status": "completed", "usage": usage, "output": [{"type": "reasoning"}, {"type": "message"}]}})
        + '\n\n')

    async def upstream(request):
        captured.append({"headers": dict(request.headers), "body": await request.json()})
        return web.Response(text=stream, content_type="text/event-stream")

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("0733", "upstream-0733")])
    args = SimpleNamespace(
        origin="unused", provider="unused", key_suffixes="0733", files_dir=None,
        log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model=KIMI,
        temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model=None,
        responses_model=KIMI, responses_max_output_tokens=32768, max_rpm=40,
        first_body_dir=str(tmp_path / "first"), anthropic_body_dir=None, preserve_pi_effort_markers=False)
    live = relay.Relay(args)
    payload = {"model": "kimi-k3", "stream": True, "instructions": "You are Codex.", "store": False,
               "input": [{"role": "user", "content": [{"type": "input_text", "text": "Solve it."}]}],
               "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
               "reasoning": {"summary": "auto"}}

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/responses", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "RESPONSES_UPSTREAM", str(server.make_url("/v1/responses")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                async with client.post("/arm/codex-smoke/task/regex-log/v1/responses", json=payload,
                                       headers={"Authorization": "Bearer relay-held"}) as response:
                    assert response.status == 200
                    return await response.text()

    assert asyncio.run(exercise()) == stream
    sent = captured[0]
    assert sent["headers"]["Authorization"] == "Bearer upstream-0733"
    assert sent["body"] == {**payload, "max_output_tokens": 32768}
    record = json.loads((tmp_path / "requests.jsonl").read_text())
    assert record["kind"] == "responses" and record["task"] == "regex-log" and record["key"] == "0733"
    assert record["usage"] == usage and record["response_status"] == "completed"
    assert record["upstream_max_output_tokens"] == 32768 and record["max_output_tokens_filled"] == 32768
    assert record["orig"]["max_output_tokens"] is None and record["orig"]["tool_types"] == ["function"]
    first = tmp_path / "first" / "codex-smoke__regex-log__responses.json"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert json.loads(first.read_text())["body"]["instructions"] == "You are Codex."
    assert "upstream-0733" not in (tmp_path / "requests.jsonl").read_text() + first.read_text()


def git_repo(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pin"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_prepare_tasks_overlays_the_published_definition_and_pins_it(tmp_path, monkeypatch):
    pytest.importorskip("dirhash")
    from evaluations.frontier_harness import tasks
    from evaluations.terminal_bench.__main__ import verify_task_identity

    fhe, tb2 = tmp_path / "fhe", tmp_path / "tb2"
    fhe.mkdir()
    tb2.mkdir()
    fhe_head = git_repo(fhe, {
        "benchmark.json": json.dumps({"name": "FrontierHarness Eval v1", "task_ids": [
            "datacurve/anko", "terminal-bench/regex-log"]}),
        "tasks/regex-log/task.toml": '[agent]\ntimeout_sec = 1200.0\n[verifier]\ntimeout_sec = 900.0\n'
                                     '[environment]\ndocker_image = "a/regex-log:1"\ncpus = 1\nmemory_mb = 2048\n',
        "tasks/regex-log/instruction.md": "Published instruction.\n",
        "skills/frontierharness-eval/scripts/providers.sh": " ".join(tasks.FHE_V1_ALLOWED_HOSTS),
    })
    tb2_head = git_repo(tb2, {
        "regex-log/task.toml": '[agent]\ntimeout_sec = 900.0\n',
        "regex-log/instruction.md": "Upstream instruction.\n",
        "regex-log/tests/test.sh": "echo 1 > /logs/verifier/reward.txt\n",
        "regex-log/solution/solve.sh": "echo solution\n",
    })
    monkeypatch.setattr(tasks, "FHE_COMMIT", fhe_head)
    monkeypatch.setattr(tasks, "TB2_COMMIT", tb2_head)
    out = tmp_path / "out"
    manifest = tasks.prepare_tasks(fhe, tb2, out, ["regex-log"])
    assert sorted(manifest["tasks"]) == ["regex-log"] and manifest["deep_swe_commit"] is None
    assert manifest["dataset_commit"] == f"fhe-v1@{fhe_head[:12]}+tb2@{tb2_head[:12]}"
    task = harness.load_task(out / "regex-log")
    assert task.agent_timeout_sec == 1200.0 and task.instruction == "Published instruction.\n"
    assert (out / "regex-log/tests/test.sh").is_file() and (out / "regex-log/solution/solve.sh").is_file()
    identity = verify_task_identity(out, ["regex-log"], out / "task_checksums.json")
    assert identity["mismatched"] == [] and identity["dataset_commit"] == manifest["dataset_commit"]
    assert json.loads((out / "allowlist.json").read_text()) == sorted(tasks.FHE_V1_ALLOWED_HOSTS)
    with pytest.raises(ValueError, match="not empty"):
        tasks.prepare_tasks(fhe, tb2, out, ["regex-log"])
    with pytest.raises(ValueError, match="not v1"):
        tasks.prepare_tasks(fhe, tb2, tmp_path / "other", ["no-such-task"])


def native_run(root: Path) -> Path:
    work = root / "native"
    work.mkdir()
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fhe-task-manifest.json").write_text(json.dumps({"tasks": {
        name: {"id": f"terminal-bench/{name}"} for name in ("regex-log", "openssl-selfsigned-cert")}}))
    (work / "run.json").write_text(json.dumps({
        "run_label": "smoke", "arms": ["tofu", "codex"], "tasks": ["regex-log", "openssl-selfsigned-cert"],
        "tasks_dir": str(tasks_dir), "allow_hosts": ["github.com"],
        "task_identity": {"dataset_commit": "fhe-v1@x+tb2@y"}}))

    def attempt(arm, task, number, submitted, state, result=None):
        directory = work / "trials" / arm / task / f"attempt-{number}"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"submitted_epoch": submitted, **state}))
        if result is not None:
            (directory / "result.json").write_text(json.dumps(result))

    def result(reward, outcome="scored", **agent):
        return {"outcome": outcome, "reward": reward, "finished_at": 1300.0,
                "phases": {"prepare": {"started": 1000.0}, "agent": {"started": 1010.0, **agent}}}

    attempt("tofu", "regex-log", 1, 1000.0, {"phase": "scored"}, result(1.0, exit_code=0, timed_out=False))
    attempt("tofu", "openssl-selfsigned-cert", 1, 1000.0, {"phase": "lost"})
    attempt("tofu", "openssl-selfsigned-cert", 2, 5000.0, {"phase": "scored"},
            result(0.0, exit_code=3, timed_out=False))
    attempt("codex", "regex-log", 1, 1000.0, {"phase": "scored"}, result(0.0, exit_code=0, timed_out=False))
    attempt("codex", "openssl-selfsigned-cert", 1, 1000.0, {"phase": "no_reward"},
            result(None, outcome="no_reward", exit_code=0, timed_out=False))
    return work


def relay_log(path: Path) -> Path:
    rows = [
        {"kind": "messages", "arm": "tofu-smoke", "task": "regex-log", "status": 200, "ts": 1100.0, "key": "0733",
         "usage_start": {"input_tokens": 1000, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 0,
                         "output_tokens": 1}, "usage_delta": {"output_tokens": 100}},
        {"kind": "messages", "arm": "tofu-smoke", "task": "regex-log", "status": 200, "ts": 1200.0, "key": "0733",
         "usage_start": {"input_tokens": 500, "cache_read_input_tokens": 10000}, "usage_delta": {"output_tokens": 50}},
        {"kind": "messages", "arm": "tofu-smoke", "task": "openssl-selfsigned-cert", "status": 200, "ts": 1100.0,
         "key": "0733", "usage_start": {"input_tokens": 7}, "usage_delta": {"output_tokens": 7}},
        {"kind": "messages", "arm": "tofu-smoke", "task": "openssl-selfsigned-cert", "status": 200, "ts": 5100.0,
         "key": "0733", "usage_start": {"input_tokens": 100}, "usage_delta": {"output_tokens": 10}},
        {"kind": "responses", "arm": "codex-smoke", "task": "regex-log", "status": 200, "ts": 1150.0, "key": "0733",
         "usage": {"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1500}, "output_tokens": 40}},
        {"kind": "responses", "arm": "codex-smoke", "task": "regex-log", "status": 502, "ts": 1160.0, "key": "0733"},
        {"kind": "file", "name": "codex.tar.gz", "ts": 1001.0},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def fhe_repo(root: Path) -> Path:
    repo = root / "fhe"
    scripts = repo / "skills/frontierharness-eval/scripts"
    scripts.mkdir(parents=True)
    (scripts / "pricing.json").write_text(json.dumps({
        "version": "kimi-k3-2026-08-20", "unit_tokens": 1000000,
        "models": {"k3": {"fresh_input": 3.0, "cache_write": 3.0, "cache_read": 0.3, "output": 15.0}}}))
    (repo / "benchmark.json").write_text(json.dumps({
        "task_count": 2, "task_ids": ["terminal-bench/openssl-selfsigned-cert", "terminal-bench/regex-log"]}))
    return repo


def test_export_applies_fhe_status_rules_and_prices_relay_usage(tmp_path):
    from evaluations.frontier_harness import export

    summary = export.export(native_dir=native_run(tmp_path), relay_log=relay_log(tmp_path / "relay.jsonl"),
                            fhe_repo=fhe_repo(tmp_path), out_root=tmp_path / "fhe-runs", label="smoke-test",
                            run_id="smoke-test", provider="FRIDAY kimi-k3", run_normalizer=False)

    def trial(arm, name):
        return json.loads((tmp_path / "fhe-runs" / arm / "trials" / name / "trial.json").read_text())

    first = trial("tofu", "regex-log__a1")
    assert first["status"] == "success" and first["turns"] == 2 and first["keys"] == ["0733"]
    # (1000*3 + 9000*0.3 + 100*15) + (500*3 + 10000*0.3 + 50*15) micro-dollars; first call's cached
    # reads are repriced at the fresh rate for the first-cold cost.
    assert first["cost_usd"] == pytest.approx(0.0072 + 0.00525)
    assert first["cost_first_cold_usd"] == pytest.approx(0.01245 + 9000 * 2.7e-6)
    assert first["input_tokens"] == 20500 and first["cached_input_tokens"] == 19000 and first["output_tokens"] == 150
    assert first["first_turn_cached_tokens"] == 9000 and first["duration_seconds"] == 300.0
    lost = trial("tofu", "openssl-selfsigned-cert__a1")
    assert lost["status"] == "infra_invalid" and lost["turns"] == 1
    retried = trial("tofu", "openssl-selfsigned-cert__a2")
    assert retried["status"] == "failure" and retried["turns"] == 1 and retried["attempt"] == 1
    codex = trial("codex", "regex-log__a1")
    assert codex["status"] == "failure" and codex["turns"] == 1
    assert codex["cost_usd"] == pytest.approx(500 * 3e-6 + 1500 * 0.3e-6 + 40 * 15e-6)
    assert trial("codex", "openssl-selfsigned-cert__a1")["status"] == "infra_invalid"
    run = json.loads((tmp_path / "fhe-runs" / "codex" / "run.json").read_text())
    assert run["harness"] == "codex" and run["methodology_comparable"] is False
    assert run["egress_policy"] == {"mode": "allowlist", "scope": "trial egress proxy (agent and verifier)",
                                    "allowed_hosts": ["github.com"]}
    calls = [json.loads(line) for line in (tmp_path / "fhe-runs" / "tofu" / "calls.jsonl").read_text().splitlines()]
    assert [(row["task"], row["attempt"]) for row in calls] == [
        ("regex-log", 1), ("regex-log", 1), ("openssl-selfsigned-cert", 1), ("openssl-selfsigned-cert", 2)]
    assert summary["arms"]["tofu"]["keys"] == ["0733"]


def test_fhe_normalizer_scores_the_export_and_keeps_smoke_out_of_canonical_cells(tmp_path):
    node = shutil.which("node")
    checkout = os.environ.get("FHE_EVAL_CHECKOUT")
    script = Path(checkout or "/nonexistent") / "skills/frontierharness-eval/scripts/normalize-results.mjs"
    if not node or not script.is_file():
        pytest.skip("needs node and FHE_EVAL_CHECKOUT (the FHE repository at the pinned commit)")
    from evaluations.frontier_harness import export

    repo = fhe_repo(tmp_path)
    shutil.copy(script, repo / "skills/frontierharness-eval/scripts/normalize-results.mjs")
    native, log = native_run(tmp_path), relay_log(tmp_path / "relay.jsonl")
    scored = export.export(native_dir=native, relay_log=log, fhe_repo=repo, out_root=tmp_path / "check",
                           label="pipeline-check", run_id="check", provider="p")
    candidate = json.loads((tmp_path / "check" / "tofu" / "candidate.json").read_text())
    assert candidate["successful"] == 1 and candidate["completed"] == 2 and candidate["infra_invalid"] == 0
    assert candidate["comparable"] is False
    assert "1/2 passed" in scored["arms"]["tofu"]["normalizer"]
    export.export(native_dir=native, relay_log=log, fhe_repo=repo, out_root=tmp_path / "smoke",
                  label="smoke-20261112", run_id="smoke", provider="p")
    assert json.loads((tmp_path / "smoke" / "tofu" / "candidate.json").read_text())["completed"] == 0


def test_v11_protocol_keeps_task_budgets_and_asks_every_arm_for_max_effort(tmp_path):
    protocol = harness.PROTOCOLS["fhe-v11"]
    assert protocol.agent_timeout_sec is None and protocol.task_resources and protocol.verifier_attempts == 1
    assert protocol.reasoning_effort == "max" and not protocol.harness_defaults
    live = make_runner(tmp_path, protocol="fhe-v11", allow_hosts=["pypi.org"])
    task = live.tasks["regex-log"]
    tofu = live.spec_for(task, "tofu", 1)
    assert "--thinking max" in tofu["agent"]["program"] and tofu["agent_timeout_sec"] == 1200
    assert tofu["cpus"] == 1 and tofu["memory"] == "2048m" and tofu["verifier_attempts"] == 1
    codex = live.spec_for(task, "codex", 1)
    config = tomllib.loads(codex["agent"]["log_files"]["codex-config.toml"])
    assert config["model_reasoning_effort"] == "max" and config["web_search"] == "disabled"
    assert config["model_providers"]["relay"]["wire_api"] == "responses"


def test_v1_protocol_still_leaves_codex_its_own_effort(tmp_path):
    live = make_runner(tmp_path, protocol="fhe-v1", allow_hosts=["pypi.org"])
    codex = live.spec_for(live.tasks["regex-log"], "codex", 1)
    assert "model_reasoning_effort" not in tomllib.loads(codex["agent"]["log_files"]["codex-config.toml"])


def test_export_scores_an_operator_invalidated_attempt_infra_invalid(tmp_path):
    from evaluations.frontier_harness import export
    from evaluations.terminal_bench import runner as native_runner

    native = native_run(tmp_path)
    attempt = native / "trials" / "tofu" / "regex-log" / "attempt-1"
    native_runner.write_json(attempt / native_runner.INVALIDATION_FILE,
                             {"rule": "D-2: model access withdrawn mid-trial", "evidence": ["relay 401"]})
    export.export(native_dir=native, relay_log=relay_log(tmp_path / "relay.jsonl"), fhe_repo=fhe_repo(tmp_path),
                  out_root=tmp_path / "fhe-runs", label="smoke-test", run_id="smoke-test",
                  provider="FRIDAY kimi-k3", run_normalizer=False)
    trial = json.loads((tmp_path / "fhe-runs" / "tofu" / "trials" / "regex-log__a1" / "trial.json").read_text())
    assert trial["status"] == "infra_invalid"
    assert trial["status_reason"] == "invalidated by the operator: D-2: model access withdrawn mid-trial"
