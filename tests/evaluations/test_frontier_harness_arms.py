"""FHE v1 reference arms: Claude Code at its pinned release, harness defaults, on the relay's Anthropic face."""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from evaluations import claude_code_contract as contract
from evaluations.terminal_bench import harness, runner

pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {"tests/test.sh"}


def fhe_runner(tmp_path: Path, *, anthropic_url=None, protocol="fhe-v1", model="kimi-k3") -> runner.Runner:
    task_dir = tmp_path / "tasks" / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
                                        '[environment]\ndocker_image = "a/b:1"\ncpus = 1\nmemory_mb = 2048\n')
    (task_dir / "instruction.md").write_text("Write the regex.\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["regex-log"], arms=["claude-code"],
        protocol=protocol, model=model, relay_url="http://relay:9", files_url="http://relay:9/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://p:1", max_alive_min=150, max_attempts=2, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="fha-a1", anthropic_url=anthropic_url, source_guard=False,
        allow_hosts=["pypi.org"])
    live.protocol = harness.PROTOCOLS[protocol]
    live.artifacts = {"claude-code": harness.Artifact(name="claude-code", filename="cc.tar.gz", sha256="c" * 64,
                                                      version="claude-code 2.1.237")}
    live.tasks = {"regex-log": harness.load_task(task_dir)}
    return live


