"""FrontierHarness Eval v1 task identity for the native runner.

Responsibility: materialize the v1 tasks as runner task directories and pin
them.

Terminal-Bench (21 tasks): tests, environment and solution come from the
legacy Harbor registry pin of ``terminal-bench@2.0`` (the dataset FHE's
``run-trials.sh`` runs); ``task.toml`` and ``instruction.md`` come from the FHE
repository, whose published copies differ from every upstream revision
(``allow_internet``, two 1200 s agent budgets, PATH/workdir env, an offline
pyknotid bundle in build-cython-ext's instruction). The published copies win
because they are the frozen v1 definition the leaderboard cites.

DeepSWE (9 tasks, suite ``datacurve``): the whole task directory comes from the
deep-swe corpus at FHE's reproduction pin, because FHE documents that "DeepSWE
uses the pinned corpus metadata" (``reference.md``, "Task metadata"). Three
published copies are an older schema 1.1 definition (no artifact collection,
images without the ``-v1.1`` tag, no instruction to commit) that the corpus's
grader cannot score.

The manifest records every source digest so both choices stay auditable.

Entry points: ``prepare_tasks``, ``FHE_V1_ALLOWED_HOSTS``.
Dependencies: git (read-only on every checkout), ``dirhash``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

FHE_REPOSITORY = "https://github.com/frontier-harness-eval/eval"
FHE_COMMIT = "e837a70bd6beb4e72eeeda62dd06e3bd34f6cb63"
TB2_REPOSITORY = "https://github.com/laude-institute/terminal-bench-2"
# harbor registry.json: terminal-bench 2.0 -> this commit for every task.
TB2_COMMIT = "69671fbaac6d67a7ef0dfec016cc38a64ef7a77c"
SUITE = "terminal-bench"
DEEPSWE_REPOSITORY = "https://github.com/datacurve-ai/deep-swe"
# FHE e837a70 SKILL.md "Benchmark stack": the corpus commit FHE reproductions install
# (the published v1.1 label is not a Git tag).
DEEPSWE_COMMIT = "435ee89ec2f2e2289f33b0da4f992f0b7b7266b9"
DEEPSWE_SUITE = "datacurve"
# FHE e837a70 skills/frontierharness-eval/scripts/providers.sh provider_egress_policy,
# without the provider host: model calls go to the relay, which no trial proxy carries.
FHE_V1_ALLOWED_HOSTS = (
    "astral.sh", "*.astral.sh", "github.com", "*.github.com", "*.githubusercontent.com",
    "*.supabase.co", "pypi.org", "*.pythonhosted.org", "*.npmjs.org", "*.ubuntu.com",
    "*.debian.org", "*.pytorch.org", "*.ecr.aws", "*.cloudfront.net",
)
# Agent-visible inputs the published definition names but no upstream task provides.
PENDING_REPAIRS = {
    "build-cython-ext": "instruction names /app/pyknotid.bundle (FHE offline repair); "
                        "no terminal-bench-2 revision or image creates it",
}
# build-cython-ext's "exact upstream 0.5.3 source ... offline as /app/pyknotid.bundle": the pins
# FHE e837a70 skills/frontierharness-eval/isolated-evaluation-repairs.md records for that source.
PYKNOTID_REPOSITORY = "https://github.com/SPOCKnots/pyknotid.git"
PYKNOTID_TAG = "0.5.3"
PYKNOTID_COMMIT = "441c807dbec2ee32e1da572e24e58d52a4eb7afa"
PYKNOTID_TESTS_TREE = "25307f3fc7c3c1688e7ad36f8b435b41e20e610e"
PYKNOTID_BUNDLE = "environment/agent-inputs/pyknotid.bundle"
PYKNOTID_TARGET = "/app/pyknotid.bundle"


def write_pyknotid_bundle(pyknotid_repo: Path, task_dir: Path) -> dict:
    """Bundle tag 0.5.3 of a pyknotid clone into ``task_dir`` and declare it an agent input.

    The clone must resolve the tag to the pinned commit and tests tree; the
    bundle holds only ``refs/tags/0.5.3``, so ``git clone`` then ``git checkout
    0.5.3`` (the instruction's steps) yields exactly that source.
    """
    commit = _git(pyknotid_repo, "rev-parse", f"refs/tags/{PYKNOTID_TAG}^{{commit}}")
    tests = _git(pyknotid_repo, "rev-parse", f"{PYKNOTID_COMMIT}:tests") if commit == PYKNOTID_COMMIT else None
    if commit != PYKNOTID_COMMIT or tests != PYKNOTID_TESTS_TREE:
        raise ValueError(f"{pyknotid_repo}: tag {PYKNOTID_TAG} is {commit} with tests tree {tests}, expected "
                         f"{PYKNOTID_COMMIT} / {PYKNOTID_TESTS_TREE}")
    bundle = (task_dir / PYKNOTID_BUNDLE).resolve()  # git runs with -C <pyknotid_repo>
    bundle.parent.mkdir(parents=True, exist_ok=True)
    _git(pyknotid_repo, "bundle", "create", str(bundle), f"refs/tags/{PYKNOTID_TAG}")
    digest = _sha256(bundle)
    (task_dir / "agent-inputs.json").write_text(json.dumps(
        [{"source": PYKNOTID_BUNDLE, "target": PYKNOTID_TARGET, "sha256": digest}], indent=2) + "\n",
        encoding="utf-8")
    return {"repair": "agent input /app/pyknotid.bundle", "source": PYKNOTID_REPOSITORY, "tag": PYKNOTID_TAG,
            "commit": PYKNOTID_COMMIT, "tests_tree": PYKNOTID_TESTS_TREE, "bundle_sha256": digest}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_allowlist_source(fhe_repo: Path) -> None:
    """Every allowlisted host must appear verbatim in the pinned providers.sh."""
    source = (Path(fhe_repo) / "skills/frontierharness-eval/scripts/providers.sh").read_text(encoding="utf-8")
    missing = [host for host in FHE_V1_ALLOWED_HOSTS if host not in source]
    if missing:
        raise ValueError(f"allowlist hosts absent from providers.sh: {missing}")


def _check_pin(repo: Path, expected: str) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    if head != expected:
        raise ValueError(f"{repo} is at {head}, expected {expected}")
    if _git(repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"{repo} has modified tracked files")


def _same_definition(published: Path, source: Path) -> bool:
    """Whether FHE's published task.toml/instruction.md say what the corpus task says."""
    def text(path: Path) -> str:
        return "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").strip().splitlines())

    return (tomllib.loads((published / "task.toml").read_text(encoding="utf-8"))
            == tomllib.loads((source / "task.toml").read_text(encoding="utf-8"))
            and text(published / "instruction.md") == text(source / "instruction.md"))


