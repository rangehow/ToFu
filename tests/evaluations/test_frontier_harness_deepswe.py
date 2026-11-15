"""DeepSWE (schema 1.3, separate verifier) on the native runner: task contract, collect, verifier, export.

The contract comes from the deep-swe corpus at FHE's reproduction pin
435ee89ec2f2e2289f33b0da4f992f0b7b7266b9:

- the agent works in the task image and commits;
- ``[[verifier.collect]]`` runs in the agent's container and writes ``/logs/artifacts/model.patch``;
- the verifier runs in a separate image built from ``tests/Dockerfile`` (the task image plus the hidden tests), with no network;
- ``tests/test.sh`` writes ``reward.json``, or a ``reward.txt`` of ``-1`` when the grader itself crashes.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness, vm_trial

pytestmark = pytest.mark.unit

BASE = "3f269a72ff69398b1250c584171f32d12c0d8085"
COLLECT = ("cd /app && mkdir -p /logs/artifacts && git config --global --add safe.directory /app && "
           f"git diff --binary {BASE} HEAD > /logs/artifacts/model.patch")


def deepswe_task(root: Path, name: str = "anko-typed-variable-bindings", **overrides) -> Path:
    fields = {"artifacts": '["/logs/artifacts/model.patch"]', "verifier_network": "no-network", "dockerfile": True}
    fields.update(overrides)
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir()
    (task / "solution").mkdir()
    (task / "task.toml").write_text(f'''schema_version = "1.3"
artifacts = {fields["artifacts"]}
[task]
name = "datacurve/{name}"
[metadata]
base_commit_hash = "{BASE}"
[verifier]
network_mode = "{fields["verifier_network"]}"
environment_mode = "separate"
timeout_sec = 1800.0
[verifier.env]
GOFLAGS = "-mod=mod"
[verifier.environment]
build_timeout_sec = 1800.0
cpus = 2
memory_mb = 8192
storage_mb = 20480
[[verifier.collect]]
command = "{COLLECT}"
timeout_sec = 300.0
[agent]
network_mode = "no-network"
timeout_sec = 5400.0
[environment]
build_timeout_sec = 1800.0
docker_image = "public.ecr.aws/d3j8x8q7/swe-bench-202605:kh79betfed7ets4an20cr4j57182y9wt-v1.1"
cpus = 2
memory_mb = 8192
storage_mb = 20480
''', encoding="utf-8")
    (task / "instruction.md").write_text("Add typed bindings.\n\nIMPORTANT: commit everything when you are done.\n")
    (task / "tests" / "test.sh").write_text("python3 /tests/grader.py prepare\n")
    if fields["dockerfile"]:
        (task / "tests" / "Dockerfile").write_text("FROM public.ecr.aws/d3j8x8q7/swe-bench-202605:x\nCOPY test.sh /tests/test.sh\n")
    (task / "solution" / "solution.patch").write_text("diff --git a/x b/x\n")
    return task


def artifact(name: str) -> harness.Artifact:
    return harness.Artifact(name=name, filename=f"{name}.tar.gz", sha256="c" * 64, version=f"{name} 1")


def deepswe_spec(tmp_path: Path, allow_hosts=("pypi.org",)) -> tuple[harness.Task, dict]:
    task = harness.load_task(deepswe_task(tmp_path))
    protocol = harness.PROTOCOLS["fhe-v1"]
    agent = harness.codex_agent(artifact=artifact("codex"), files_url="http://r/files",
                                model_base_url="http://r/arm/codex-x/task/anko/v1", model="kimi-k3",
                                instruction=task.instruction)
    spec = harness.build_trial_spec(task=task, arm="codex", attempt=1, agent=agent, protocol=protocol,
                                    agent_timeout_sec=harness.agent_timeout_for(task, protocol),
                                    egress_upstream="http://proxy:8080", allow_hosts=list(allow_hosts))
    return task, spec


def test_deepswe_task_carries_the_separate_verifier_contract_into_the_trial_spec(tmp_path):
    task, spec = deepswe_spec(tmp_path)
    assert task.verifier_mode == "separate" and task.agent_network_mode == "no-network"
    assert spec["agent_timeout_sec"] == 5400 and spec["verifier_timeout_sec"] == 1800.0
    assert spec["cpus"] == 2 and spec["memory"] == "8192m"
    assert spec["verifier"] == {
        "mode": "separate", "network": "none", "cpus": 2, "memory": "8192m", "build_timeout_sec": 1800.0,
        "collect": [{"command": COLLECT, "timeout_sec": 300.0}], "artifacts": ["/logs/artifacts/model.patch"]}
    assert spec["verifier_env"] == {"GOFLAGS": "-mod=mod"}
    # A no-network agent reaches only the model relay: the run allowlist does not widen it.
    assert spec["egress"]["allow_hosts"] == []
    json.dumps(spec)


def test_shared_verifier_tasks_keep_their_spec(tmp_path):
    task_dir = tmp_path / "regex-log"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
        '[environment]\ndocker_image = "a/regex-log:1"\ncpus = 1\nmemory_mb = 2048\nallow_internet = false\n')
    (task_dir / "instruction.md").write_text("x\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    task = harness.load_task(task_dir)
    agent = harness.codex_agent(artifact=artifact("codex"), files_url="http://r/files",
                                model_base_url="http://r/v1", model="kimi-k3", instruction="x")
    spec = harness.build_trial_spec(task=task, arm="codex", attempt=1, agent=agent,
                                    protocol=harness.PROTOCOLS["fhe-v1"], agent_timeout_sec=1200,
                                    egress_upstream=None, allow_hosts=["pypi.org"])
    assert task.verifier_mode == "shared" and "verifier" not in spec
    assert spec["egress"]["allow_hosts"] == ["pypi.org"]


@pytest.mark.parametrize("overrides, message", [
    ({"artifacts": '["/workspace/model.patch"]'}, "under /logs/artifacts"),
    ({"verifier_network": "public"}, "no-network"),
    ({"dockerfile": False}, "tests/Dockerfile"),
])
def test_unsupported_separate_verifier_shapes_are_refused_at_load(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        harness.load_task(deepswe_task(tmp_path, **overrides))


class FakeDocker:
    """Records docker argv; ``cp`` out of a container writes the collected patch."""

    def __init__(self, patch: bytes | None = b"diff --git a/vm/vm.go b/vm/vm.go\n+typed\n"):
        self.calls: list[tuple[str, ...]] = []
        self.patch = patch

    def __call__(self, *args, check=True, timeout=None, phase="docker"):
        self.calls.append(tuple(args))
        if args[0] == "cp" and ":" in args[1] and self.patch is not None:
            Path(args[2]).write_bytes(self.patch)
        return subprocess.CompletedProcess(["docker", *args], 0, b"", b"")


def trial_for(tmp_path: Path, monkeypatch, docker: FakeDocker, reward_files: dict[str, str]):
    task, spec = deepswe_spec(tmp_path)
    spec = dict(spec, out_dir=str(tmp_path / "out"), task_dir=str(task.path), trial_key="anko-codex-a1")
    trial = vm_trial.Trial(spec)
    trial.out.mkdir(parents=True)
    trial.proxy = type("Proxy", (), {"phase": "agent"})()
    trial.result["container_home"] = "/root"
    execs = []

    def fake_exec(container, program, env, marker, timeout_sec, stdout_path, stderr_path, workdir=None,
                  completion=None, user=None):
        execs.append({"container": container, "program": program, "env": env, "timeout": timeout_sec,
                      "user": user})
        if "test.sh" in program:
            for name, text in reward_files.items():
                (trial.out / "verifier" / name).write_text(text)
        return {"exit_code": 0, "timed_out": False, "duration_s": 1.0}

    sealed = []
    monkeypatch.setattr(vm_trial, "docker", docker)
    monkeypatch.setattr(vm_trial, "exec_with_deadline", fake_exec)
    monkeypatch.setattr(vm_trial, "seal_verifier_logs",
                        lambda path: (sealed.append(path), path.mkdir(parents=True, exist_ok=True)))
    return trial, execs, sealed


def test_collect_then_a_fresh_offline_verifier_image_grades_the_collected_patch(tmp_path, monkeypatch):
    docker = FakeDocker()
    trial, execs, sealed = trial_for(tmp_path, monkeypatch, docker,
                                     {"reward.json": '{"reward": 1, "f2p_passed": 9, "apply_failed": 0}'})
    trial.run_verifier()
    agent_container, verifier_tag = trial.container, "tbn-verifier-anko-codex-a1"
    # 1. Anything the agent left at an artifact path is removed, then the task's collect command runs as root.
    assert docker.calls[0] == ("exec", "-u", "0", agent_container, "rm", "-rf", "/logs/artifacts/model.patch")
    assert execs[0] == {"container": agent_container, "program": COLLECT, "env": {}, "timeout": 300.0, "user": "0"}
    # 2. The artifact leaves the agent container, which is then removed before any verification starts.
    cp = next(call for call in docker.calls if call[0] == "cp")
    assert cp[1] == f"{agent_container}:/logs/artifacts/model.patch"
    assert Path(cp[2]) == trial.out / "artifacts" / "model.patch"
    assert docker.calls.index(("rm", "-f", agent_container)) > docker.calls.index(cp)
    # 3. The verifier image is the task's tests/Dockerfile, built without network.
    build = next(call for call in docker.calls if call[0] == "build")
    assert build == ("build", "--network", "none", "-t", verifier_tag, str(trial.task_dir / "tests"))
    # 4. A new container runs it with no network, the verifier's resources and env, the artifacts read-only.
    run = next(call for call in docker.calls if call[0] == "run")
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--cpus") + 1] == "2" and run[run.index("--memory") + 1] == "8192m"
    assert f"{trial.out / 'verifier'}:/logs/verifier" in run
    assert f"{trial.out / 'artifacts'}:/logs/artifacts:ro" in run
    assert "GOFLAGS=-mod=mod" in run and verifier_tag in run
    grade = execs[1]
    assert grade["container"] != agent_container and grade["user"] == "0" and grade["timeout"] == 1800.0
    assert "/tests/test.sh" in grade["program"]
    assert sealed == [trial.out / "verifier"]
    collect = trial.result["phases"]["collect"]
    assert collect["artifacts"]["/logs/artifacts/model.patch"]["bytes"] == len(docker.patch)
    assert len(collect["artifacts"]["/logs/artifacts/model.patch"]["sha256"]) == 64
    assert trial.result["reward"] == 1.0 and trial.result["phases"]["verifier"]["reward_source"] == "reward.json"


def test_an_unapplied_patch_is_a_scored_zero_and_the_crash_sentinel_is_unscored(tmp_path, monkeypatch):
    trial, _, _ = trial_for(tmp_path / "a", monkeypatch, FakeDocker(),
                            {"reward.json": '{"reward": 0, "apply_failed": 1}'})
    trial.run_verifier()
    assert trial.result["reward"] == 0.0
    crashed, _, _ = trial_for(tmp_path / "b", monkeypatch, FakeDocker(), {"reward.txt": "-1\n"})
    crashed.run_verifier()
    verifier = crashed.result["phases"]["verifier"]
    assert crashed.result["reward"] is None
    assert verifier["raw_reward"] == -1.0 and verifier["reward_sentinel"] == "verifier_crash"


def test_a_missing_artifact_is_recorded_and_still_graded(tmp_path, monkeypatch):
    trial, execs, _ = trial_for(tmp_path, monkeypatch, FakeDocker(patch=None),
                                {"reward.json": '{"reward": 0}'})
    trial.run_verifier()
    entry = trial.result["phases"]["collect"]["artifacts"]["/logs/artifacts/model.patch"]
    assert entry["copied"] is False and entry["bytes"] is None
    assert trial.result["reward"] == 0.0 and "/tests/test.sh" in execs[-1]["program"]


def test_teardown_removes_the_verifier_container_and_image(tmp_path, monkeypatch):
    docker = FakeDocker()
    trial, _, _ = trial_for(tmp_path, monkeypatch, docker, {"reward.json": '{"reward": 1}'})
    trial.run_verifier()
    trial.teardown()
    assert ("rm", "-f", "tbn-vrf-anko-codex-a1") in docker.calls
    assert ("rmi", "-f", "tbn-verifier-anko-codex-a1") in docker.calls


def git_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pin"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_prepare_tasks_takes_deepswe_tasks_whole_from_the_corpus_pin(tmp_path, monkeypatch):
    pytest.importorskip("dirhash")
    from evaluations.frontier_harness import tasks
    from evaluations.terminal_bench.__main__ import verify_task_identity

    corpus_task = deepswe_task(tmp_path / "corpus-src")
    corpus_files = {f"tasks/{corpus_task.name}/{path.relative_to(corpus_task)}": path.read_text()
                    for path in corpus_task.rglob("*") if path.is_file()}
    published_toml = (corpus_task / "task.toml").read_text().replace('schema_version = "1.3"', 'schema_version = "1.1"')
    fhe_head = git_repo(tmp_path / "fhe", {
        "benchmark.json": json.dumps({"name": "FrontierHarness Eval v1", "task_ids": [
            "datacurve/anko-typed-variable-bindings", "terminal-bench/regex-log"]}),
        "tasks/regex-log/task.toml": '[agent]\ntimeout_sec = 1200.0\n[verifier]\ntimeout_sec = 900.0\n'
                                     '[environment]\ndocker_image = "a/regex-log:1"\ncpus = 1\nmemory_mb = 2048\n',
        "tasks/regex-log/instruction.md": "Published instruction.\n",
        "tasks/anko-typed-variable-bindings/task.toml": published_toml,
        "tasks/anko-typed-variable-bindings/instruction.md": "Add typed bindings.\n",
        "skills/frontierharness-eval/scripts/providers.sh": " ".join(tasks.FHE_V1_ALLOWED_HOSTS),
    })
    tb2_head = git_repo(tmp_path / "tb2", {"regex-log/task.toml": "", "regex-log/instruction.md": "",
                                           "regex-log/tests/test.sh": "true\n"})
    deep_head = git_repo(tmp_path / "deep-swe", corpus_files)
    monkeypatch.setattr(tasks, "FHE_COMMIT", fhe_head)
    monkeypatch.setattr(tasks, "TB2_COMMIT", tb2_head)
    monkeypatch.setattr(tasks, "DEEPSWE_COMMIT", deep_head)
    out = tmp_path / "out"
    manifest = tasks.prepare_tasks(tmp_path / "fhe", tmp_path / "tb2", out, deep_swe_repo=tmp_path / "deep-swe")
    entry = manifest["tasks"]["anko-typed-variable-bindings"]
    assert entry["id"] == "datacurve/anko-typed-variable-bindings" and entry["source"].startswith("deep-swe@")
    assert entry["published_definition_matches"] is False
    assert manifest["deep_swe_commit"] == deep_head and manifest["dataset_commit"].endswith(deep_head[:12])
    copied = harness.load_task(out / "anko-typed-variable-bindings")
    assert copied.verifier_mode == "separate" and copied.agent_timeout_sec == 5400.0
    assert (out / "anko-typed-variable-bindings/tests/Dockerfile").is_file()
    identity = verify_task_identity(out, sorted(manifest["tasks"]), out / "task_checksums.json")
    assert identity["mismatched"] == []
    with pytest.raises(ValueError, match="datacurve"):
        tasks.prepare_tasks(tmp_path / "fhe", tmp_path / "tb2", tmp_path / "no-corpus")


def test_export_maps_deepswe_trials_to_their_fhe_ids_with_patch_evidence(tmp_path):
    from evaluations.frontier_harness import export

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fhe-task-manifest.json").write_text(json.dumps({"tasks": {
        "anko-typed-variable-bindings": {"id": "datacurve/anko-typed-variable-bindings"}}}))
    native = tmp_path / "native"
    attempt = native / "trials" / "codex" / "anko-typed-variable-bindings" / "attempt-1"
    attempt.mkdir(parents=True)
    (native / "run.json").write_text(json.dumps({
        "run_label": "smoke", "arms": ["codex"], "tasks": ["anko-typed-variable-bindings"],
        "tasks_dir": str(tasks_dir), "allow_hosts": ["pypi.org"], "task_identity": {"dataset_commit": "x"}}))
    (attempt / "state.json").write_text(json.dumps({"submitted_epoch": 1000.0, "phase": "no_reward"}))
    (attempt / "result.json").write_text(json.dumps({
        "outcome": "no_reward", "reward": None, "finished_at": 4000.0,
        "phases": {"prepare": {"started": 1000.0}, "agent": {"started": 1010.0, "exit_code": 0, "timed_out": False},
                   "collect": {"artifacts": {"/logs/artifacts/model.patch": {
                       "copied": True, "bytes": 120, "sha256": "d" * 64}}},
                   "verifier": {"raw_reward": -1.0, "reward_sentinel": "verifier_crash"}}}))
    log = tmp_path / "relay.jsonl"
    log.write_text(json.dumps({"kind": "responses", "arm": "codex-smoke", "task": "anko-typed-variable-bindings",
                               "status": 200, "ts": 1100.0, "key": "0733",
                               "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n")
    repo = tmp_path / "fhe"
    (repo / "skills/frontierharness-eval/scripts").mkdir(parents=True)
    (repo / "skills/frontierharness-eval/scripts/pricing.json").write_text(json.dumps({
        "version": "kimi-k3-2026-08-20", "unit_tokens": 1000000,
        "models": {"k3": {"fresh_input": 3.0, "cache_write": 3.0, "cache_read": 0.3, "output": 15.0}}}))
    export.export(native_dir=native, relay_log=log, fhe_repo=repo, out_root=tmp_path / "fhe-runs",
                  label="smoke-deepswe", run_id="smoke-deepswe", provider="p", run_normalizer=False)
    trial = json.loads((tmp_path / "fhe-runs/codex/trials/anko-typed-variable-bindings__a1/trial.json").read_text())
    assert trial["id"] == "datacurve/anko-typed-variable-bindings" and trial["suite"] == "datacurve"
    assert trial["status"] == "infra_invalid" and "verifier_crash" in trial["status_reason"]
    assert trial["model_patch"] == {"copied": True, "bytes": 120, "sha256": "d" * 64}


def test_trial_budget_counts_collect_and_build_and_a_shorter_session_is_refused(tmp_path):
    from evaluations.terminal_bench import runner

    task, spec = deepswe_spec(tmp_path)
    assert runner.trial_budget_sec(spec) == 5400 + 1800 + 1800 + 300 + runner.TRIAL_OVERHEAD_SEC
    live = runner.Runner.__new__(runner.Runner)
    live.config = runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path, tasks=[task.name], arms=["codex"], protocol="fhe-v1",
        model="kimi-k3", relay_url="http://relay:9", files_url="http://relay:9/files",
        artifacts_manifest=tmp_path / "artifacts.json", endpoint="https://g/system/857", image="rootfs://x",
        branch="b", proxy="http://proxy:8080", max_alive_min=180, max_attempts=1, context_tokens=1_000_000,
        poll_interval_sec=1, run_label="smoke", allow_hosts=["pypi.org"])
    live.protocol = harness.PROTOCOLS["fhe-v1"]
    live.artifacts = {"codex": artifact("codex")}
    live.tasks = {task.name: task}
    with pytest.raises(ValueError, match=r"max-alive-min 180 .*anko-typed-variable-bindings/codex \(215 min\)"):
        live.run(1)


def corpus_checkout() -> Path:
    import os

    checkout = os.environ.get("DEEPSWE_CORPUS_CHECKOUT")
    if not checkout or not (Path(checkout) / "tasks").is_dir():
        pytest.skip("needs DEEPSWE_CORPUS_CHECKOUT (datacurve-ai/deep-swe at the FHE corpus pin)")
    return Path(checkout)


FHE_V1_DEEPSWE = ("anko-typed-variable-bindings", "arktype-json-schema-refs-dependencies", "expr-try-catch-errors",
                  "fastapi-deprecation-response-headers", "httpx-multipart-response-parsing",
                  "katex-multicolumn-array-spans", "meriyah-explicit-resource-declarations",
                  "python-statemachine-state-data-scoping", "scc-bounded-memory-spilling")


def test_every_v1_corpus_task_loads_as_the_contract_vm_trial_runs():
    import hashlib

    corpus = corpus_checkout()
    graders = set()
    for name in FHE_V1_DEEPSWE:
        task = harness.load_task(corpus / "tasks" / name)
        contract = task.separate_verifier
        assert task.verifier_mode == "separate" and task.agent_network_mode == "no-network"
        assert task.agent_timeout_sec == 5400.0 and task.verifier_timeout_sec == 1800.0
        assert contract["artifacts"] == ["/logs/artifacts/model.patch"] and contract["network"] == "none"
        [collect] = contract["collect"]
        assert collect["command"].endswith(" HEAD > /logs/artifacts/model.patch") and "git diff --binary" in collect["command"]
        test_sh = (task.path / "tests" / "test.sh").read_text()
        # The crash sentinel vm_trial reads as "no reward": the shared EXIT trap of every task.
        assert "echo -1 > /logs/verifier/reward.txt" in test_sh and "python3 /tests/grader.py prepare" in test_sh
        graders.add(hashlib.sha256((task.path / "tests" / "grader.py").read_bytes()).hexdigest())
    assert len(graders) == 1, "the corpus grader is shared verbatim by every task"


# The grader applies test.patch with ``git apply --allow-empty`` (git >= 2.35, as in the
# task images). The flag only changes how an *empty* patch is treated, so for these
# non-empty fixtures a shim that drops it lets an older host git run the same grader.
TEST_PATCH = ("diff --git a/vm/typed_test.go b/vm/typed_test.go\nnew file mode 100644\n--- /dev/null\n"
              "+++ b/vm/typed_test.go\n@@ -0,0 +1 @@\n+package vm\n")


def git_without_allow_empty(root: Path) -> Path:
    import shutil

    shim = root / "bin" / "git"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text('#!/bin/bash\nargs=()\nfor a in "$@"; do [ "$a" = --allow-empty ] || args+=("$a"); done\n'
                    f'exec {shutil.which("git")} "${{args[@]}}"\n')
    shim.chmod(0o755)
    return shim.parent


def run_grader(grader: Path, root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PATH": f"{git_without_allow_empty(root)}:/usr/bin:/bin", "HOME": str(root / "home"),
           "GIT_CONFIG_GLOBAL": str(root / "home" / ".gitconfig"),
           "TESTS_DIR": str(root / "tests"), "VERIFIER_DIR": str(root / "out" / "verifier"),
           "APP_DIR": str(root / "app"), "ARTIFACTS_DIR": str(root / "out" / "artifacts")}
    (root / "home").mkdir(exist_ok=True)
    return subprocess.run(["python3", str(grader), *args], env=env, capture_output=True, text=True, timeout=120)


# ``reports_pass``: whether the fail-to-pass test passes in the suite run test.sh would make
# (with no patch the base state is graded, where it fails).
@pytest.mark.parametrize("patch, reports_pass, reward", [("solved", True, 1.0), ("unappliable", True, 0.0),
                                                          ("solved", False, 0.0), ("none", False, 0.0)])
def test_the_corpus_grader_scores_a_collected_patch_as_vm_trial_reads_it(tmp_path, patch, reports_pass, reward):
    grader = corpus_checkout() / "tasks" / FHE_V1_DEEPSWE[0] / "tests" / "grader.py"
    app = tmp_path / "app"
    base = git_repo(app, {"vm/vm.go": "package vm\n// old\n"})
    (tmp_path / "work").mkdir()
    subprocess.run(["git", "clone", "-q", str(app), str(tmp_path / "work" / "repo")], check=True)
    work = tmp_path / "work" / "repo"
    (work / "vm" / "vm.go").write_text("package vm\n// new typed bindings\n")
    subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "solve"],
                   check=True)
    # Exactly what the task's collect command writes, run in the agent's repository.
    diff = subprocess.run(["git", "-C", str(work), "diff", "--binary", base, "HEAD"], check=True,
                          capture_output=True).stdout
    artifacts = tmp_path / "out" / "artifacts"
    artifacts.mkdir(parents=True)
    if patch == "solved":
        (artifacts / "model.patch").write_bytes(diff)
    elif patch == "unappliable":
        (artifacts / "model.patch").write_bytes(diff.replace(b"-// old", b"-// something else"))
    tests = tmp_path / "tests"
    tests.mkdir()
    report = tmp_path / "out" / "verifier" / "new-ctrf.json"
    (tests / "config.json").write_text(json.dumps({
        "base_commit": base, "f2p_node_ids": ["vm.TestTyped"], "p2p_node_ids": ["vm.TestOld"],
        "grade": {"format": "ctrf", "node_id": "suite.name", "tool_label": "fixture", "reports": [str(report)]}}))
    (tests / "test.patch").write_text(TEST_PATCH)
    prepared = run_grader(grader, tmp_path, "prepare")
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    if patch != "unappliable":
        status = "passed" if reports_pass else "failed"
        report.write_text(json.dumps({"results": {"tests": [{"suite": "vm", "name": "TestTyped", "status": status},
                                                            {"suite": "vm", "name": "TestOld", "status": "passed"}]}}))
        graded = run_grader(grader, tmp_path, "grade")
        assert graded.returncode == 0, graded.stdout + graded.stderr
    trial = vm_trial.Trial({"out_dir": str(tmp_path / "out"), "task_dir": str(tmp_path / "task"), "trial_key": "k",
                            "trial_id": "t", "task_name": FHE_V1_DEEPSWE[0], "arm": "codex",
                            "agent": {"kind": "codex"}})
    value, source = trial.read_reward()
    assert (value, source) == (reward, "reward.json")
    if patch == "unappliable":
        assert json.loads((tmp_path / "out" / "verifier" / "reward.json").read_text())["apply_failed"] == 1
    if patch == "solved":
        assert (app / "vm" / "vm.go").read_text() == "package vm\n// new typed bindings\n"
    if patch == "none":
        assert (app / "vm" / "vm.go").read_text() == "package vm\n// old\n"
    if patch != "unappliable":
        assert (app / "vm" / "typed_test.go").read_text() == "package vm\n"
