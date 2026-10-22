#!/usr/bin/env python3
"""Compare frozen native SSE sources in alternating fresh, CPU-pinned processes.

Entry: --baseline REV --dependency-dir CARGO_TARGET/debug/deps --output FILE.
Build locked dependencies first with cargo test --locked -p tofu-runtime --test sse.
Timing and allocator instrumentation use separate executables. This diagnostic
checks exact delivered work but grants no application/default-selection verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("packages/tofu-runtime/src/sse.rs",
           "packages/tofu-runtime/src/execution_contract.rs")
PROBE = "benchmarks/sse_framing.rs"
CASES = {
    "short": [20000, 64, 1, 4096],
    "large": [2000, 4096, 1, 4096],
    "multiline": [1000, 64, 64, 4096],
    "unicode": [4000, 64, 4, 7, "unicode"],
    "byte_fragments": [5000, 64, 1, 1],
    "tiny": [20000, 1, 1, 4096],
    "unicode_full": [4000, 64, 4, 4096, "unicode"],
}


def output(command: list[str]) -> str:
    return subprocess.check_output(command, cwd=ROOT, text=True, timeout=120)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--dependency-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rustc", default="rustc")
    parser.add_argument("--candidate-framer", type=Path,
                        help="compare an explicit experimental sse.rs without changing runtime sources")
    parser.add_argument("--pairs", type=int, default=12)
    args = parser.parse_args()
    if not 2 <= args.pairs <= 100:
        parser.error("--pairs must be between 2 and 100")
    dependency_dir = args.dependency_dir.resolve()
    libraries = list(dependency_dir.glob("libserde_json-*.rlib"))
    if len(libraries) != 1:
        parser.error("use an isolated dependency directory with one locked serde_json rlib")
    revision = output(["git", "rev-parse", "--verify", "--end-of-options",
                       args.baseline + "^{commit}"]).strip()
    cpu = min(os.sched_getaffinity(0))
    report = {
        "format": "tofu.sse-ownership-measure/v1",
        "baseline_revision": revision,
        "rustc": output([args.rustc, "--version"]).strip(),
        "affinity_cpu": cpu,
        "timing_scope": "SSE framing and exact payload validation; seven batches per fresh process",
        "limitations": ["Microbenchmark only; no whole-application or default-selection admission.",
                        "Concurrent host work may affect clocks.",
                        "Allocator peak is requested live bytes, not RSS/PSS."],
        "sources_sha256": {}, "binaries_sha256": {}, "cases": {},
    }
    with tempfile.TemporaryDirectory(prefix="tofu-sse-framing-") as temporary:
        stage = Path(temporary)
        # Snapshot both arms before compilation; later workspace edits cannot mix cohorts.
        for arm in ("before", "after"):
            identities = {}
            report["binaries_sha256"][arm] = {}
            for name in (*SOURCES, PROBE):
                raw = (subprocess.check_output(["git", "show", f"{revision}:{name}"],
                                               cwd=ROOT, timeout=30)
                       if arm == "before" and name in SOURCES else (ROOT / name).read_bytes())
                if arm == "after" and name == SOURCES[0] and args.candidate_framer is not None:
                    raw = args.candidate_framer.read_bytes()
                destination = stage / arm / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                identities[name] = hashlib.sha256(raw).hexdigest()
            report["sources_sha256"][arm] = identities
            for mode in ("timing", "allocations"):
                command = [args.rustc, "--edition=2021", "-O", "-C", "codegen-units=1",
                           str(stage / arm / PROBE), "-o", str(stage / f"{arm}-{mode}"),
                           "--extern", f"serde_json={libraries[0]}",
                           "--remap-path-prefix", f"{stage / arm}=sse-probe",
                           "-L", f"dependency={dependency_dir}"]
                if mode == "allocations":
                    command.extend(["--cfg", "allocation_counts"])
                subprocess.run(command, check=True, timeout=120)
                report["binaries_sha256"][arm][mode] = hashlib.sha256(
                    (stage / f"{arm}-{mode}").read_bytes()).hexdigest()
        if (report["sources_sha256"]["before"] == report["sources_sha256"]["after"]
                and report["binaries_sha256"]["before"] != report["binaries_sha256"]["after"]):
            raise RuntimeError("identical sources produced different probes; comparison refused")
        for label, arguments in CASES.items():
            rows, allocations = [], {}
            for mode, pairs in (("allocations", 1), ("timing", args.pairs)):
                for pair in range(pairs):
                    order = ("before", "after") if pair % 2 == 0 else ("after", "before")
                    for arm in order:
                        command = [str(stage / f"{arm}-{mode}"), *map(str, arguments)]
                        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True,
                                              preexec_fn=lambda: os.sched_setaffinity(0, {cpu})) as process:
                            try:
                                stdout, _ = process.communicate(timeout=60)
                            except BaseException:
                                process.kill()
                                process.wait()
                                raise
                            if process.returncode:
                                raise RuntimeError(f"{label}/{arm}/{mode} failed: {process.returncode}")
                            row = json.loads(stdout)
                            row.update(arm=arm, pid=process.pid, pair=pair)
                        if mode == "timing":
                            rows.append(row)
                        else:
                            allocations[arm] = row
            medians = {arm: statistics.median([
                statistics.median(row["samples_ns"]) / row["iterations"]
                for row in rows if row["arm"] == arm]) for arm in ("before", "after")}
            ratios = [statistics.median(next(row for row in rows
                       if row["arm"] == "after" and row["pair"] == pair)["samples_ns"])
                      / statistics.median(next(row for row in rows
                       if row["arm"] == "before" and row["pair"] == pair)["samples_ns"])
                      for pair in range(args.pairs)]
            report["cases"][label] = {"arguments": arguments, "rows": rows,
                                      "allocation": allocations, "median_ns_per_event": medians,
                                      "paired_ratios": ratios, "median_ratio": statistics.median(ratios)}
            print(f"{label}: candidate/baseline median {statistics.median(ratios):.3f}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