def test_fhe_claude_code_runs_with_harness_defaults_on_the_relays_anthropic_face(tmp_path):
    live = fhe_runner(tmp_path)
    agent = live.agent_for(live.tasks["regex-log"], "claude-code")
    env = agent["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://relay:9/arm/claude-code-fha-a1/task/regex-log/anthropic"
    assert env["ANTHROPIC_MODEL"] == "kimi-k3" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "kimi-k3"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    assert "--effort" not in agent["program"] and agent["effort"] is None
    assert "--permission-mode=bypassPermissions" in agent["program"] and "--print" in agent["program"]
    assert env["no_proxy"].startswith("relay,")


def test_a_configured_bridge_and_other_protocols_keep_their_claude_code_launch(tmp_path):
    bridged = fhe_runner(tmp_path / "a", anthropic_url="http://bridge:2")
    agent = bridged.agent_for(bridged.tasks["regex-log"], "claude-code")
    assert agent["env"]["ANTHROPIC_BASE_URL"] == "http://bridge:2/arm/claude-code-fha-a1"
    pinned = fhe_runner(tmp_path / "b", protocol="claude-6h-medium", model="claude-opus-5.5",
                        anthropic_url="http://bridge:2")
    agent = pinned.agent_for(pinned.tasks["regex-log"], "claude-code")
    assert shlex.join(["--effort", harness.CLAUDE_CODE_EFFORT]) in agent["program"]
    assert agent["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
    chat_only = fhe_runner(tmp_path / "c", protocol="vendor-6h", model="glm-5.3-flash")
    with pytest.raises(ValueError, match="anthropic_url"):
        chat_only.agent_for(chat_only.tasks["regex-log"], "claude-code")


def test_claude_code_builds_only_pinned_releases():
    assert contract.release_integrity("2.1.237")[contract.CLAUDE_CODE_PLATFORM_PACKAGE].startswith("sha512-WTE+")
    assert contract.release_integrity(contract.CLAUDE_CODE_VERSION) == contract.CLAUDE_CODE_TARBALL_INTEGRITY
    assert contract.tarball_url(contract.CLAUDE_CODE_PLATFORM_PACKAGE, "2.1.237").endswith(
        "/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.237.tgz")
    with pytest.raises(SystemExit, match="not pinned"):
        contract.release_integrity("2.1.999")
    with pytest.raises(SystemExit):
        contract.main(["build-runtime", "--work-dir", "/nonexistent", "--out-dir", "/nonexistent",
                       "--version", "2.1.999"])


def test_fhe_opencode_uses_the_task_scoped_chat_face_for_kimi(tmp_path):
    live = fhe_runner(tmp_path)
    live.artifacts["opencode"] = harness.Artifact(name="opencode", filename="oc.tar.gz", sha256="d" * 64,
                                                  version="opencode 1.18.19 linux-x64-baseline")
    task = live.tasks["regex-log"]
    assert live.model_base_url(task, "opencode") == "http://relay:9/arm/opencode-fha-a1/task/regex-log/v1"
    agent = live.agent_for(task, "opencode")
    config = __import__("json").loads(agent["log_files"]["opencode.json"])
    assert config["provider"]["benchmark"]["options"]["baseURL"] == \
        "http://relay:9/arm/opencode-fha-a1/task/regex-log/v1"
    assert agent["version"] == "opencode 1.18.19 linux-x64-baseline"
    glm = fhe_runner(tmp_path / "glm", protocol="vendor-6h", model="glm-5.3-flash")
    assert glm.model_base_url(glm.tasks["regex-log"], "opencode") == "http://relay:9/arm/opencode-fha-a1/v1"


def test_relay_chat_face_attributes_task_scoped_requests(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    import asyncio
    import json
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    async def upstream(request):
        return web.json_response({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("6672", "secret-6672")])
    live = relay.Relay(SimpleNamespace(
        origin="unused", provider="unused", key_suffixes="6672", files_dir=None,
        log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model="kimi-k3",
        temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model=None,
        responses_model=None, max_rpm=None, first_body_dir=str(tmp_path / "first"), anthropic_body_dir=None,
        preserve_pi_effort_markers=False, stop_file=None))

    async def exercise():
        app = web.Application()
        app.router.add_post("/chat", upstream)
        async with TestServer(app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                reply = await client.post("/arm/opencode-fha-a1/task/regex-log/v1/chat/completions",
                                          json={"model": "benchmark-name", "stream": False,
                                                "messages": [{"role": "user", "content": "hi"}]})
                return reply.status

    assert asyncio.run(exercise()) == 200
    [record] = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert record["kind"] == "chat" and record["task"] == "regex-log" and record["key"] == "6672"
    assert record["context"]["tofu_blocks"] == [] and record["first_body"].startswith("opencode-fha-a1__regex-log")


def test_agent_inputs_are_pinned_task_files_and_reach_the_trial_spec(tmp_path):
    import hashlib
    import json

    live = fhe_runner(tmp_path)
    task_dir = live.tasks["regex-log"].path
    payload = b"bundle bytes"
    (task_dir / "environment" / "agent-inputs").mkdir(parents=True)
    (task_dir / "environment" / "agent-inputs" / "pyknotid.bundle").write_bytes(payload)
    entry = {"source": "environment/agent-inputs/pyknotid.bundle", "target": "/app/pyknotid.bundle",
             "sha256": hashlib.sha256(payload).hexdigest()}
    (task_dir / harness.AGENT_INPUTS_FILE).write_text(json.dumps([entry]))
    task = harness.load_task(task_dir)
    assert task.agent_inputs == (entry,)
    live.tasks["regex-log"] = task
    live.artifacts["codex"] = harness.Artifact(name="codex", filename="c.tar.gz", sha256="e" * 64, version="codex")
    assert live.spec_for(task, "codex", 1)["agent_inputs"] == [entry]
    for bad, message in ((dict(entry, sha256="0" * 64), "sha256 pin"),
                         (dict(entry, source="tests/test.sh"), "outside tests/"),
                         (dict(entry, source="../x"), "outside tests/"),
                         (dict(entry, target="app/pyknotid.bundle"), "absolute path")):
        (task_dir / harness.AGENT_INPUTS_FILE).write_text(json.dumps([bad]))
        with pytest.raises(ValueError, match=message):
            harness.load_task(task_dir)


def test_vm_trial_places_agent_inputs_owned_by_the_image_user(tmp_path, monkeypatch):
    import hashlib
    import subprocess

    from evaluations.terminal_bench import vm_trial

    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(["docker", *args], 0, b"", b"")

    payload = b"bundle bytes"
    task_dir = tmp_path / "task"
    (task_dir / "environment" / "agent-inputs").mkdir(parents=True)
    (task_dir / "environment" / "agent-inputs" / "pyknotid.bundle").write_bytes(payload)
    spec = {"out_dir": str(tmp_path / "out"), "task_dir": str(task_dir), "trial_key": "k", "trial_id": "t",
            "task_name": "build-cython-ext", "arm": "codex", "agent": {"kind": "codex"},
            "agent_inputs": [{"source": "environment/agent-inputs/pyknotid.bundle", "target": "/app/pyknotid.bundle",
                              "sha256": hashlib.sha256(payload).hexdigest()}]}
    monkeypatch.setattr(vm_trial, "docker", docker)
    trial = vm_trial.Trial(spec)
    trial.place_agent_inputs("1000", "1000")
    assert ("exec", "-u", "0", trial.container, "mkdir", "-p", "/app") in calls
    assert ("cp", str(task_dir / "environment/agent-inputs/pyknotid.bundle"), f"{trial.container}:/app/pyknotid.bundle") in calls
    assert ("exec", "-u", "0", trial.container, "chown", "1000:1000", "/app/pyknotid.bundle") in calls
    assert trial.result["agent_inputs"] == [{"target": "/app/pyknotid.bundle",
                                             "sha256": spec["agent_inputs"][0]["sha256"], "bytes": len(payload)}]
    (task_dir / "environment/agent-inputs/pyknotid.bundle").write_bytes(b"tampered")
    with pytest.raises(vm_trial.InfraError, match="differs from its pin"):
        vm_trial.Trial(spec).place_agent_inputs("1000", "1000")


def test_pyknotid_bundle_holds_the_pinned_tag_and_clones_as_the_instruction_says(tmp_path, monkeypatch):
    import subprocess

    from evaluations.frontier_harness import tasks

    repo = tmp_path / "pyknotid"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    (repo / "setup.py").write_text("# 0.5.3\n")
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "0.5.3"],
                 ["tag", "0.5.3"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:tests"], capture_output=True,
                          text=True).stdout.strip()
    task_dir = tmp_path / "out" / "build-cython-ext"
    task_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="expected"):
        tasks.write_pyknotid_bundle(repo, task_dir)
    monkeypatch.setattr(tasks, "PYKNOTID_COMMIT", head)
    monkeypatch.setattr(tasks, "PYKNOTID_TESTS_TREE", tree)
    monkeypatch.chdir(tmp_path)
    repair = tasks.write_pyknotid_bundle(repo, task_dir.relative_to(tmp_path))
    assert repair["commit"] == head and repair["tests_tree"] == tree
    [entry] = __import__("json").loads((task_dir / "agent-inputs.json").read_text())
    assert entry["target"] == "/app/pyknotid.bundle" and entry["sha256"] == repair["bundle_sha256"]
    assert not (repo / tasks.PYKNOTID_BUNDLE).exists(), "the bundle lands in the task, not in the clone"
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(task_dir / entry["source"]), str(clone)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(clone), "-c", "advice.detachedHead=false", "checkout", "-q", "0.5.3"], check=True)
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == head


