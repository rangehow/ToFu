"""Protocol amendment 2026-11-12: a task image without a system CA store gets the pinned bundle, for every arm."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness, third_party, vm_trial

pytestmark = pytest.mark.unit

BUNDLE = harness.Artifact(name=harness.SYSTEM_CA_BUNDLE_ARTIFACT, filename="mozilla-ca-2026-11-05.tar.gz",
                          sha256="c" * 64, version="Mozilla CA bundle 2026-11-05")
TARGET = "/etc/ssl/certs/ca-certificates.crt"


def minimal_task(root: Path) -> harness.Task:
    task = root / "regex-log"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
        '[environment]\ndocker_image = "alexgshaw/regex-log:20251031"\ncpus = 1\nmemory_mb = 2048\n', encoding="utf-8")
    (task / "instruction.md").write_text("Write the regex.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8")
    return harness.load_task(task)


def trial_spec(tmp_path: Path, *, bundle: bool, agent: dict | None = None) -> dict:
    spec = harness.build_trial_spec(
        task=minimal_task(tmp_path), arm="goose", attempt=1, agent={"kind": "goose", **(agent or {})},
        protocol=harness.PROTOCOLS["fhe-v1"],
        agent_timeout_sec=1200, egress_upstream=None,
        system_ca_bundle=BUNDLE.spec("http://relay:9/files", TARGET) if bundle else None)
    return dict(spec, out_dir=str(tmp_path / "out"))


def test_the_trial_spec_names_the_pinned_bundle_where_it_goes_and_what_counts_as_a_system_store(tmp_path):
    assert "system_ca_bundle" not in trial_spec(tmp_path / "a", bundle=False)
    spec = trial_spec(tmp_path / "b", bundle=True)
    assert spec["system_ca_bundle"] == {**BUNDLE.spec("http://relay:9/files", TARGET), "member": "cacert.pem",
                                        "target": TARGET, "probe": list(harness.SYSTEM_CA_BUNDLE_FILES)}
    # The archive fetch-runtimes packs is the artifact, and its member the file, the runner installs.
    release = third_party.RELEASE_FILES[harness.SYSTEM_CA_BUNDLE_ARTIFACT]
    assert (release["member"], release["mode"]) == (harness.SYSTEM_CA_BUNDLE_MEMBER, 0o644)


def test_a_run_refuses_an_artifacts_manifest_without_the_bundle():
    with pytest.raises(SystemExit, match="'ca-bundle'"):
        harness.require_system_ca_bundle({})
    assert harness.require_system_ca_bundle({BUNDLE.name: BUNDLE}) is BUNDLE


def fake_docker(calls: list, probe_answer: bytes | None):
    """``docker`` that records its arguments; the store probe finds ``probe_answer`` (None: no store)."""
    def docker(*args, check=True, timeout=None, phase="docker"):
        calls.append(args)
        if args[4:6] == ("sh", "-c"):
            found = probe_answer is not None
            return subprocess.CompletedProcess(["docker", *args], 0 if found else 1, probe_answer or b"", b"")
        return subprocess.CompletedProcess(["docker", *args], 0, b"", b"")
    return docker


def test_an_image_without_a_system_store_gets_the_pinned_bundle_as_root_before_the_agent_installs(
        tmp_path, monkeypatch):
    calls, fetched = [], []
    root = tmp_path / "cache-root"
    root.mkdir()
    (root / "cacert.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(vm_trial, "docker", fake_docker(calls, None))
    monkeypatch.setattr(vm_trial.Trial, "fetch_artifact",
                        lambda self, artifact: fetched.append(artifact["name"]) or root)
    agent = {"artifacts": [{"name": "goose", "url": "http://relay:9/files/goose.tar.bz2", "sha256": "d" * 64,
                            "container_path": "/var/tmp/tofu-goose"}]}
    trial = vm_trial.Trial(trial_spec(tmp_path, bundle=True, agent=agent))
    (trial.out / "agent").mkdir(parents=True)
    trial.install_agent()
    container = trial.container
    probe, *rest = calls
    assert probe[:6] == ("exec", "-u", "0", container, "sh", "-c")
    assert list(probe[7:]) == ["sh", *harness.SYSTEM_CA_BUNDLE_FILES]
    # Installed as root at the system path, then the agent's own runtime, in that order.
    assert rest[:3] == [("exec", "-u", "0", container, "mkdir", "-p", "/etc/ssl/certs"),
                        ("cp", str(root / "cacert.pem"), f"{container}:{TARGET}"),
                        ("exec", "-u", "0", container, "chmod", "0644", TARGET)]
    assert rest[3][:4] == ("exec", container, "mkdir", "-p") and fetched == ["ca-bundle", "goose"]
    record = {"state": "installed", "image_bundle": None, "sha256": BUNDLE.sha256}
    assert trial.result["system_ca_bundle"] == record
    assert trial.result["phases"]["install"]["system_ca_bundle"] == record


def test_an_image_with_its_own_store_keeps_it_and_nothing_is_fetched(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vm_trial, "docker", fake_docker(calls, b"/etc/ssl/cert.pem\n"))
    monkeypatch.setattr(vm_trial.Trial, "fetch_artifact",
                        lambda self, artifact: pytest.fail("an image with a store must not get the bundle"))
    trial = vm_trial.Trial(trial_spec(tmp_path, bundle=True))
    entry = {}
    trial.provide_system_ca_bundle(entry)
    assert len(calls) == 1
    assert entry["system_ca_bundle"] == {"state": "present", "image_bundle": "/etc/ssl/cert.pem", "sha256": None}


def test_a_spec_without_the_bundle_runs_no_store_step(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vm_trial, "docker", fake_docker(calls, None))
    trial = vm_trial.Trial(trial_spec(tmp_path, bundle=False))
    entry = {}
    trial.provide_system_ca_bundle(entry)
    assert calls == [] and entry == {} and "system_ca_bundle" not in trial.result


def test_the_store_probe_counts_only_a_non_empty_bundle_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vm_trial, "docker", fake_docker(calls, b"x\n"))
    vm_trial.Trial(trial_spec(tmp_path, bundle=True)).provide_system_ca_bundle({})
    script = calls[0][6]
    missing, empty, dangling, present = (str(tmp_path / name) for name in ("missing", "empty", "dangling", "present"))
    Path(empty).write_text("", encoding="utf-8")
    os.symlink(missing, dangling)
    Path(present).write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    def probe(*paths):
        return subprocess.run(["sh", "-c", script, "sh", *paths], capture_output=True, text=True)

    found = probe(missing, empty, dangling, present)
    assert (found.returncode, found.stdout.strip()) == (0, present)
    absent = probe(missing, empty, dangling)
    assert (absent.returncode, absent.stdout) == (1, "")
