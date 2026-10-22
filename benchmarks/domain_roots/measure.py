#!/usr/bin/env python3
"""Fresh-process ABBA driver and independent serialization-history oracle.

Only this retained runner admits measurements. Both engines run the same Rust
root forest and retry policy; this is not production Harness or Entity capacity.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resource_probe():
    # Preserve the mount/proc evidence as well as the common container-root
    # v1/v2 files; a missing v2 cpu.max does not mean unlimited CPU.
    paths = ("/proc/self/cgroup", "/sys/fs/cgroup/cpu.max",
             "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
             "/sys/fs/cgroup/cpu/cpu.stat", "/sys/fs/cgroup/cpuset/cpuset.cpus",
             "/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory.max")
    return {"affinity": sorted(os.sched_getaffinity(0)),
            "cgroup_files": {name: Path(name).read_text() for name in paths if Path(name).exists()},
            "cgroup_mounts": [line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                              if " - cgroup" in line]}


def request(index, parameters):
    domain = 0 if index * 37 % 100 < parameters["hot_percent"] else index % parameters["domains"]
    other = (domain + 1) % parameters["domains"] if index * 61 % 100 < parameters["cross_percent"] else domain
    return {"scope": {"tenant": 1, "owner": 1}, "command_id": f"command-{index:08}", "reads": [],
            "predicates": [{"domain": domain, "slot": 0, "at_least": 1}],
            "changes": [{"domain": domain, "slot": 0, "delta": -1}, {"domain": other, "slot": 1, "delta": 1}]}


def validate(records, backend, parameters, sqlite_version):
    if not records or records[0].get("phase") != "configuration" or records[-1].get("phase") != "complete":
        raise ValueError("missing ordered configuration/completion")
    configuration, complete = records[0], records[-1]
    if any(row.get("phase") != "sample" for row in records[1:-1]):
        raise ValueError("unknown/duplicate phase")
    if configuration["parameters"] != parameters or configuration["backend"] != backend:
        raise ValueError("actual matrix mismatch")
    actual = configuration["configuration"]
    if backend == "native":
        if actual["wal_durability"] != "Strict" or actual["owner"] != "one existing Engine":
            raise ValueError("native durability/owner mismatch")
    elif (actual["sqlite_version"], actual["journal_mode"], actual["synchronous"], actual["cache_size"], actual["mmap_size"]) != (sqlite_version, "wal", 2, -32768, 0):
        raise ValueError("SQLite durability/configuration mismatch")
    requests = [request(i, parameters) for i in range(parameters["operations"])]
    if configuration["input_sha256"] != hashlib.sha256(encoded(requests)).hexdigest():
        raise ValueError("input corpus mismatch")
    samples = [row["sample"] for row in records[1:-1]]
    if len(samples) != parameters["operations"] or sorted(row["index"] for row in samples) != list(range(len(requests))):
        raise ValueError("missing/duplicate measured operation")
    state = [[1_000_000] * parameters["slots"] for _ in range(parameters["domains"])]
    generation = [0] * parameters["domains"]
    last_changed = [1] * parameters["domains"]
    ordered = sorted(samples, key=lambda row: row["outcome"]["receipt"]["sequence"])
    for position, sample in enumerate(ordered, 2):
        for field in ("index", "attempts", "conflicts", "prepare_ns", "commit_ns", "total_ns", "discarded_prepared_bytes"):
            if type(sample[field]) is not int or sample[field] < 0:
                raise ValueError("invalid raw timing/retry field")
        req = requests[sample["index"]]
        outcome = sample["outcome"]
        receipt = outcome["receipt"]
        if outcome["replayed"] or receipt["sequence"] != position or not 1 <= receipt["read_sequence"] < position:
            raise ValueError("invalid receipt serialization sequence")
        if receipt["scope"] != req["scope"] or receipt["command_id"] != req["command_id"] or receipt["request_hash"] != list(hashlib.sha256(encoded(req)).digest()):
            raise ValueError("receipt identity mismatch")
        dependencies = set(req["reads"]) | {c["domain"] for c in req["changes"]} | {p["domain"] for p in req["predicates"]}
        if any(last_changed[d] > receipt["read_sequence"] for d in dependencies):
            raise ValueError("stale read domain escaped revalidation")
        if any(state[p["domain"]][p["slot"]] < p["at_least"] for p in req["predicates"]):
            raise ValueError("unsatisfied predicate committed")
        expected_values = []
        touched = set()
        for change in req["changes"]:
            d, slot = change["domain"], change["slot"]
            state[d][slot] += change["delta"]
            touched.add(d)
            expected_values.append({"domain": d, "slot": slot, "value": state[d][slot]})
        for domain in touched:
            generation[domain] += 1
            last_changed[domain] = position
        roots = receipt["roots"]
        if receipt["values"] != expected_values or len(roots) != len(touched) or {r["key"]["domain"] for r in roots} != touched:
            raise ValueError("receipt intermediate values or root set mismatch")
        for root in roots:
            if root["key"]["scope"] != req["scope"] or root["generation"] != generation[root["key"]["domain"]] or root["slots"] != parameters["slots"] or len(root["root"]) != 32:
                raise ValueError("root metadata mismatch")
        if sample["attempts"] != sample["conflicts"] + 1 or sample["total_ns"] < sample["prepare_ns"] + sample["commit_ns"]:
            raise ValueError("attempt or timing accounting mismatch")
        if sample["conflicts"] and not sample["discarded_prepared_bytes"]:
            raise ValueError("failed preparation bytes were omitted")
    if complete["operations"] != len(samples) or complete["receipt_count"] != len(samples) or complete["reopen_verified"] is not True:
        raise ValueError("incomplete durable readback")
    if complete["state_sha256"] != hashlib.sha256(encoded(state)).hexdigest():
        raise ValueError("final state differs from independent vector oracle")
    if complete["conflicts"] != sum(s["conflicts"] for s in samples) or complete["discarded_prepared_bytes"] != sum(s["discarded_prepared_bytes"] for s in samples):
        raise ValueError("retry costs were omitted")
    if complete["wall_ns"] <= 0 or complete["cpu_us"] < 0 or not 1 <= complete["max_parallel_preparations"] <= parameters["workers"]:
        raise ValueError("invalid wall/CPU/concurrency accounting")
    return {"operations": len(samples), "throughput_ops_s": len(samples) * 1e9 / complete["wall_ns"],
            "p50_ns": statistics.median(s["total_ns"] for s in samples),
            "cpu_us_per_op": complete["cpu_us"] / len(samples), **complete}


def identities(repo, binary, runtime):
    names = subprocess.check_output(["git", "ls-files", "packages/tofu-db", "packages/tofu-runtime", "contracts"], cwd=repo, text=True).splitlines()
    files = [repo / name for name in names]
    files += [p for p in (repo / "benchmarks/domain_roots").rglob("*") if p.is_file() and p.suffix in {".rs", ".toml", ".lock", ".py"}]
    return {"binary": sha(binary), "runtime": {p.name: sha(p) for p in sorted(runtime.iterdir()) if p.is_file()},
            "source": {str(p.relative_to(repo)): sha(p) for p in sorted(set(files))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True, help="frozen SQLite shared-library directory")
    parser.add_argument("--sqlite-version", default="3.53.4")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", default="1,4,8")
    parser.add_argument("--domains", type=int, default=32)
    parser.add_argument("--slots", type=int, default=64)
    parser.add_argument("--evidence-bytes", type=int, default=1024)
    parser.add_argument("--operations", type=int, default=128)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--scenarios", default="independent,hot,cross")
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    binary, runtime = args.binary.resolve(), args.runtime.resolve()
    workers = [int(value) for value in args.workers.split(",")]
    cases = {"independent": (0, 0), "hot": (0, 100), "cross": (100, 0), "mixed": (50, 10)}
    scenarios = args.scenarios.split(",")
    if not workers or min(workers) < 1 or len(workers) != len(set(workers)) or any(s not in cases for s in scenarios):
        parser.error("invalid worker/scenario matrix")
    if args.operations * args.domains > 4_194_304 or args.operations < 1:
        parser.error("oracle matrix bound exceeded")
    args.output.mkdir(parents=True, exist_ok=False)
    raw = args.output / "raw"; raw.mkdir()
    stores = args.output / "stores"; stores.mkdir()
    environment = dict(os.environ, LD_LIBRARY_PATH=str(runtime) + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
    matrix = [{"domains": args.domains, "slots": args.slots, "evidence_bytes": args.evidence_bytes, "workers": worker,
               "operations": args.operations, "cross_percent": cases[scenario][0], "hot_percent": cases[scenario][1]}
              for worker in workers for scenario in scenarios]
    manifest = {"state": "running", "diagnostic": args.diagnostic, "matrix": matrix, "resources": resource_probe(),
                "binary": str(binary), "child_ld_library_path": environment["LD_LIBRARY_PATH"],
                "before_identity": identities(repo, binary, runtime), "runs": [], "failures": []}
    def save():
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    save()
    for number, parameters in enumerate(matrix):
        for leg, backend in enumerate(("native", "sqlite", "sqlite", "native")):
            name = f"case-{number:02}-{leg}-{backend}"
            command = [str(binary), backend, str(stores / name), *[str(parameters[k]) for k in ("domains", "slots", "evidence_bytes", "workers", "operations", "cross_percent", "hot_percent")]]
            report = {"id": name, "backend": backend, "parameters": parameters, "command": command}
            started = time.monotonic_ns()
            with (raw / f"{name}.jsonl").open("w") as output, (raw / f"{name}.stderr").open("w") as errors:
                child = subprocess.Popen(command, env=environment, stdout=output, stderr=errors)
                report["pid"] = child.pid
                try:
                    report["returncode"] = child.wait(timeout=args.timeout_seconds)
                except BaseException as error:
                    child.kill(); report["returncode"] = child.wait()
                    report["failure"] = repr(error)
            report["process_wall_ns"] = time.monotonic_ns() - started
            manifest["runs"].append(report)
            try:
                if report["returncode"] != 0 or "failure" in report:
                    raise ValueError("measurement child failed or timed out")
                records = [json.loads(line) for line in (raw / f"{name}.jsonl").read_text().splitlines()]
                report["result"] = validate(records, backend, parameters, args.sqlite_version)
            except BaseException as error:
                report["failure"] = repr(error); manifest["failures"].append({"id": name, "error": repr(error)}); save(); raise
            save()
            print(json.dumps({"completed": name, "operations": report["result"]["operations"], "conflicts": report["result"]["conflicts"]}), flush=True)
    manifest["after_identity"] = identities(repo, binary, runtime)
    if manifest["before_identity"] != manifest["after_identity"]:
        manifest["failures"].append({"error": "source/binary/runtime identity changed"}); save(); raise ValueError("identity changed")
    if len({run["pid"] for run in manifest["runs"]}) != len(manifest["runs"]):
        raise ValueError("reused process identity")
    manifest["state"] = "complete"; save()
    summary = {"scope": "synthetic domain-root persistence, equal shared preparation; no production Harness/Entity replacement verdict", "runs": manifest["runs"]}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
