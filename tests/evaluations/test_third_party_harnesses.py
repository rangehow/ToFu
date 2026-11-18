"""Third-party arms on the native runner: launch specs, the per-trial setup phase, the relay chat face."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from evaluations.terminal_bench import harness, runner, third_party, vm_trial

pytestmark = pytest.mark.unit

KIMI = "kimi-k3"
BASE = "http://10.0.0.5:18731/arm/goose-smoke/v1"


def runtime_artifacts() -> dict[str, harness.Artifact]:
    pinned = {name: harness.Artifact(name=name, filename=item["filename"], sha256=item["sha256"],
                                     version=item["version"]) for name, item in third_party.RUNTIMES.items()}
    # A packed release binary's archive digest is computed when it is packed; any digest stands in here.
    packed = {name: harness.Artifact(name=name, filename=item["filename"], sha256="f" * 64, version=item["version"])
              for name, item in third_party.RELEASE_FILES.items()}
    return {**pinned, **packed}


def build(arm: str) -> dict:
    return third_party.ARMS[arm](artifacts=runtime_artifacts(), files_url="http://10.0.0.5:18731/files",
                                 model_base_url=BASE, model=KIMI, instruction="Write the regex.\n")


@pytest.mark.parametrize("arm", sorted(third_party.ARMS))
def test_every_arm_installs_during_setup_and_runs_by_absolute_path_without_a_credential(arm):
    agent = build(arm)
    for program in (agent["program"], agent["setup"]["program"], agent["install_check"]):
        subprocess.run(["bash", "-n", "-c", program], check=True)
    assert agent["setup"]["timeout_sec"] == third_party.SETUP_TIMEOUT_SEC
    assert agent["log_files"]["instruction.md"] == "Write the regex.\n" and agent["deadline_grace_sec"] == 0
    # The task's tools stay as the image has them: nothing joins PATH, the harness runs from its own home.
    assert "PATH" not in agent["env"] and "PATH=" not in agent["program"]
    assert agent["program"].startswith("exec /var/tmp/tofu-")
    assert agent["env"]["no_proxy"].startswith("10.0.0.5,") and agent["env"]["NO_PROXY"] == agent["env"]["no_proxy"]
    assert "sk-" not in json.dumps(agent)
    artifacts = runtime_artifacts()
    for spec in agent["artifacts"]:
        pinned = artifacts[spec["name"]]
        assert spec["sha256"] == pinned.sha256 and spec["url"].endswith("/files/" + pinned.filename)
        assert spec["container_path"].startswith("/var/tmp/tofu-")
    setup = agent["setup"]["program"]
    if "npm install" in setup:
        assert f"--before={third_party.DEPENDENCY_SNAPSHOT}" in setup and "npm ls" in setup
    if " pip install " in setup:
        assert f"--exclude-newer {third_party.DEPENDENCY_SNAPSHOT}" in setup and "pip freeze" in setup
        assert "UV_PYTHON_PREFERENCE=only-managed" in setup


def test_kimi_code_runs_like_harbors_adapter_given_a_base_url():
    agent = build("kimi-code")
    env = agent["env"]
    assert env["KIMI_MODEL_PROVIDER_TYPE"] == "openai" and env["KIMI_MODEL_BASE_URL"] == BASE
    assert env["KIMI_MODEL_NAME"] == KIMI and env["KIMI_MODEL_API_KEY"] == third_party.RELAY_KEY
    assert env["KIMI_DISABLE_TELEMETRY"] == "true" and env["KIMI_CODE_NO_AUTO_UPDATE"] == "true"
    # The adapter injects no model facts or effort for a base URL it was given: harness defaults.
    assert not {"KIMI_MODEL_MAX_CONTEXT_SIZE", "KIMI_MODEL_CAPABILITIES", "KIMI_MODEL_THINKING_EFFORT"} & set(env)
    assert "--output-format stream-json" in agent["program"] and '--prompt "$(cat ' in agent["program"]
    assert f"{third_party.KIMI_CODE_PACKAGE}@{third_party.KIMI_CODE_VERSION} " in agent["setup"]["program"]


def test_qwen_code_runs_like_harbors_adapter_with_openai_auth_yolo_and_chat_recording():
    agent = build("qwen-code")
    program = agent["program"]
    for flag in ("--yolo", "--auth-type openai", f"--model {KIMI}", f"--openai-base-url {BASE}",
                 f"--openai-api-key {third_party.RELAY_KEY}", "--chat-recording", '--prompt "$(cat '):
        assert flag in program
    assert agent["collect"] == [{"container_path": "$HOME/.qwen/projects", "name": "qwen-sessions"}]
    assert f"{third_party.QWEN_CODE_PACKAGE}@{third_party.QWEN_CODE_VERSION} " in agent["setup"]["program"]


def test_goose_reaches_the_relay_chat_face_through_host_and_base_path():
    agent = build("goose")
    env = agent["env"]
    assert env["GOOSE_PROVIDER"] == "openai" and env["GOOSE_MODEL"] == KIMI
    assert env["OPENAI_HOST"] + "/" + env["OPENAI_BASE_PATH"] == BASE + "/chat/completions"
    assert env["OPENAI_API_KEY"] == third_party.RELAY_KEY and env["GOOSE_DISABLE_KEYRING"] == "true"
    recipe = json.loads(agent["log_files"]["goose-recipe.yaml"])
    assert recipe["prompt"] == "Write the regex.\n" and recipe["instructions"] == third_party.GOOSE_RECIPE_INSTRUCTIONS
    assert [extension["name"] for extension in recipe["extensions"]] == ["developer", "todo"]
    config = json.loads(agent["log_files"]["goose-config.yaml"])
    assert set(config["extensions"]) == {"developer", "todo", "summon"} and config["GOOSE_MODEL"] == KIMI
    assert f"exec {third_party.GOOSE} run --recipe /logs/agent/goose-recipe.yaml --output-format stream-json" \
        in agent["program"]
    # The release tree is root-owned; setup links its binary from a directory it creates itself.
    assert f'ln -sf "$binary" {third_party.GOOSE}' in agent["setup"]["program"]


def test_mini_swe_agent_runs_like_harbors_adapter_on_litellm_with_the_relay_as_api_base():
    agent = build("mini-swe-agent")
    program = agent["program"]
    for flag in ("--yolo", f"--model=openai/{KIMI}", "--output=/logs/agent/mini-swe-agent.trajectory.json",
                 "-c mini -c model.model_kwargs.timeout=3600 -c model.model_class=litellm",
                 f"-c model.model_kwargs.api_base={BASE}", '--task="$(cat ', "--exit-immediately"):
        assert flag in program
    assert agent["env"]["MSWEA_CONFIGURED"] == "true" and agent["env"]["MSWEA_COST_TRACKING"] == "ignore_errors"
    assert agent["env"]["OPENAI_API_KEY"] == third_party.RELAY_KEY
    setup = agent["setup"]["program"]
    assert f"--python {third_party.MINI_SWE_AGENT_PYTHON} " in setup
    assert f"mini-swe-agent=={third_party.MINI_SWE_AGENT_VERSION} 'litellm[mcp]' orjson fastapi" in setup


def test_hermes_runs_like_harbors_adapter_with_the_openai_api_provider_and_its_config():
    agent = build("hermes")
    env = agent["env"]
    assert env["OPENAI_BASE_URL"] == BASE and env["OPENAI_API_KEY"] == third_party.RELAY_KEY
    assert env["HERMES_HOME"] == third_party.HERMES_STATE and env["TERMINAL_ENV"] == "local"
    for flag in ("--yolo chat", '-q "$(cat ', " -Q ", f"--model {KIMI}", "--provider openai-api"):
        assert flag in agent["program"]
    config = json.loads(agent["log_files"]["hermes-config.yaml"])
    assert config["model"] == KIMI and config["toolsets"] == ["hermes-cli"] and config["provider"] == "auto"
    assert config["memory"] == {"memory_enabled": False, "user_profile_enabled": False}
    assert config["terminal"] == {"backend": "local", "timeout": 180} and "max_turns" not in json.dumps(config)
    # Hermes refuses wheel/sdist builds: its pinned source tree comes in as an artifact and installs
    # as its own installer's hash-verified tier does, an editable `uv sync` from its uv.lock.
    source = next(item for item in agent["artifacts"] if item["name"] == "hermes-src")
    assert source["sha256"] == third_party.RUNTIMES["hermes-src"]["sha256"]
    assert source["container_path"] == third_party.HERMES_SRC_HOME
    setup = agent["setup"]["program"]
    assert f"cp -R {third_party.HERMES_SRC_HOME}/hermes-agent-{third_party.HERMES_REVISION} " in setup
    assert f"venv --python {third_party.HERMES_PYTHON} " in setup and "sync --extra all --locked" in setup
    assert "pip install" not in setup.split("sync --extra all --locked")[0]
    assert "hermes-install-tier.txt" in setup and '"$HERMES_HOME/config.yaml"' in setup
    # At 0.20.4 `hermes version` is no subcommand (argparse rejects it); the flag is `--version`.
    assert agent["install_check"].endswith("/bin/hermes --version")


def test_oh_my_pi_runs_its_preinstalled_release_binary_in_print_mode_with_harbors_minimal_custom_provider():
    agent = build("oh-my-pi")
    program = agent["program"]
    assert program.startswith(f"exec {third_party.OH_MY_PI} -p ")
    for flag in ("--mode json", f"--provider {third_party.OH_MY_PI_PROVIDER}", f"--model {KIMI}"):
        assert flag in program
    # Endpoint, key, wire API and model id only: no context or output limit, as Harbor declares for Pi.
    assert json.loads(agent["log_files"]["omp-models.yml"]) == {"providers": {third_party.OH_MY_PI_PROVIDER: {
        "baseUrl": BASE, "apiKey": third_party.RELAY_KEY, "api": "openai-completions", "models": [{"id": KIMI}]}}}
    # The binary arrives in its runtime archive; no package manager runs in the trial.
    assert [(spec["name"], spec["container_path"]) for spec in agent["artifacts"]] == [
        ("omp", third_party.OH_MY_PI_HOME)]
    assert third_party.OH_MY_PI == third_party.OH_MY_PI_HOME + "/" + third_party.RELEASE_FILES["omp"]["member"]
    setup = agent["setup"]["program"]
    assert "npm" not in setup and "bun" not in setup and '"$HOME/.omp/agent/models.yml"' in setup
    assert agent["install_check"].endswith(f"{third_party.OH_MY_PI} --version")


def task_dir(root: Path, name: str = "regex-log") -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
        f'[environment]\ndocker_image = "alexgshaw/{name}:20251031"\ncpus = 1\nmemory_mb = 2048\n', encoding="utf-8")
    (task / "instruction.md").write_text("Write the regex.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8")
    return task


def make_runner(tmp_path: Path) -> runner.Runner:
    config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path, tasks=["regex-log"], arms=sorted(third_party.ARMS),
        protocol="fhe-v1", model=KIMI, relay_url="http://relay:9", files_url="http://relay:9/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://proxy:8080", max_alive_min=120, max_attempts=1, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="smoke", allow_hosts=["pypi.org", "*.npmjs.org"])
    live = runner.Runner.__new__(runner.Runner)
    live.config = config
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.artifacts = runtime_artifacts()
    live.tasks = {"regex-log": harness.load_task(task_dir(tmp_path))}
    return live


def test_runner_gives_third_party_arms_the_chat_face_and_counts_setup_in_the_trial_budget(tmp_path):
    live = make_runner(tmp_path)
    task = live.tasks["regex-log"]
    # kimi-k3's own face is Anthropic (Tofu's route); these arms speak OpenAI chat completions.
    for arm in third_party.ARMS:
        assert live.model_base_url(task, arm) == f"http://relay:9/arm/{arm}-smoke/task/regex-log/v1"
    spec = live.spec_for(task, "kimi-code", 1)
    assert spec["agent"]["kind"] == "kimi-code" and spec["egress"]["allow_hosts"] == ["*.npmjs.org", "pypi.org"]
    assert spec["agent"]["env"]["KIMI_MODEL_BASE_URL"] == "http://relay:9/arm/kimi-code-smoke/task/regex-log/v1"
    assert runner.trial_budget_sec(spec) == (third_party.SETUP_TIMEOUT_SEC + 1200 + 900 * spec["verifier_attempts"]
                                             + runner.TRIAL_OVERHEAD_SEC)
    live.config.source_guard = True
    with pytest.raises(ValueError, match="no source-guard projection"):
        live.agent_for(task, "qwen-code")


def test_vm_trial_setup_runs_before_the_install_check_and_a_failure_is_an_install_error(tmp_path, monkeypatch):
    agent = dict(build("mini-swe-agent"), artifacts=[])
    events = []
    outcome = {"exit_code": 0, "timed_out": False, "duration_s": 3.0}

    def fake_docker(*args, check=True, timeout=None, phase="docker"):
        events.append(("docker", args[0], args[-1]))
        return subprocess.CompletedProcess(["docker", *args], 0, b"mini-swe-agent 2.4.6\n", b"")

    def fake_exec(container, program, env, marker, timeout_sec, stdout_path, stderr_path, workdir=None,
                  completion=None, user=None):
        events.append(("exec", marker, timeout_sec, program == agent["setup"]["program"], env == agent["env"], user))
        Path(stderr_path).write_bytes(b"error: no solution found when resolving dependencies\n")
        return dict(outcome)

    monkeypatch.setattr(vm_trial, "docker", fake_docker)
    monkeypatch.setattr(vm_trial, "exec_with_deadline", fake_exec)

    def trial(key: str) -> vm_trial.Trial:
        spec = {"out_dir": str(tmp_path / key), "task_dir": str(tmp_path / "task"), "trial_key": key,
                "trial_id": key, "task_name": "regex-log", "arm": "mini-swe-agent", "agent": agent}
        live = vm_trial.Trial(spec)
        (live.out / "agent").mkdir(parents=True)
        return live

    ok = trial("ok")
    ok.install_agent()
    # Setup runs as the image's user with the agent's environment, then the install check.
    assert events[0] == ("exec", "tbn-setup-ok", third_party.SETUP_TIMEOUT_SEC, True, True, None)
    assert events[1] == ("docker", "exec", agent["install_check"])
    assert ok.result["phases"]["install"]["setup"]["exit_code"] == 0
    assert (ok.out / "agent" / "instruction.md").read_text() == "Write the regex.\n"
    outcome["exit_code"] = 1
    events.clear()
    failed = trial("failed")
    with pytest.raises(vm_trial.InfraError) as failure:
        failed.install_agent()
    assert failure.value.phase == "install" and "no solution found" in str(failure.value)
    assert [event[0] for event in events] == ["exec"]  # no install check after a failed setup


def test_the_relay_serves_the_chat_url_the_runner_gives_these_arms(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    captured = []
    usage = {"prompt_tokens": 1200, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 1024}}

    async def upstream(request):
        captured.append({"headers": dict(request.headers), "body": await request.json()})
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": usage})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("0733", "upstream-0733")])
    args = SimpleNamespace(
        origin="unused", provider="unused", key_suffixes="0733", files_dir=None,
        log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30, model=KIMI,
        temperature=None, top_p=None, max_tokens=None, reasoning_effort=None, anthropic_model=None,
        responses_model=None, responses_max_output_tokens=None, max_rpm=10, first_body_dir=None,
        anthropic_body_dir=None, preserve_pi_effort_markers=False, stop_file=None)
    live = relay.Relay(args)
    payload = {"model": f"openai/{KIMI}", "messages": [{"role": "user", "content": "Solve it."}]}
    planned = make_runner(tmp_path / "run")
    path = urlsplit(planned.model_base_url(planned.tasks["regex-log"], "goose")).path + "/chat/completions"

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/v1/chat/completions")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                async with client.post(path, json=payload,
                                       headers={"Authorization": f"Bearer {third_party.RELAY_KEY}"}) as response:
                    assert response.status == 200
                    return await response.json()

    assert asyncio.run(exercise())["usage"] == usage
    assert captured[0]["body"]["model"] == KIMI
    assert captured[0]["headers"]["Authorization"] == "Bearer upstream-0733"
    record = json.loads((tmp_path / "requests.jsonl").read_text())
    assert record["kind"] == "chat" and record["arm"] == "goose-smoke" and record["task"] == "regex-log"
    assert record["usage"] == usage and record["key"] == "0733"
    assert "upstream-0733" not in (tmp_path / "requests.jsonl").read_text()


def test_fetch_runtimes_reuses_a_matching_local_copy_and_refuses_a_digest_off_the_pin(tmp_path, monkeypatch):
    data = b"pinned release bytes"
    pin = {"filename": "tool-1.0.tar.gz", "url": "https://example.invalid/tool-1.0.tar.gz",
           "sha256": hashlib.sha256(data).hexdigest(), "version": "tool 1.0"}
    source, files, manifest = tmp_path / "source", tmp_path / "files", tmp_path / "artifacts.json"
    source.mkdir()
    (source / "tool-1.0.tar.gz").write_bytes(data)
    manifest.write_text(json.dumps({"artifacts": {"tofu": {"filename": "t", "sha256": "a" * 64, "version": "t"}}}))
    monkeypatch.setattr(third_party, "RUNTIMES", {"tool": pin})
    monkeypatch.setattr(third_party, "RELEASE_FILES", {})
    monkeypatch.setattr(third_party.urllib.request, "urlopen",
                        lambda *_, **__: pytest.fail("a matching local copy must not be downloaded"))
    document = third_party.fetch_runtimes(files, manifest, [source])
    assert (files / "tool-1.0.tar.gz").read_bytes() == data
    assert document["artifacts"]["tool"] == {"filename": "tool-1.0.tar.gz", "sha256": pin["sha256"],
                                             "version": "tool 1.0"}
    assert set(json.loads(manifest.read_text())["artifacts"]) == {"tofu", "tool"}
    monkeypatch.setattr(third_party, "RUNTIMES", {"tool": dict(pin, filename="tool-2.0.tar.gz")})
    monkeypatch.setattr(third_party.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(b"tampered"))
    with pytest.raises(SystemExit, match="sha256 differs"):
        third_party.fetch_runtimes(files, manifest, [])
    assert not (files / "tool-2.0.tar.gz").exists() and not list(files.glob("*.partial"))


def test_fetch_runtimes_packs_pinned_release_files_repeatably_and_refuses_one_off_the_pin(tmp_path, monkeypatch):
    binary, roots = b"\x7fELF single-file release", b"-----BEGIN CERTIFICATE-----\n"
    releases = {
        "tool": {"asset": "tool-linux-x64", "member": "bin/tool", "mode": 0o755,
                 "url": "https://example.invalid/tool-linux-x64", "file_sha256": hashlib.sha256(binary).hexdigest(),
                 "filename": "tool-1.0-linux-x64.tar.gz", "version": "tool 1.0 release binary"},
        "roots": {"asset": "roots-2026.pem", "member": "roots.pem", "mode": 0o644,
                  "url": "https://example.invalid/roots-2026.pem", "file_sha256": hashlib.sha256(roots).hexdigest(),
                  "filename": "roots-2026.tar.gz", "version": "roots 2026"}}
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool-linux-x64").write_bytes(binary)
    (source / "roots-2026.pem").write_bytes(roots)
    monkeypatch.setattr(third_party, "RUNTIMES", {})
    monkeypatch.setattr(third_party, "RELEASE_FILES", releases)
    monkeypatch.setattr(third_party.urllib.request, "urlopen",
                        lambda *_, **__: pytest.fail("a pinned local file must not be downloaded"))
    first = third_party.fetch_runtimes(tmp_path / "a", tmp_path / "a.json", [source])["artifacts"]
    archive = tmp_path / "a" / "tool-1.0-linux-x64.tar.gz"
    assert first["tool"] == {"filename": "tool-1.0-linux-x64.tar.gz",
                             "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                             "version": f"tool 1.0 release binary (file sha256 {releases['tool']['file_sha256'][:12]})"}
    # The runner unpacks with the "tar" filter (vm_trial.fetch_artifact): each member keeps its mode.
    for name, data in (("tool", binary), ("roots", roots)):
        release = releases[name]
        with tarfile.open(tmp_path / "a" / release["filename"]) as bundle:
            member = bundle.getmember(release["member"])
            assert (member.mode, member.uid, member.gid, member.mtime) == (release["mode"], 0, 0, 0)
            bundle.extractall(tmp_path / "unpacked", filter="tar")
        assert (tmp_path / "unpacked" / release["member"]).read_bytes() == data
    assert os.access(tmp_path / "unpacked" / "bin" / "tool", os.X_OK)
    assert not os.access(tmp_path / "unpacked" / "roots.pem", os.X_OK)
    # One file packs to one archive, and an archive already in place is kept by its member's digest.
    assert third_party.fetch_runtimes(tmp_path / "b", tmp_path / "b.json", [source])["artifacts"] == first
    assert third_party.fetch_runtimes(tmp_path / "a", tmp_path / "a.json", [])["artifacts"] == first
    monkeypatch.setattr(third_party.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(b"tampered"))
    with pytest.raises(SystemExit, match="sha256 differs"):
        third_party.fetch_runtimes(tmp_path / "c", tmp_path / "c.json", [])
    assert not list((tmp_path / "c").iterdir())