def prepare_tasks(fhe_repo: Path, tb2_repo: Path, out_dir: Path, names: list[str] | None = None,
                  deep_swe_repo: Path | None = None, pyknotid_repo: Path | None = None) -> dict:
    """Write one runner task directory per selected v1 task (default: all), plus their pins.

    DeepSWE tasks need ``deep_swe_repo``, a checkout of the corpus at
    ``DEEPSWE_COMMIT``; ``pyknotid_repo`` (a pyknotid clone with tag 0.5.3)
    supplies build-cython-ext's offline bundle, otherwise that task stays a
    pending repair. ``out_dir`` must be new or empty. Returns the manifest
    that is also written to ``out_dir/fhe-task-manifest.json``;
    ``out_dir/task_checksums.json`` is the ``--task-checksums`` file the native
    runner verifies before any trial.
    """
    from dirhash import dirhash

    fhe_repo, tb2_repo, out_dir = Path(fhe_repo), Path(tb2_repo), Path(out_dir)
    _check_pin(fhe_repo, FHE_COMMIT)
    _check_pin(tb2_repo, TB2_COMMIT)
    check_allowlist_source(fhe_repo)
    benchmark = json.loads((fhe_repo / "benchmark.json").read_text(encoding="utf-8"))
    suites = {task_id.split("/", 1)[1]: task_id.split("/", 1)[0] for task_id in benchmark["task_ids"]}
    selected = sorted(names) if names else sorted(suites)
    unknown = sorted(set(selected) - set(suites))
    if unknown:
        raise ValueError(f"not v1 tasks: {unknown}")
    unsupported = sorted(name for name in selected if suites[name] not in (SUITE, DEEPSWE_SUITE))
    if unsupported:
        raise ValueError(f"no task source for suite of {unsupported}")
    deep = [name for name in selected if suites[name] == DEEPSWE_SUITE]
    if deep:
        if deep_swe_repo is None:
            raise ValueError(f"{DEEPSWE_SUITE} tasks need the deep-swe corpus at {DEEPSWE_COMMIT} "
                             f"(deep_swe_repo): {deep}")
        deep_swe_repo = Path(deep_swe_repo)
        _check_pin(deep_swe_repo, DEEPSWE_COMMIT)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} is not empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = {}
    for name in selected:
        target = out_dir / name
        published = fhe_repo / "tasks" / name
        if suites[name] == DEEPSWE_SUITE:
            source = deep_swe_repo / "tasks" / name
            shutil.copytree(source, target)
            row = {"source": f"deep-swe@{DEEPSWE_COMMIT[:12]}",
                   "deep_swe_tree": _git(deep_swe_repo, "rev-parse", f"HEAD:tasks/{name}"),
                   "published_definition_matches": _same_definition(published, source)}
        else:
            shutil.copytree(tb2_repo / name, target, ignore=shutil.ignore_patterns("task.toml", "instruction.md"))
            for filename in ("task.toml", "instruction.md"):
                shutil.copyfile(published / filename, target / filename)
            row = {"source": f"fhe@{FHE_COMMIT[:12]}+tb2@{TB2_COMMIT[:12]}",
                   "tb2_tree": _git(tb2_repo, "rev-parse", f"HEAD:{name}")}
            if name == "build-cython-ext" and pyknotid_repo is not None:
                row["repair"] = write_pyknotid_bundle(Path(pyknotid_repo), target)
        tasks[name] = {
            "id": f"{suites[name]}/{name}", **row,
            "fhe_task_toml_sha256": _sha256(published / "task.toml"),
            "fhe_instruction_sha256": _sha256(published / "instruction.md"),
            "checksum": dirhash(target, "sha256"),
        }
    identity = f"fhe-v1@{FHE_COMMIT[:12]}+tb2@{TB2_COMMIT[:12]}"
    if deep:
        identity += f"+deep-swe@{DEEPSWE_COMMIT[:12]}"
    manifest = {
        "benchmark": benchmark["name"], "fhe_repository": FHE_REPOSITORY, "fhe_commit": FHE_COMMIT,
        "tb2_repository": TB2_REPOSITORY, "tb2_commit": TB2_COMMIT,
        "deep_swe_repository": DEEPSWE_REPOSITORY, "deep_swe_commit": DEEPSWE_COMMIT if deep else None,
        "dataset_commit": identity, "allowed_hosts": sorted(FHE_V1_ALLOWED_HOSTS),
        "pending_repairs": {name: note for name, note in PENDING_REPAIRS.items()
                            if name in tasks and "repair" not in tasks[name]},
        "tasks": tasks,
    }
    (out_dir / "fhe-task-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                    encoding="utf-8")
    pins = {"dataset_commit": identity, "task_checksums": {name: row["checksum"] for name, row in tasks.items()}}
    (out_dir / "task_checksums.json").write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "allowlist.json").write_text(json.dumps(sorted(FHE_V1_ALLOWED_HOSTS), indent=2) + "\n",
                                            encoding="utf-8")
    return manifest