def test_fhe_pi_installs_in_the_trial_and_runs_harbors_endpoint_mode_on_the_responses_face(tmp_path):
    import json
    import subprocess

    from evaluations import pi_contract
    from evaluations.terminal_bench import third_party

    live = fhe_runner(tmp_path)
    node = third_party.RUNTIMES["node"]
    live.artifacts["node"] = harness.Artifact(name="node", filename=node["filename"], sha256=node["sha256"],
                                              version=node["version"])
    task = live.tasks["regex-log"]
    url = live.model_base_url(task, "pi")
    assert url == "http://relay:9/arm/pi-fha-a1/task/regex-log/v1"
    spec = live.spec_for(task, "pi", 1)
    agent = spec["agent"]
    # No Pi archive: the pinned Node is copied in and Pi 0.84.2 installs during the trial's setup phase,
    # as Harbor installs Pi, before and outside the agent budget.
    assert [(item["name"], item["container_path"]) for item in agent["artifacts"]] == [("node", third_party.NODE_HOME)]
    setup = agent["setup"]["program"]
    assert f"{pi_contract.PI_PACKAGE}@{third_party.PI_RESPONSES_VERSION}" in setup
    assert f"--prefix {third_party.PI_RESPONSES_HOME}/pi " in setup
    assert f"--before={third_party.DEPENDENCY_SNAPSHOT}" in setup and "npm ls" in setup
    assert agent["setup"]["timeout_sec"] == third_party.SETUP_TIMEOUT_SEC
    assert runner.trial_budget_sec(spec) == (third_party.SETUP_TIMEOUT_SEC + 1200 + 900 * spec["verifier_attempts"]
                                             + runner.TRIAL_OVERHEAD_SEC)
    config = json.loads(agent["log_files"][pi_contract.GUEST_MODELS_FILE])
    assert config == {"providers": {"harbor-endpoint": {
        "baseUrl": url, "apiKey": "relay-held", "api": "openai-responses", "models": [{"id": "kimi-k3"}]}}}
    program = agent["program"]
    for text in (program, setup, agent["install_check"]):
        subprocess.run(["bash", "-n", "-c", text], check=True)
    cli = f"{third_party.PI_RESPONSES_HOME}/pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    assert f"{third_party.NODE} {cli} --print --mode json" in program
    assert "--provider harbor-endpoint --model kimi-k3" in program and "--thinking" not in program
    # Pi 0.84.2 has no end-of-options marker (it exits "Error: Unknown option: --"): the instruction is
    # the last argument, as Harbor passes it, and one that starts with '-' is refused up front.
    assert '--model kimi-k3 "$(< /logs/agent/instruction.md)"' in program
    with pytest.raises(ValueError, match="option"):
        third_party.pi_responses_agent(artifacts=live.artifacts, files_url="http://relay:9/files",
                                       model_base_url=url, model="kimi-k3", instruction="--help me\n")
    assert agent["install_check"].endswith(f"{third_party.NODE} {cli} --version")
    # Node stays off PATH; Pi's quiet settings are the same as for the archive-installed Pi.
    assert "PATH" not in agent["env"] and agent["env"]["PI_OFFLINE"] == "1"
    assert agent["env"]["no_proxy"].startswith("relay,")
    assert agent["kind"] == "pi" and agent["thinking"] is None and "pi 0.84.2" in agent["version"]
    live.config.source_guard = True
    with pytest.raises(ValueError, match="source-guard"):
        live.agent_for(task, "pi")


