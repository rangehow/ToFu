# Immutable Harness graph prototype

This executable experiment stores shared immutable history, branch heads,
command receipts, and context witnesses in the **existing native
AuthorityDatabase**. The SQLite reference runs the identical Rust protocol over
scoped shared rows with WAL/FULL. SQLite supports the same logical capability;
this experiment separates a representation change from storage-engine speed.
It is not an application Harness benchmark or a production feature admission.

`src/lib.rs` defines the protocol. Each token binds tenant, owner, branch,
projection version, node hash, depth, generation, and epoch. A persisted witness
must exactly match a supplied token. Node hashes commit the canonical header,
which includes the owner scope, parent hash, depth, projector version, and delta
SHA-256. The returned suffix independently validates each node and reaches the
witnessed ancestor. Unknown receipts, changed commands under an existing receipt,
wrong owner/branch, stale CAS, and forged witnesses fail.

Fork shares the historical node hash and writes three metadata values
(head/witness/receipt), independent of history length. These are a constant
number of **logical operations**; each engine's physical index costs still
apply. `context` with a valid witness reads the appended delta, with three
metadata gets plus one node get per fragment. Every full materialization still
costs at least its output bytes. No query cache is used by the graph protocol.

The fragment model is append-only canonical text. Explicit `reset` for edit,
delete, or compaction and `adopt` invalidate previous context by advancing epoch;
generation also prevents ABA CAS acceptance. These operations are tested, but
real mutable Turn edits, compaction, projection, leases, provider assembly, and
fork authorization are **not wired** to this experiment. Caller identity is
trusted at this adapter boundary; scoped keys are not transport authentication.
There is no claim of arbitrary JSON incremental projection equivalence.

Context requests reject output exceeding a fragment count or serialized context
result byte bound (defaults 4096 fragments / 8 MiB; hard maxima 131072 / 64 MiB).
The bound includes token and JSON escaping, and excludes the small JSONL driver
status/work envelope. Each stored delta is at most 1 MiB. There is no pagination;
callers receiving a bound error must choose a smaller application-level window.
The experimental `reset` is a full replacement fragment, not a patch API.

All writes in one graph command publish atomically. Native uses Strict WAL
(default checked at open); SQLite reads back WAL/FULL and an explicit 48 MiB
page-cache setting. Native's direct AuthorityDatabase constructors use a 32 MiB
block cache and 16 MiB page cache, plus an enabled transaction overlay with
foreground merge thresholds (8 MiB pending bytes / 48 MiB WAL watermark / 12000
entries). The cache capacities are not a total process memory budget, and these
different architectures are not asserted equivalent. This runner bypasses daemon resource probes, transport,
background maintenance, and application event batching. Immutable nodes,
witnesses, and full request/result receipts are retained indefinitely. Receipts
repeat the request, including delta bytes (currently a JSON byte vector); all
such puts are included in logical write bytes. This prototype has no retention or
GC policy and does not claim low physical storage amplification.

## Run correctness

```sh
cargo test --manifest-path benchmarks/harness_graph/Cargo.toml \
  --features sqlite-reference -- --test-threads=4
python3 -m pytest -q benchmarks/harness_graph/tests/test_measure.py
cargo build --release --manifest-path benchmarks/harness_graph/Cargo.toml \
  --features sqlite-reference --bins
python3 benchmarks/harness_graph_controls.py \
  --binary "$CARGO_TARGET_DIR/release/tofudb-harness-graph" \
  --output /tmp/harness-graph-oracle --steps 512 --seed 20261020
```

The independent Python oracle uses a history-vector state machine and independently
computes hashes. It checks randomized operations, exact full/suffix responses,
owner and u64 boundaries, epoch/CAS/replay errors, response bounds, clean reopen,
and actual SIGKILL after acknowledged commands. Rust tests inject a failure after
each append row write and verify all touched namespaces roll back together.
The measurement-driver tests reject empty stdout, failed processes, and timeouts,
retain original evidence, and verify the child was reaped.

## Run measurements

Use a frozen release binary and a new output directory. Preserve the directory;
fixture databases and all original raw rows are deliberately not deleted.

```sh
python3 benchmarks/harness_graph/measure.py \
  --binary "$CARGO_TARGET_DIR/release/measure" \
  --output /tmp/harness-graph-main --delta 32
python3 benchmarks/harness_graph/measure.py \
  --binary "$CARGO_TARGET_DIR/release/measure" \
  --output /tmp/harness-graph-delta1 --histories 100000 --branches 32 \
  --delta 1 --iterations 8
python3 benchmarks/harness_graph/measure.py \
  --binary "$CARGO_TARGET_DIR/release/measure" \
  --output /tmp/harness-graph-delta256 --histories 100000 --branches 32 \
  --delta 256 --iterations 8
```

The main matrix has history lengths 1k/10k/100k, branches 1/32/256 at 1k and
100k, and one branch at 10k. The manifest lists the actual matrix. Each base
history is built once per backend. Branches extend cumulatively across stages;
base setup, durable fork/append operations, close, and reopen are separate
records. Each read stage runs four fresh processes in native/SQLite/SQLite/native
order; paired suffix/control reads also alternate order. The default 12 repeated
pairs sample branch indices evenly across the declared branch range. They do not
visit all 256 branches. The first context after open is separately recorded;
OS/filesystem caches are uncontrolled, so **reopen is not disk cold**. Repeated
reads do not guarantee every header fits the engine cache.

Both read arms return the exact same suffix and current token. The control
`synthetic_compact_header_chain` traverses compact graph headers to genesis and
then executes the same suffix algorithm. It does not read full prefix payloads.
It is still a point-walk synthetic graph control, **not the production Turn
header range-scan baseline**. Production `model_context` currently caps its
header list at 10,000; the 100k experiment is not an equivalent migration of that
fast path. Speed ratios against this control cannot be called production speedup.

`records.jsonl` retains each original result, serialized response hash/length,
logical reads/writes/value bytes, node/header reads, elapsed nanoseconds, PID,
scope, and configuration. Context timing includes execution and result
serialization, excluding independent expected-output verification and hashing.
Logical byte counters count values crossing the row adapter, not physical disk
traffic. Fork/append times include durable command commit. Open/close, process
wall/CPU time, RSS/high-water snapshots, and physical store file sizes are
reported separately. They are observations of this direct adapter and fixture,
not full Harness resource admission. Failures stop the run, retain raw evidence,
and cannot produce a complete manifest. The driver verifies phase completeness,
exit status, exact response/work parity, joined processes, and binary/source
identities before and after the run.
