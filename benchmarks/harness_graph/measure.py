#!/usr/bin/env python3
"""Run fresh-process ABBA over identical graph algorithms and durable stores.

The Rust binary owns measured regions. This driver preserves every stdout/stderr,
checks exact output/work parity, and records failures instead of dropping samples.
Fixtures are extended from 1 to 32 to 256 branches; their setup never enters read
latency. A reopened process does not imply a cold OS/filesystem cache.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import time


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--histories", default="1000,10000,100000")
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--delta", type=int, default=8)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--branches", default="1,32,256")
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    args = parser.parse_args()
    histories = [int(x) for x in args.histories.split(",")]
    branches = sorted({int(x) for x in args.branches.split(",")})
    if not histories or any(n not in (1000, 10000, 100000) for n in histories):
        parser.error("histories must be drawn from 1000,10000,100000")
    if not branches or any(b not in (1, 32, 256) for b in branches):
        parser.error("branches must be drawn from 1,32,256")
    if args.iterations < 1 or not 0 < args.delta <= 1024 or args.timeout_seconds <= 0:
        parser.error("iterations and delta must be positive and bounded")
    binary = args.binary.resolve(strict=True)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    (root / "fixtures").mkdir()
    source_root = Path(__file__).resolve().parents[2]
    sources = [p for p in (source_root / "benchmarks/harness_graph").rglob("*")
               if p.is_file() and p.suffix in (".rs", ".py", ".toml", ".lock", ".md")]
    sources.append(source_root / "benchmarks/harness_sqlite_reference.rs")
    native_paths = subprocess.check_output(["git", "ls-files", "packages/tofu-db", "packages/tofu-runtime", "contracts", "VERSION"], cwd=source_root, text=True).splitlines()
    sources.extend(source_root / p for p in native_paths)
    sources = sorted(set(sources))
    def source_hashes() -> dict[str, str]:
        return {str(p.relative_to(source_root)): sha(p) for p in sources}
    actual_matrix = [{"history": n, "branches": b} for n in histories for b in ([1] if n == 10000 else branches)]
    metadata = {
        "schema_version": 1, "diagnostic": args.diagnostic,
        "binary": str(binary), "binary_sha256": sha(binary),
        "source_sha256": source_hashes(),
        "source_base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip(),
        "host": platform.platform(), "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "histories": histories, "requested_branches": branches, "actual_matrix": actual_matrix, "delta": args.delta, "iterations": args.iterations,
        "phase_timeout_seconds": args.timeout_seconds,
        "backend_order": ["native", "sqlite", "sqlite", "native"],
        "first_read": "first context after process reopen; OS and filesystem caches uncontrolled",
        "read_control": "synthetic identical-graph compact-header parent chain; not production Turn range scan",
        "cache_comparability": "SQLite explicit 48MiB; native source defaults 32MiB block plus 16MiB page; unequal cache architectures",
        "fixture_policy": "durable base setup once per N/backend, extend branch counts cumulatively; separate from measured reads",
        "runs": [], "failures": [],
    }
    def save() -> None:
        (root / "manifest.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    save()
    responses: dict[tuple, str] = {}
    works: dict[tuple, dict] = {}
    with (root / "records.jsonl").open("w") as all_records:
        def run(backend: str, path: Path, phase: str, history: int, count: int, run_id: str, order: str) -> None:
            command = [str(binary), backend, str(path), phase, str(history), str(count), str(args.delta), str(args.iterations), order, run_id]
            out = root / "raw" / f"{run_id}.jsonl"
            err = root / "raw" / f"{run_id}.stderr"
            start = time.monotonic_ns()
            cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            timeout = False
            with out.open("w") as stdout, err.open("w") as stderr:
                child = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                try:
                    returncode = child.wait(timeout=args.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timeout = True
                    child.kill()
                    returncode = child.wait()
                except BaseException as error:
                    child.kill()
                    returncode = child.wait()
                    metadata["failures"].append({"run_id": run_id, "kind": "interrupted", "error": str(error), "returncode": returncode})
                    save()
                    raise
            cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            report = {"run_id": run_id, "command": command, "pid": child.pid, "returncode": returncode, "timeout": timeout,
                      "child_user_cpu_seconds": cpu_after.ru_utime - cpu_before.ru_utime,
                      "child_system_cpu_seconds": cpu_after.ru_stime - cpu_before.ru_stime,
                      "wall_ns": time.monotonic_ns() - start, "stdout": str(out.relative_to(root)), "stderr": str(err.relative_to(root))}
            metadata["runs"].append(report)
            records = []
            for line in out.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError as error:
                    metadata["failures"].append({"run_id": run_id, "kind": "malformed_record", "error": str(error)})
                    save()
                    raise
                records.append(record)
                all_records.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                if record["phase"] == "context":
                    key = (history, count, record["branch_index"])
                    digest = record["response_sha256"]
                    serialized = json.dumps(record["result"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    if hashlib.sha256(serialized).hexdigest() != digest or len(serialized) != record["response_bytes"]:
                        metadata["failures"].append({"run_id": run_id, "kind": "serialization_digest_mismatch"})
                    if key in responses and responses[key] != digest:
                        metadata["failures"].append({"run_id": run_id, "kind": "response_mismatch", "key": key})
                    responses[key] = digest
                    work_key = key + (record["mode"],)
                    if work_key in works and works[work_key] != record["work"]:
                        metadata["failures"].append({"run_id": run_id, "kind": "logical_work_mismatch", "key": work_key})
                    works[work_key] = record["work"]
            all_records.flush()
            try:
                assert records and records[0]["phase"] == "open" and records[-1]["phase"] == "close"
                assert all(r["pid"] == child.pid and r["run_id"] == run_id and r["backend"] == backend and r["history"] == history and r["branches"] == count and r["delta"] == args.delta for r in records)
                phases = [r["phase"] for r in records]
                assert phases.count("open") == phases.count("close") == 1 and "failure" not in phases
                if phase == "fixture":
                    assert phases == ["open", "fixture_setup", "close"]
                    assert records[1]["durable_commits"] == history + 1 and records[1]["main"]["head"]["depth"] == history
                elif phase == "extend":
                    assert phases.count("fixture_extend") == 1
                    extended = next(r for r in records if r["phase"] == "fixture_extend")
                    prior = extended["prior_branches"]
                    assert extended["durable_commits"] == (count - prior) * (1 + args.delta)
                    forks = [r for r in records if r["phase"] == "fork"]
                    appends = [r for r in records if r["phase"] == "branch_append"]
                    assert len(forks) == count - prior and {r["branch_index"] for r in forks} == set(range(prior, count))
                    assert len(appends) == (count - prior) * args.delta
                    assert {(r["branch_index"], r["fragment_index"]) for r in appends} == {(b, i) for b in range(prior, count) for i in range(args.delta)}
                    assert len(records) == 3 + len(forks) + len(appends)
                else:
                    contexts = [r for r in records if r["phase"] == "context"]
                    assert phases.count("read_complete") == 1 and len(records) == 4 + 2 * args.iterations
                    assert len(contexts) == 1 + 2 * args.iterations
                    first_contexts = [r for r in contexts if r["state"] == "first_context_after_open"]
                    assert len(first_contexts) == 1 and first_contexts[0]["branch_index"] == 0
                    assert first_contexts[0]["mode"] == ("synthetic_compact_header_chain" if order == "scan-first" else "witness_delta")
                    for iteration in range(args.iterations):
                        pair = [r for r in contexts if r["state"] == "repeated" and r["iteration"] == iteration]
                        assert len(pair) == 2 and {r["mode"] for r in pair} == {"witness_delta", "synthetic_compact_header_chain"}
                        expected_branch = 0 if args.iterations == 1 else iteration * (count - 1) // (args.iterations - 1)
                        assert all(r["branch_index"] == expected_branch for r in pair)
            except (AssertionError, KeyError, StopIteration) as error:
                metadata["failures"].append({"run_id": run_id, "kind": "incomplete_or_invalid_phase", "error": str(error), "record_count": len(records)})
            if returncode or timeout:
                metadata["failures"].append({"run_id": run_id, "kind": "process_failure", "returncode": returncode, "timeout": timeout})
            save()
            print(json.dumps({"run_id": run_id, "returncode": returncode, "wall_seconds": report["wall_ns"] / 1e9}), flush=True)
            if metadata["failures"]:
                raise RuntimeError("measurement failed; raw evidence retained in " + str(root))
        for history in histories:
            paths = {backend: root / "fixtures" / f"n{history}-{backend}" for backend in ("native", "sqlite")}
            for backend, path in paths.items():
                run(backend, path, "fixture", history, 1, f"n{history}-{backend}-setup", "delta-first")
            counts = [1] if history == 10000 else branches
            for count in counts:
                for backend, path in paths.items():
                    run(backend, path, "extend", history, count, f"n{history}-b{count}-{backend}-extend", "delta-first")
                for leg, backend in enumerate(("native", "sqlite", "sqlite", "native")):
                    order = "delta-first" if leg % 2 == 0 else "scan-first"
                    run(backend, paths[backend], "read", history, count, f"n{history}-b{count}-leg{leg}-{backend}", order)
    metadata["binary_sha256_after"] = sha(binary)
    metadata["source_sha256_after"] = source_hashes()
    if metadata["binary_sha256_after"] != metadata["binary_sha256"] or metadata["source_sha256_after"] != metadata["source_sha256"]:
        metadata["failures"].append({"kind": "input_identity_changed"})
        save()
        raise RuntimeError("binary or source changed during measurement")
    metadata["exact_response_keys_verified"] = len(responses)
    metadata["logical_work_keys_verified"] = len(works)
    metadata["complete"] = True
    save()


if __name__ == "__main__":
    main()