def test_catalog_pi_launch_and_release_pins_are_unchanged(tmp_path):
    from evaluations import pi_contract

    glm = fhe_runner(tmp_path, protocol="vendor-6h", model="glm-5.3-flash")
    glm.artifacts["pi"] = harness.Artifact(name="pi", filename="pi.tar.gz", sha256="f" * 64, version="pi 1.4.0")
    task = glm.tasks["regex-log"]
    assert glm.model_base_url(task, "pi") == "http://relay:9/arm/pi-fha-a1/v1"
    program = glm.agent_for(task, "pi")["program"]
    assert "--provider zai --model glm-5.3-flash --thinking" in program and "dist/bundle/cli.js" in program
    assert ' -- "$(< /logs/agent/instruction.md)"' in program
    assert pi_contract.pi_cli(pi_contract.PI_VERSION) == pi_contract.PI_CLI
    with pytest.raises(ValueError, match="not pinned"):
        pi_contract.pi_cli("0.0.1")
    with pytest.raises(ValueError, match="endpoint api"):
        pi_contract.guest_endpoint_models_config(base_url="http://r", model="kimi-k3", api="ollama")
    with pytest.raises(SystemExit):
        pi_contract.main(["build-runtime", "--work-dir", "/nonexistent", "--out-dir", "/nonexistent",
                          "--version", "0.0.1"])
