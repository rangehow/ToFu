"""Offline paired snapshot CPU probe; no storage/network or production admission.

Run from the repository with ``python -m benchmarks.conversation_snapshot_cpu
--reference-ref <pre-change commit> --output data/benchmarks/snapshot-cpu.json``.
Same-process alternating pairs deliberately exclude process/startup/RSS evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import types

import orjson

from lib.conversation_sync import service, validation
from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
from lib.conversation_sync.snapshot_query import ConversationSnapshotQuery
from lib import turn_projection_segments as projection

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("lib/conversation_sync/compiled_validation.py", "lib/turn_projection_segments.py")


def fixture():
    turns = []
    for index in range(12):
        rounds, segments = [], []
        for position in range(16):
            call = f"call-{index}-{position}"
            content = f"synthetic:{call}:" + "工具-result-" * 760
            rounds.append({"toolCallId": call, "toolName": "fetch_url",
                           "toolArgs": {"url": "https://example.invalid/fixture"},
                           "toolContent": content, "status": "done", "llmRound": position})
            segments.append({"type": "tool_use", "blockId": "tool:" + call,
                             "id": call, "name": "fetch_url",
                             "input": rounds[-1]["toolArgs"],
                             "result": {"content": content, "status": "done"}})
        terminal = "terminal-汉🙂" * 500
        segments.append({"type": "text", "blockId": "text:terminal", "terminal": True,
                         "deliverable": True, "text": terminal})
        turns.append({"turnId": f"turn-{index}", "conversationId": "cpu-fixture",
                      "laneId": "main", "ordinal": index, "actor": "assistant",
                      "kind": "reply", "runId": "synthetic-run",
                      "status": "running" if index == 11 else "completed",
                      "projection": {"content": terminal, "segments": segments, "toolRounds": rounds},
                      "projectionRevision": 1, "settlement": {}, "createdAt": 1, "updatedAt": 1})
    return {"syncSequence": 1, "conversationRevision": 1, "settings": {},
            "turns": turns, "attempts": [], "queueItems": []}


def load_frozen(ref):
    modules, hashes = [], {}
    for path in SOURCES:
        source = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT)
        module = types.ModuleType("frozen_" + Path(path).stem)
        exec(compile(source, f"{ref}:{path}", "exec"), module.__dict__)
        modules.append(module)
        hashes[path] = hashlib.sha256(source).hexdigest()
    return modules, hashes


@contextmanager
def arm(modules):
    old = (validation._SUCCESS_PREDICATES, service.public_value_with_stable_segments,
           service.snapshot_with_reference_tool_segments)
    compiler, projector = modules
    validation._SUCCESS_PREDICATES = compiler.compile_success_predicates(OPENAPI_SCHEMAS)
    service.public_value_with_stable_segments = projector.public_value_with_stable_segments
    service.snapshot_with_reference_tool_segments = projector.snapshot_with_reference_tool_segments
    try:
        yield
    finally:
        (validation._SUCCESS_PREDICATES, service.public_value_with_stable_segments,
         service.snapshot_with_reference_tool_segments) = old


def measure(action, samples):
    wall, cpu = [], []
    for _ in range(samples):
        start_wall, start_cpu = time.perf_counter_ns(), time.thread_time_ns()
        result = action()
        cpu.append(time.thread_time_ns() - start_cpu)
        wall.append(time.perf_counter_ns() - start_wall)
    return {"wall_ns": wall, "thread_cpu_ns": cpu,
            "p50_ms": statistics.median(wall) / 1e6,
            "mean_cpu_ms": statistics.mean(cpu) / 1e6}, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-ref", required=True)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10000 or not 1 <= args.pairs <= 20:
        parser.error("bounded samples/pairs required")
    ref = subprocess.check_output(["git", "rev-parse", args.reference_ref + "^{commit}"],
                                  cwd=ROOT, text=True).strip()
    frozen, reference_hashes = load_frozen(ref)
    from lib.conversation_sync import compiled_validation
    current = [compiled_validation, projection]
    stored = fixture()
    before = orjson.dumps(stored)

    class Repository:
        def snapshot(self, *_args, **_kwargs):
            return stored

    query = service.ConversationSyncService(Repository(), snapshot_query_factory=lambda loader:
        ConversationSnapshotQuery(loader, gather_seconds=0))
    report = {"scope": __doc__, "reference_ref": ref, "samples_per_arm_per_pair": args.samples,
              "warmups": 20, "reference_hashes": reference_hashes,
              "candidate_hashes": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES},
              "fixture_sha256": hashlib.sha256(before).hexdigest(), "pairs": [],
              "limitations": ["synthetic fixture, injected repository; no production data",
                              "same process, alternating arms; no RSS/startup evidence",
                              "gather disabled in both arms; no network/queue/storage/durability",
                              "not a production performance gate; P99 not certified"]}
    with arm(frozen):
        snapshot = query.snapshot("cpu-fixture", 1)
        expected = query.snapshot("cpu-fixture", 1, segment_payload="refs")
    report["full_wire_bytes"] = len(orjson.dumps(snapshot))
    report["refs_wire_bytes"] = len(orjson.dumps(expected))
    report["output_sha256"] = hashlib.sha256(orjson.dumps(expected)).hexdigest()
    for pair in range(args.pairs):
        for name in (("reference", "candidate") if pair % 2 == 0 else ("candidate", "reference")):
            modules = frozen if name == "reference" else current
            with arm(modules):
                actions = {
                    "validation": lambda: validation.decode("ConversationSyncSnapshot", snapshot),
                    "refs_projection": lambda: modules[1].snapshot_with_reference_tool_segments(snapshot),
                    "snapshot_encode": lambda: orjson.dumps(query.snapshot("cpu-fixture", 1, segment_payload="refs")),
                }
                row = {"pair": pair, "arm": name, "stages": {}}
                for stage, action in actions.items():
                    for _ in range(20):
                        action()
                    clocks, result = measure(action, args.samples)
                    target = snapshot if stage == "validation" else expected
                    assert result == (orjson.dumps(target) if stage == "snapshot_encode" else target)
                    row["stages"][stage] = clocks
                assert orjson.dumps(stored) == before, "fixture was mutated"
                report["pairs"].append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"full_wire_bytes": report["full_wire_bytes"], "refs_wire_bytes": report["refs_wire_bytes"],
                      "pairs": [{"pair": row["pair"], "arm": row["arm"],
                                 "p50_ms": {k: v["p50_ms"] for k, v in row["stages"].items()}}
                                for row in report["pairs"]]}, indent=2))


if __name__ == "__main__":
    main()
