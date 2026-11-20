# Storage performance charter (Tofu-DB vs SQLite)

Full-catalog history promotion and accepted-event-to-push anchors follow
[publication latency](TOFUDB_PUBLICATION_LATENCY.md#full-catalog-history-promotion-and-write-delivery-anchors).
Effective cache-budget evidence and eager-admission qualification follow
[decoded-page admission](TOFUDB_WRITE_PATH_OVERLAY.md#decoded-page-admission-and-benchmark-budgets).
This document defines how we measure Tofu-DB against the legacy sidecar and what "comprehensively better" means. It is intentionally separate from
`STORAGE_TOFUDB.md` (which documents the certified durability/correctness
guarantees) — this file is about *speed, footprint, and tail behavior under the agent-harness workload*.

The [Harness database architecture experiments](TOFUDB_HARNESS_DATABASE.md)
target work that grows with history, branching and repeated projection updates.
They distinguish a better execution data model from a native-engine advantage
and from whole-Agent results; experimental primitives do not change production
storage selection or weaken the acceptance criteria below.

## SQLite reference and change review

Problem: isolated improvements can leave deployment, recovery and maintenance
costs outside the measured path. SQLite is an architectural reference as well
as a numerical baseline. Decision: review the affected rows below before a
storage change, reuse their existing owners, and name both the measured gain
and the remaining gap. The presence of a module is implementation evidence;
only matching results and qualified measurements establish parity.

| SQLite principle and primary reference | Existing Tofu owner | Evidence required / current boundary |
|---|---|---|
| Simple embedding and distribution ([amalgamation](https://sqlite.org/amalgamation.html)) | `serverctl_pkg/native_engine.py`, desktop packaging, [native lifecycle](NATIVE_STORAGE_LIFECYCLE.md) | Build and certify before service interruption; unchanged sources reuse the artifact. Source-development rebuilds remain distinct from packaged startup. Keep source identity and rollback checks. |
| Explicit WAL recovery and checkpoint work ([WAL](https://sqlite.org/wal.html)) | `engine.rs`, `entity_overlay.rs`, [recovery attribution](TOFUDB_RECOVERY_ATTRIBUTION.md) | Measure shutdown, reopen and first correct read, plus application request-to-readiness separately. Sweep retained history and unreplayed tail independently; a tiny reopened fixture cannot establish production RTO. |
| Readers retain stable snapshots while a writer commits ([WAL concurrency](https://sqlite.org/wal.html#concurrency)) | `authority_query_view.rs`, [embedded phases](TOFUDB_EMBEDDED_PHASES.md) | Writer progress with old readers, concurrent suffix retention, and exact snapshot/owner fences. Reader-only throughput does not prove writer progress. |
| Checkpoint work is explicit and resumable ([WAL checkpoints](https://sqlite.org/wal.html#checkpointing)) | `maintenance.rs`, `entity.rs`, [write overlay](TOFUDB_WRITE_PATH_OVERLAY.md) | Foreground latency, full-drain work, WAL bytes and oldest-reader pins are separate observations. Keep batching and durability boundaries; never copy SQLite page-checkpoint code into a content-addressed format without equivalent recovery proof. |
| Atomic data and index changes ([atomic commit](https://sqlite.org/atomiccommit.html)) | `authority.rs`, `versioned_document.rs`, Turn transition owners | Commit-fault, lost-ACK, restart, deletion and restore checks must agree on primary state and derived indexes. A stale index cannot be hidden by returning fewer rows. |
| Reuse verified pages and query work ([cache retention](https://sqlite.org/atomiccommit.html)) | [entity pages](TOFUDB_ENTITY_PAGES.md), [result cache](TOFUDB_QUERY_RESULT_CACHE.md), `cold_read_tests.rs` | Cold/warm I/O counts and output equality; snapshot, tenant, owner, operation and payload remain in cache identity. Cache retention never becomes work admission. |
| Choose access paths that avoid unnecessary reads ([query optimizer](https://sqlite.org/optoverview.html)) | `entity.rs`, `tofusql.rs`, domain query owners | Measure page visits, rows materialized, output bytes and write cost. Prefix/range/covering access and pagination follow the actual operation; an extra index or a new generic optimizer needs evidence. |
| Local calls avoid a network round trip ([small queries](https://sqlite.org/np1queryprob.html)) | `lib/storage/`, embedded and wire clients | Keep application-semantic and raw-engine comparisons distinct. The SQLite sidecar includes transport costs absent from direct SQLite calls; an N+1 rule must account for that boundary. |
| Mature SQL, search and JSON tools ([FTS5](https://sqlite.org/fts5.html), [JSON](https://sqlite.org/json1.html)) | SQL authority, search projections, existing TofuSQL catalog | Reuse the current capability owner. Native catalog/search support does not establish full SQLite SQL/extension parity; compare ordering, Unicode, continuation and error semantics on the same corpus. |
| Incremental online backup ([backup API](https://sqlite.org/backup.html)) | `online_backup.rs`, `fastpath.rs`, [online GC](TOFUDB_ONLINE_GC.md) | Restore and read the pinned snapshot, exercise writer overlap and interrupted publication, and retain the previous valid recovery generation. Backup-file existence is insufficient. |
| Stable interfaces and durable formats ([version policy](https://sqlite.org/versionnumbers.html)) | Native format/schema gates, `build.rs`, release certification | Pin engine/library/source identities and test supported historical stores. A successful current-version round trip does not certify upgrade/rollback compatibility. |
| Fault testing and optimization equivalence ([SQLite testing](https://sqlite.org/testing.html)) | `vfs.rs`, `entity_overlay_tests.rs`, semantic differential suite and filesystem certification | Compare optimized/reference outputs; retain I/O errors, short writes, sync loss, malformed input and real process-crash coverage. Existing tests do not establish SQLite-level coverage or device power-loss guarantees. |
| Match the storage architecture to its deployment ([appropriate uses](https://sqlite.org/whentouse.html)) | Selected-authority lifecycle, [storage operations](STORAGE_OPERATIONS.md), local/FUSE comparison cohorts | SQLite's local WAL/shared-memory and network-locking assumptions are not a blanket FUSE guarantee. Benchmark the actual volume and durability; backend changes remain explicit owner decisions. |

Alternatives rejected: copying every SQLite feature introduces capabilities
without a demonstrated product need; quoting raw-SQL microbenchmarks as whole
application wins drops semantic work; checking only the latest complaint leaves
unmeasured lifecycle costs. New machinery requires a concrete gap in the common
owner and an experiment that beats measurement noise without changing results.

The existing `storage_performance_charter_v1.json` and
`tofudb_charter_assess.py` own numerical acceptance. Their history-scaling,
distracted-host and reader/writer-scaling gaps remain visible until matching
evidence exists. The nightly `check_perf_regression.py` job measures task replay;
it is not the native-versus-SQLite certification. Focused comparison-harness
tests run in the ordinary unit suite. Full native/SQLite, actual-volume and
process-crash receipts remain explicit verification work; no new background
benchmark service or automatic production restart is introduced.

## Loose-block length reuse

Problem: loose-block reads validated handle length, then RealFile queried the
same length again inside read_all. Decision: reuse the observation as an
allocation hint while reading to EOF under the original byte bound. Actual
growth beyond the bound still refuses; a shortened block checks its minimum
before header access. The VFS default delegates through existing read_all,
preserving wrapper/encryption/fault boundaries. Alternatives rejected: reading
only the observed prefix can hide appended corrupt bytes; removing size and
checksum checks weakens integrity.

`loose_read_measure` runs three alternating pairs of 128 warm 64 KiB reads
over private temporary files on the project's FUSE volume. Duplicate/reused
length medians were 0.00481/0.00434 ms; median P95s 0.00615/0.00567 ms. Every
read checks its length and digest. This small component result excludes opens,
block verification, cold caches and application RPC; it is not a task-speed
claim. One duplicate real file-metadata query is removed per loose read.
Run `cargo run --manifest-path packages/tofu-db/Cargo.toml --example
loose_read_measure -- <existing-base-directory>`. File growth/shrink and
simulated read faults are covered in `vfs::tests`.

## Hot-cache scaling

Problem: fixed 2/1 GiB payload/page ceilings stopped cache growth while the
launch probe still reported abundant headroom; chunk proofs inherited the
payload ceiling and could repeatedly decode a larger working set.
Decision: retain headroom/16 and /32 defaults and lean floors, with explicit
overrides bounded per cache by headroom/4. Allocation remains demand-driven;
ordinary eviction, immutable-source verification and close/zero-capacity
release retain ownership. No worker or durable state is added.
Alternatives rejected: larger fixed caps merely move the plateau; relying on
kernel caching does not retain decoded proofs; unlimited overrides can consume
all launch-observed headroom.

The opt-in `hot_cache_scaling_tests::hot_cache_scaling_measurement` runs three alternating pairs against the production verified decoder/cache, using 300 distinct 1 MiB chunks and the same current encoded allocations. With 128 GiB declared headroom, old/scaled decode capacities were 128/512 MiB. Three timed passes produced identical output digests: old/scaled hits 0/900, misses 900/0, median 401.26/1.27 ms in the debug build. Accounted residency was 133,252,159/314,763,240 bytes, within each budget, and returned to zero after release. This measures verified decode reuse, excluding source I/O, JSON, RPC and whole-task latency; it does not establish a production speedup. Run with `cargo test --manifest-path packages/tofu-db/Cargo.toml --lib hot_cache_scaling_measurement -- --ignored --nocapture`. Ordinary resource tests cover lean/pressured hosts and continued growth on larger hosts.

## Native event batch composition

Problem: the application event batcher used the wire-frame budget to combine
independent calls, while a native transaction above its format-bound carried
payload ceiling materializes and synchronizes referenced loose blocks under
authority. A disposable same-source test of 500 distinct 8 KiB streams reproduced
this boundary twice: one batch took 3.86/3.76 s with one spill each; four 125-row
batches took 293/201 ms without spill. All four 500-row SIGKILL recoveries were
exact. These storage-only measurements do not certify the 1000-conversation
production latency target.

Decision: native readiness and control observations publish the additive
`resourceBudget.eventBatchComposition` declaration defined in
`contracts/storage_v2.json`. Its carried ceiling comes directly from the native
transaction format constant. Raw segment/event header costs and catalog/entity
metadata coefficients come from their existing encoders. The composition cost
is raw event JSON bytes plus the native format stamp, segment/event headers,
stream identity, and estimated metadata. One quarter of the advertised ceiling
is reserved for transaction-level framing and state-dependent representation
work. This is a conservative grouping estimate, never a durability or fresh
source proof; engine admission and the existing spill path remain authoritative.

The native client reads only the cached readiness declaration. The batcher
obtains its selected client outside the queue condition, then claims only one
composition-limited RPC group. Future events remain queued and cancellable;
an unchanged head is checked after client acquisition. Every ACK, fence,
natural-key outcome, callback, timeout, retry and shutdown boundary retains the
original per-group behavior. A legal individually oversized event is submitted
alone with the original event admission ceiling. Queue residency, worker count
and connection pools retain the existing launch-probed sizing. Metrics expose
the active composition ceiling and maximum estimated composition bytes.

A flush barrier ends its FIFO prefix; future events and their provider failures cannot join or invalidate it. Accepted events receive an ordinal, and a barrier records the last accepted ordinal. One bounded primitive failure summary and its earliest ordinal persist until batcher closure, covering actual provider/RPC failure and accepted events cancelled before dispatch. The summary retains no exception, traceback or event graph. Every covered barrier receives a fresh typed error; later success is not proof that the failed accepted prefix became durable. The explicit per-row `attempt_not_live` rejection also records an unstored accepted event without changing its business result or sibling ACKs; natural-key deduplication remains durable. A claimed event's producer awaits its batch outcome instead of timing out, and observational callback failures do not invent persistence failure. Six added integer/slot fields cost 256 additional retained bytes per queued event within the existing byte quota.

Alternatives: a fixed small item cap starves cheap events and ignores payload
size; increasing the native carried ceiling changes bounded replay memory and
does not address lock work; using the wire bytes alone misses metadata and
native format costs. SQL and older/invalid native declarations retain legacy
wire grouping and do not claim this optimization. The headroom fraction is a
selection policy to measure, not an optimality claim.

## Application write amplification

**Problem:** on STORAGE-PRESSURE-COMPARE-1's fixed task, Codex writes 2.2–2.4 MB
and makes 1.6–2.7 fsyncs per Turn. Tofu wrote:
- SQL authority: 16.2 MB and 157 fsyncs per Turn;
- TofuDB f3102951: 30.8 MB and 344 fsyncs per Turn.

That amplification is what makes network-volume stalls hurt Tofu. The
attribution assigns every byte and fsync at the FUSE boundary to one write
site; shares sum to the measured total in each cell
(`.eval-runs/write-amp-20261113/results/ATTRIBUTION.md`, `TABLES.md`).

**Decision:** remove each write the contracts do not need, by structure: a
copy stored once, a derived copy rebuilt from its authority, a frame committed
at a boundary the stream contract already has. No time window, batch size,
debounce interval, threshold or cap is introduced. Acknowledged writes stay
durable.

| redundant write (measured share) | structural removal | contract kept | status |
|---|---|---|---|
| diagnostic evidence stored twice per transaction: in the attempt event and the carried task-event row (38% of frame-command bytes) | the row is the single copy; the attempt event names it, and full replay resolves it | replay, Request Inspector, commit-before-push, retention | done; [record](STORAGE_EVENT_REPLAY.md#diagnostic-evidence-is-stored-once) |
| five SQL indexes duplicating a PK/UNIQUE key; `storage_events_task_idx` alone was 4.8% of SQL bytes | fresh authorities never create them; the offline index transition retires them | every query keeps its access path (the PK index serves the hint) | done; [record](STORAGE_OPERATIONS.md#duplicate-key-indexes) |
| text frames emitted before their projection write was admitted, one standalone commit each (21 of 24 commits per Turn, 13.6% of SQL fsyncs) | the provider text coalescer emits when the committed cadence admits the write | commit-before-push, browser cadence, cold replay of every frame | held: measured −13.5% SQL / −4.3% TofuDB fsyncs, but on TofuDB the first commit a WAL stall holds becomes a visible projection write, so a crash erases a change a reader saw in about every stall instead of about 1 in 3. Lands after the engine publishes only acknowledged commits (DB-5), with 0/3 observed losses |
| model continuation rewritten whole at every settlement (559 KB per Turn at 50 Turns, quadratic in conversation length) | append-only per-Turn segments plus a versioned head; a typed base-rewrite instead of a threshold | restore, CAS frontier, receipts, lifecycle (Turn delete drops head and segments) | R5: DB-5 owns the engine half, WRITE-AMP owns SQL and the producer |
| `key_stats.json` telemetry rewritten with fsync on every flush (4 fsync/Turn) | split decisions (overrides, billing stops: fsynced before returning) from reconstructible telemetry (atomic replace, no fsync) | decision durability; telemetry keeps its documented crash-loss window | done (config-store fsyncs 4.1 → 0.1 per Turn); [record](modules/auth_providers_billing.md#telemetry-is-published-without-a-durability-barrier) |
| running task-result checkpoints rewriting state the Turn already holds (12 commits per Turn: 6 sampled, 3 at tool/round boundaries, 3 lifecycle) | the provider boundary writes no row; each round end takes the round's one sampled checkpoint, which a conversation attempt skips because every reader of its row reads its Turn ([decision](modules/task_engine.md#persistence-discipline)) | polling (the durable view reads the Turn), restart recovery (`turn.recover`; birth, `code_exec` boundary and terminal rows unchanged), billing, recovery fields (`todoState`, evolution consumption) | implemented on `perf/write-amp-f-20261113`: task-result commits 12 → 6 per Turn; fsync/Turn −3.8% on SQL and −1.1% on TofuDB; bytes/Turn flat on SQL (+0.1%) and +1.4% on TofuDB (next row); on TofuDB the first event after a 32 s stall arrives in 0.9–1.1 s instead of 3.1–7.1 s; 0 acknowledged writes lost in 36 kill cells; a task without a conversation writes 24–50% fewer sampled bytes |
| a stored Turn projection holds each finished tool result twice, in `toolRounds[].toolContent` and in `segments[].result.content` (25.5 KB for one 12 KB result), and TofuDB re-materializes both at each structural projection event: (f)'s round-end mirror refresh, which the retired throttle skipped at about half the round ends, added 1.47 such events and 0.41 MB per Turn (`projectionCheckpointBytes` +71 KB, `overlayMaterializedBytes` +161 KB) | a `tool_use` segment names its round by identity (`id`, `llmRound`) instead of embedding the result body; readers take bodies from `toolRounds`, already the browser's authority for rich bodies | browser prose placement (segments), tool bodies (`toolRounds`), the thin task-row form, compat deliverable text (text segments only) | proposal to the Turn projection owner; the projection checkpoint cost per structural event goes to DB-5's R5 |
| a task without a conversation rewrites its whole tool-round and segment arrays at every sampled checkpoint, quadratic in its rounds (124 MB of row payload over 100 rounds of 4 s) | append-only per-round task-row segments under a small head, written in the round end's transaction; readers assemble the arrays, and terminal publication stays one row | polling, crash diagnostics, cold replay and terminal rows of tasks without a conversation | proposal for R5: a task-result storage contract change on both authorities |
| per-frame conversation revision bump (+2 indexes) and change-log index maintenance (4 b-trees, two overlapping) | contract-bound (`conversationRevision`, change replay) | sync contract | proposal to the sync-contract owner |
| `run_command` journal: 4 fsyncs per modifying call | none: three are the undo commit protocol (payload, its name, the row), and the fourth is the journal's re-flush of an uncertain ancestor | undo | no change; a read-only command records nothing |
| TofuDB background rewrites each WAL byte about five more times (rotation 8.0, pack 6.6, GC 5.1, maintenance 2.0 MB/Turn; overlay 5.0 MB/Turn plus 42 merge fsyncs) | each block written once to its final home; GC as a by-product of retirement | engine durability | DB-5 (R5), with the measured tables |

With the evidence, index and key-health items, 3 reps per build:
- SQL authority: 16.20 → 12.44 MB and 157.2 → 153.3 fsyncs per Turn (−23% bytes).
- TofuDB: 30.83 → 30.02 MB and 344.3 → 340.7 fsyncs per Turn. Its foreground
  WAL fell 7%; its total is dominated by the engine background, and it already
  deduplicated the evidence copy by content.
- Stall latency is unchanged.
- Kill tests lost no acknowledged write. A change a reader saw before its WAL
  frame was acknowledged was lost at the baseline rate: 2 of 12 TofuDB cells
  across these builds, 0 on SQLite.

Evidence: `.eval-runs/write-amp-20261113/results/BEFORE_AFTER.md`.

**Alternatives rejected:**
- Compressing or capping the duplicated payloads keeps the copies and adds a
  threshold.
- A timer or debounce on frame commits trades user-visible latency for writes
  and is a tuning knob.
- Dropping the fsync of a shared file weakens the decisions stored in it.
- Deleting indexes at startup holds the single SQLite writer on populated
  authorities.

## Loose batch publication per hash shard

Problem: a transaction whose carried payload exceeds the format's 2 MiB carried
ceiling, and a checkpoint's fallback materialization, publish every pending
block as a loose file before the WAL record or CONTROL publication that relies
on it. `BlockStore::flush_pending_selection` did this one file at a time: two
stats, a shard `mkdir` probe, create, write and rename per block, then a second
pass that stat'ed, reopened and double-synced every file, then the shard
directory barriers. Wall time was the sum of every metadata round trip, which
costs milliseconds on DolphinFS. Live engine telemetry on 2026-11-02 (one 8.6 h
process lifetime) showed the maintenance component spending 26,013 s in 5,680
commits (4.6 s each, 84% of wall time). Read-only `/proc` sampling put the
maintenance thread entirely in `openat`/`statx`/`rename`/`write` under
`projections/tofudb-search/blocks/`: search-projection commits spilled their
pages one file at a time. That worker also runs the background overlay drain;
over one 90 s window `overlayBackgroundChunks` stayed flat while 31 overlay
merges ran on the foreground commit-path backstop. The 3.86/3.76 s event-batch
spill above is the same path under the authority mutex.

Decision: the batch is grouped by hash shard. Each shard writes every absent
block and syncs it on its creating handle before the rename, so a published name
never carries unsynced content and the reopen pass disappears. It then syncs
verified existing copies and fsync-only marks and issues one directory barrier.
Shard directories are independent, so a VFS that declares
`supports_concurrent_file_publication` (`RealVfs`) runs every shard but the
first on a scoped worker; the caller takes the first and joins all before
returning. A batch that runs under the authority mutex therefore finishes all
of its I/O inside the hold, which the online collector's deletion holds rely
on. The partition is the on-disk format's (one shard per first content-ID
byte), not a sized pool, and a refused thread spawn runs its shard on the
caller. Workers inherit the caller's stats component; request-profile spans are
thread-bound, so worker VFS time appears as the enclosing stage's exclusive
time. Deterministic simulation and wrapper VFSes keep one caller at a time so
fault traces stay reproducible. `ensure_shard` also marks a shard this store
created until its root barrier succeeds: previously a retry after a failed
barrier found the directory, skipped the barrier, and its acknowledged blocks
could vanish on power loss. Without a failure the operation sequence is
unchanged, which the certification matrix's pre-created shards rely on.

Measurements (2026-11-02, DolphinFS volume hosting production data, live
traffic, debug test profile on both sides): a disposable harness committed
fresh random payloads through `Engine::commit_transaction` on a new authority
and timed the steady second spill (existing shards), alternating baseline, this
change, and this change with shard concurrency withheld, three rounds each.
500 x 8 KiB: baseline 4.86/6.35/5.98 s, concurrent 0.83/0.66/0.73 s, sequential
4.46/5.38/4.62 s. 2,048 x 4 KiB over two passes: baseline 18.1-20.9 s,
concurrent 2.13-2.40 s, sequential 15.9-20.0 s. First spills that also create
shards: 500 x 8 KiB baseline 5.0-5.4 s, concurrent 0.89-1.31 s. The per-file
protocol alone is at most a modest gain; shard concurrency supplies the 8-9x.
These are storage-only spill commits; they do not establish application or
whole-Agent latency, and the effect on the live maintenance worker is
unmeasured until a deployed engine reports it.

Alternatives rejected: packing a spill into one payload segment writes one file
but puts catalog publication, CONTROL and online-GC coupling on the commit path;
raising the carried ceiling changes bounded WAL replay memory; a fixed or
CPU-sized worker pool adds a sizing knob to a format-defined partition;
remembering published shards per process would remove the remaining `mkdir`
probe but makes the operation trace depend on which blocks share a shard,
breaking reproducible fault campaigns.

Verification: `block_loose_batch_tests.rs` runs error, torn-write and lost-sync
campaigns over every operation kind on concurrent shard workers, retries each
failed batch to a durable state, proves workers run concurrently under the
caller's component, covers the failed-root-barrier retry and runs a real
filesystem batch; the certification matrix and loose-race specifications
cover the sequential path.

## Request cost attribution

Problem: process-wide nested stage totals cannot explain one slow request or separate lock queueing from the work holding the lock. Optimizing a high call
count without measuring its share of the request can leave end-to-end latency almost unchanged.

Decision: `src/request_profile.rs` adds an opt-in, content-free dispatch profile exported as `system.metrics.requestProfile` (`tofudb.request-profile/v1`). Set
`TOFU_DB_REQUEST_PROFILE_EVERY=1` before launching a disposable authority to
record every request; N records every Nth request. Zero/unset disables it. Configuration is read once per process. Sampling is deterministic and may alias
periodic workloads; use every request for bounded experiments, and measure observer overhead with the same binary off/on/on/off before drawing conclusions.
It does not change admission, durability, retry, ownership or request results. Document materialization and public-Turn/snapshot boundaries use the same opt-in collector; their bounded memory, matched window driver and equal-work limits live in [JSON materialization](TOFUDB_JSON_MATERIALIZATION.md#window-read-ownership-and-attribution).

Existing stage timers join a thread-bound span stack. Exclusive elapsed time subtracts immediate children; exclusive stages plus `unattributedNanos` must
equal dispatch elapsed time. Inclusive stages remain available to understand nested work but must never be added together. Non-LIFO, unclosed and overflowed
spans invalidate accounting explicitly. `scripts/tofudb_profile_report.py` rejects inconsistent ledgers rather than manufacturing an attribution.

Default-off authority commit spans separate validated rebase, private blob publication, deferred indexes/recency, outbox capture, and stream encoding/catalog preparation; they add five fixed anonymous ledger slots. Overlay head-version, original blind-version and range-witness validation add three more fixed slots (72 bytes per sample/operation aggregate); existing sample bounds and size-derived byte reports remain. Additional spans cover FIFO admission, mutex acquisition, authority holds, replica creation, transaction compilation, query execution, and real-VFS
open/read/write/sync/metadata/directory/mutation operations. Fixed work counters record entity reads/reuse, overlay/tree resolutions, conflict classes, abandoned
preparation time, logical WAL/materialization bytes and requested VFS bytes. Failed operations still contribute elapsed time and attempted bytes. VFS byte
counts are not successful I/O or physical-device amplification. Requested read-all bytes come from the length observation, including a rejected bound.

The dispatch boundary starts with a parsed request and ends with the semantic response. Embedded enqueue-to-worker delay is recorded separately; neither
client scheduling, JSON conversion outside dispatch, nor response delivery is included in dispatch elapsed time. Non-embedded queue time is unknown, not zero.
The report prints dispatch mean, embedded queue mean, queue sample coverage and their combined native mean. It combines them only when queue samples cover the
same complete request population; partial/missing coverage leaves the combined mean unavailable. This sum excludes client scheduling, conversion and delivery,
so it is not end-to-end latency. A dispatch-only improvement can hide longer worker queueing and must not establish a performance win by itself.
Background-driver time remains in existing component counters and CPU sampling; it is not charged to a foreground request. Wait/hold/VFS elapsed time is wall
time, not CPU time. A wait-heavy profile identifies queue amplification; the lock-holder and CPU profiles are still needed to identify its cause.

Retention is process-local and ends on process exit, including across embedded handle reopen. Only catalog operation names plus one `uncataloged` label are
retained. There are 64 fixed histogram buckets (log2 nanosecond upper bounds),
64 nested spans, and at most 32 slow samples; sample numbers are local counters,
never application correlation IDs. No owner/key/path/payload/error text is stored. Active sampled requests release their buffers on completion or unwind;
`activeRequestBytes` and `retainedBytes` report memory cost. Collector contention
drops a sample instead of blocking storage; `selected`/`dropped` and completed request counts expose this bias. Slow samples are selected extremes, not a
representative distribution and not a source of P99 estimates. Control requests are excluded. The metrics observer never takes the authority mutex.

Alternatives rejected: pausing threads at debugger breakpoints (changes queue behavior), synchronous per-key logging (I/O and unbounded labels), and adding
nested elapsed totals as a CPU/latency breakdown (double counting). Verification: native `request_profile_tests.rs`, server/lifecycle tests, and
`tests/tofudb/test_request_profile_report.py`. Exact windows and search activation: [ownership and evidence](TOFUDB_PIPELINE_CRITICAL_PATH.md). Example after a benchmark run:

```sh
TOFU_DB_REQUEST_PROFILE_EVERY=1 python3 benchmarks/conversation_concurrency.py \
  --binary /path/to/tofu-db --native-client embedded --directory /disposable/volume \
  --conversations 128 --rounds 4 --chunk-bytes 8192 --output /tmp/profile.json
python3 scripts/tofudb_profile_report.py /tmp/profile.json
```

## Eliminating preparation rework and embedded conversion

Problem: independent conversation creation contends on an owner-global count; range witnesses compare physical leaf identities, so an unrelated key's update
or an overlay merge aborts useful preparation. The embedded ABI also parsed, cloned and re-serialized the payload before the shared compiler parsed it again.

Decision: prepared creation retains bounded, scope-owned count/activity effects. Business predicates validate first; the sequencer applies creation metadata before
recency maintenance. A count or activity-index read flushes pending effects on
the original snapshot, preserving read-your-writes and predicate conflicts.
Claim conflicts, overflow and failed commits cannot publish a partial count.

On the default overlay path, changed physical witnesses receive a bounded logical
version check: inspect in-range versions including tombstones, validate old tree
rows and the snapshot overlay against the current head. Overlay rows are checked
even when both tree witnesses are empty, detecting whole-range retirement after
a read. At most one leaf's vectors or 256 overlay key references are temporary;
existing scan, transaction and snapshot limits remain. Mounted directories and
opt-in tombstone reclamation retain conservative physical validation.

The ABI borrows validated raw JSON payload bytes and copies only those bytes
into its admitted job. Shared semantic admission, canonical receipt hashing,
worker cancellation, panic containment and watchdog poisoning remain the owners.
The raw payload has the wire byte cap before allocation; the Rust Value API
retains capped serialization. No storage format or durability mode changes.

Alternatives rejected: dropping count reads without preserving predicates;
ignoring changed leaves without checking tombstones and disappearing rows;
skipping shared dispatch to reduce transport cost. A generation-scoped WAL
suffix-copy experiment also passed its crash matrix but regressed both 1,000
conversation trials (stream P99 16.3–16.9 s versus 10.6–12.9 s). It remains an
isolated experiment; the existing WAL publication/drain protocol is unchanged.

Verification: `entity_optimistic_tests.rs`, recency fault/rollback tests and
embedded ABI receipt/cap/watchdog tests, followed by the native suite and
matched executable/library benchmark receipts.

Measured source: `573d71f7fe5e6e169981eb20f36c6b90169b1669269bfe38cd1cc2ba5841fa41`.
Same-host A/B/B/A comparison uses 1,000 conversations, strict durability, four
8-KiB chunks at one-second arrivals, profile every request, exact readback and
offline integrity. All four runs pass correctness; capacity targets still fail.
Executable/library hashes and raw receipts are retained with the isolated commit.

| Metric | Before (two runs) | After (two runs) |
|---|---:|---:|
| Create point conflicts / 1,000 captured | 974 / 998 | 0 / 1 |
| Stream range conflicts / 5,000 captured | 646 / 576 | 0 / 0 |
| Abandoned stream preparation, cumulative seconds | 11.56 / 9.77 | 1.67 / 1.45 |
| Scheduled stream P99, seconds | 11.92 / 12.03 | 11.62 / 11.80 |
| Admission P99, seconds | 5.06 / 4.88 | 4.20 / 5.24 |
| Settlement P99, seconds | 10.34 / 11.42 | 9.57 / 10.91 |

This removes substantial wasted preparation, not the dominant capacity limit:
about 98.3% of summed sampled dispatch remains identified waits. Read snapshot
acquisition still enters writer FIFO; WAL installation still drains its window.
A separate default-off 5,000-append A/B/B/A check gives 821/832 before versus
896/942 operations/s after, with overlapping P99 ranges (1.80–2.64 versus
2.26 ms). The smaller 1,000-append trial included a slower after P99 (2.90 ms);
both trials remain evidence, so no universal tail-latency win is claimed.
Verification: 1,409 native tests passed, two ignored; 89 neighboring Python tests
passed including real embedded start/restart. These are disposable benchmarks,
not a live deployment or a process-kill capacity certification.

## Profiling evidence and optimization priorities (2026-09-30)

Diagnostic source `48987578274ccb6b06d8bc8bc1ce2c576045d09cac5e70a09860f51b551b8d39` adds observation, not a new storage algorithm. Executable SHA `51f7721f761318f99ebddb4e8da8a51e2369340a2b847516262a3f40ab4eb6aa` and loaded library SHA `787d3c913dfee7246906eff4315d33f791968960d66bcf18601485a538febedf` are verified in every native load report. Runs use strict durability, DolphinFS, four unique 8-KiB chunks per conversation and fixed one-second arrivals. All native staircase runs complete exact readback and offline integrity with zero errors. The shared benchmark checkout still reported the previous native source before integration; the isolated-source/build receipt is the matching provenance. These are diagnostic runs, not new capacity certification.

| Concurrent conversations | Streaming P99 incl. schedule debt (ms) | Identified waits / summed sampled dispatch | Dropped / selected profiles |
|---|---:|---:|---:|
| 32 | 180 | 72.6% | 0 / 384 |
| 128 | 291 | 95.2% | 13 / 1,536 |
| 512 | 3,255 | 98.3% | 22 / 6,144 |
| 1,000 | 11,396 | 98.2% | 80 / 12,000 |

At 1,000 conversations, all 5,000 `turn.event.record` requests are captured:
mean dispatch is 475.5 ms, mean authority hold 1.861 ms, and the separate embedded
worker queue averages 481.8 ms. FIFO admission and rotation parking dominate
exclusive dispatch time. `event.list` and `queue.depth` spend 99.8% of sampled
dispatch waiting, despite mean snapshot holds around 5-6 microseconds. Readers
execute outside authority, but building their snapshot still queues for it.

Shared metadata also defeats useful concurrency: 993 of 1,000 `turn.create_pair`
requests encounter point conflicts (511 of 512 at the preceding load). Creation
reads/writes the owner's shared conversation count; this is a source-supported
candidate hot key, not a captured key-level attribution. Stream events encounter
524 range conflicts and 6 point conflicts, and abandon 10.84 seconds of summed
preparation. Range attribution currently combines overlay changes and tree-leaf
witness changes, so it does not prove all conflicts are false positives.

The 1,000-conversation workload offers 32.768 MB of unique stream text. Complete
workload WAL encoding is 573.783 MB and overlay materialization is 363.456 MB,
including admission, metadata and settlement; 12 checkpoints occur. These are
logical work counters, not same-payload physical-device amplification ratios.
The final rotation fence waits for all staged payloads to drain, so reducing
encoded/materialized work and shortening that fence are connected objectives.

Point-read microbenchmarks expose a different boundary: native `record.get`
end-to-end mean is about 50 microseconds; sampled dispatch mean is 12.21 us and
worker queue mean 6.72 us (2,005 samples including five warmups). All 2,005 entity
reads hit the overlay; no tree resolution or VFS read occurs. Native P99 is
80 us versus raw in-process SQLite's 10 us. That ratio includes native semantic,
JSON and worker boundaries which the raw SQLite baseline omits. Optimizing tree
lookup cannot explain this particular gap. The remaining boundary cost is not
yet a function-level CPU breakdown.

With the same conversation payloads, arrivals, volume and 30-second RPC deadline,
SQLite Sidecar at 128 conversations records streaming P99 8,225 ms and exact
readback; native's repeated 128-conversation runs are 246-291 ms with profiling.
SQLite Sidecar at 1,000 fails with 420 recorded errors, only 655 simultaneous
running conversations and 582 exact recovered contents. That failed run is not
a valid latency speedup denominator. Raw SQLite and SQLite Sidecar remain
separate baselines; missing a native SLO is not evidence of losing every SQLite
comparison. The initial SQLite launch with the new diagnostic env was rejected;
its failure is retained, the native env is now admitted by the deployment guard,
and the successful comparison scopes that env out of the SQLite child.

The 128-conversation off/on/on/off probe-cost test records productive intervals of 4.696/4.636/4.705/4.532 s: ranges overlap, not proof of zero overhead. Retained profile buffers are 99,072 bytes plus 3,944 bytes per active sampled request. Default-disabled hooks are also compared against the preceding build: 1,000 append samples show worse P99 (1.36/1.49 -> 1.68/1.89 ms), while the 5,000-sample follow-up overlaps (2.43/2.67 -> 2.40/2.89 ms); median latency and throughput do not regress in the latter. Keep both observations; there is no all-metric claim. Native tests pass 1,400 with 2 ignored while profiling is enabled. The host's system CPU profiler cannot provide usable symbol attribution (missing initial mappings, then proc-map timeouts/incomplete capture); those captures are retained but excluded from CPU conclusions. Shared-host activity and the absence of running-process SIGKILL/provider/browser coverage remain explicit limits.

Decision: prioritize generation-scoped WAL/payload draining with a short final publication fence; commit-owned deltas for shared metadata plus precise range validation; and removal of repeated JSON conversion across the embedded path. The immutable query view now bypasses writer FIFO capture under the [snapshot read contract](STORAGE_TOFUDB.md#snapshot-read-lane). Embedded transport now borrows runtime bindings instead of copying launch metadata per request; its binding/pin-release stages measure the boundary. Remaining changes need their own before/after experiment and existing conflict/crash/ownership contracts. Measure fence duration, bytes per new stream byte, discarded work, read admission and low-load boundary cost separately. Alternatives rejected: increasing worker count around the same serial gate, relaxing durability, or ranking optimizations only by tree call counts. Current measurements do not justify a promised multiplier for those architecture changes.

Raw reports, failed captures, source receipts and paired observer-cost evidence
are under `.tmp/tofudb-profiling-20260930/`. The retained report command above
reconciles accounting before exposing wait shares and per-operation work.

## Current transaction read/admission boundary (2026-09-30)

Source `8cf81a287d50a9420b9a99e0cca0a17f9c0b78e7350721a35cb4778259e4aa53`
retains transaction-local snapshot values beside their OCC witnesses and waits
for preparation pins outside authority instead of executing pin-rejected
commands under the writer mutex. Six fixed fallback counters distinguish
snapshot/preparation/commit and ordinary WouldBlock/rotation; pin waits have
an independent elapsed counter. Both executable and loaded library report this
source. Binary SHA is
`842ec2045184d2cd03f9bdcf3d189b6c9dc034d8f221e3899bd8d5ffb85e02e2`;
library SHA is
`22e2f090e7d9ec308d5f31fd5d71de70cf9c8fd745a4782490dd69149154f48d`.

The before/after/after/before comparison uses DolphinFS, strict durability,
1,000 simultaneously running conversations, four unique 8-KiB chunks at fixed
one-second arrivals, and equal 30-second deadlines. Baseline source
`ee62e08cd7a9bbf9b516174b0e72643bdfc6664979bdfcd3e8bfb0593202cf03`
adds fallback attribution only. All four runs complete exact 1,000-conversation
readback and offline integrity verification with zero request errors.

| P99 metric (seconds) | Before, two repetitions | Final, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 4.519 / 5.003 | 4.980 / 4.834 | 5 |
| Streaming including queue debt | 12.508 / 12.568 | 11.497 / 10.958 | 1 |
| Terminal settlement | 11.598 / 11.693 | 11.892 / 10.302 | 5 |

Streaming improves about 8-13%; admission and settlement show no consistent
latency improvement. Capacity acceptance remains **FAILED**. Embedded
abandon/reopen is not running-process SIGKILL coverage. Combined embedded/client
peak RSS falls from 1,292 / 1,303 MiB to 1,217 / 1,179 MiB. Fallbacks fall from
5,245 / 5,377 to 2,793 / 2,973: snapshot-capacity fallbacks fall from 2,783 /
2,815 to zero. Final commit WouldBlock fallbacks remain 1,634 / 1,692;
snapshot-plus-commit rotation fallbacks remain 1,159 / 1,281. These counters
identify branches, not per-request wall-time shares. Fixed cumulative timers
are nested and cannot be added as independent foreground costs.

The cache-only intermediate runs did not establish a material latency win and
remain in the evidence. The paired 1,000-append follow-up records worse P99
(1.722 / 2.309 ms before, 2.473 / 3.604 ms final). A subsequent same-history
5,000-append comparison in the same interleaved order records overlapping P99
(2.029 / 2.180 ms before, 2.149 / 2.075 ms final). This does not erase the
smaller-sample regression or establish improvement in every dimension.

All 16 final operation phases complete for native, sidecar SQLite and raw
SQLite. Native point-read P99 remains 0.086 ms versus raw SQLite 0.010 ms;
durable footprint is 113.37 / 59.37 / 20.15 MB respectively. Small-suite tail
sample-sufficiency flags remain binding. Logical WAL traffic is 567 / 535 MB
and overlay materialization 364 / 341 MB in the two final capacity runs;
these are not physical device write-amplification measurements.

Final validation passes 1,393 native tests (2 ignored) and 78 Python tests.
An earlier full run failed because the shutdown-checkpoint fault test did not
reach its requested metadata-injection occurrence; three isolated repetitions
and the final full rerun pass. The original failure is retained. Existing
repository documentation gate failures are reported separately. Build receipts,
all failed/intermediate runs, complete scores, commands and integration backup
live under `.tmp/tofudb-read-reuse-20260930/`; missing raw evidence must not be
reconstructed from rounded summaries. The ownership/memory/error invariants and
rejected alternatives are documented in `STORAGE_TOFUDB.md`.

## Embedded measurement contract

The active-conversation and operation harnesses both accept
`--native-client embedded` to exercise the loaded native library through the
production Python adapter. Reports identify the executable, library file hash,
and the library's own source digest; an executable receipt alone does not prove
which embedded engine was measured. The application/daemon path remains a
separate selectable transport. In-process abandon/reopen is labeled explicitly
and cannot satisfy the process-crash coverage gate.

All active-conversation authorities now receive the same 30-second RPC deadline
from `conversation_concurrency_contract.json`. Startup and restart verify the
effective client deadline. The 5/1/5-second admission/stream/settlement P99
budgets remain unchanged. Earlier embedded comparisons silently inherited a
30-second native default and a 5-second legacy-sidecar default; those failed
legs remain retained but are invalid for engine ranking. A failed legacy
adapter request does not prove a limitation of the SQLite engine.

Capture the complete content-free storage metrics envelope at admission and
completion even without a running restart. Filtering out packer pacing and
maintenance concealed possible causes of rotation stalls. Embedded engine and
client share a process: their RSS observations must not be added together.
Small operation-suite sample counts remain explicitly insufficient for P99
certification where indicated; they cannot replace the fixed-arrival
1,000-conversation gate or turn an incomplete run into a latency win.

## Previous measured capacity boundary (2026-09-30, core write path)

That comparison fixes distinct-key overlay batching and removes global
structural drains from normal single-event projection-head cleanup. Baseline
source `94a908f3d3c83804…` adds counters only to the previous final source;
final source is `8bb8457bc12df8de…`, binary SHA
`0bf60538db1f6a609d2ee004289ce916d44453900dd547d0f7611450f78ff425`.
Both builds use identical instrumentation and workload paths except that the
new historical-position counter is absent from the baseline. Native before /
after / after / before runs use DolphinFS (`fuse.bgfuse`), strict durability,
1,000 running conversations, four unique 8-KiB chunks at fixed one-second
arrival intervals, and running/final SIGKILL recovery. No latency budget changed.

| P99 metric (seconds) | Before, two repetitions | After, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 12.653 / 12.084 | 4.218 / 5.115 | 5 |
| Streaming including queue debt | 27.676 / 27.038 | 16.812 / 17.235 | 1 |
| Terminal settlement | 22.928 / 26.705 | 17.180 / 15.642 | 5 |

Overall capacity acceptance remains **FAILED**. Admission's second repetition
still exceeds its budget; both stream and settlement repetitions fail. All four
runs recover 1,000/1,000 exact completed contents and report zero unhealthy
probes. Native peak RSS falls from 1,734 / 1,723 MiB to 1,524 / 1,602 MiB.

Admission merges fall from 955 / 889 to 51 / 59 and encoded WAL bytes from
285 / 275 MB to 98 / 106 MB. In the restarted streaming/settlement epoch,
encoded WAL bytes fall from 787 / 770 MB to 480 / 461 MB; checkpoints fall
from 19 / 19 to 12 / 12. These are decimal MB and process-wide counters, not
physical write amplification. Normal terminal commits no longer perform
foreground tree batches, but semantic execution, background materialization,
and rotation parking still serialize visible progress. The intermediate
batching-only run and every failed final leg remain in the evidence bundle.

All 16 small-scale operation phases complete for TofuDB, SQLite sidecar and
raw SQLite. Native swarm-reader P99 is 3.54 ms versus sidecar 7.14 ms, but
history-bearing sidebar P99 is 3.193 ms versus 2.504 ms. Native durable footprint
is 115.58 MiB versus 56.68 MiB. These dimensions cannot be averaged into a win.
The 30-sample mixed-suite large-append P99 is 23.048 ms (previous suite 3.716 ms).
A fresh-authority 1,000-sample controlled large-append follow-up measures
baseline/final P99 4.535 / 4.578 ms and throughput 333.3 / 356.3 operations/s;
this does not erase the mixed-suite outlier or certify every history state.
Raw SQLite's orderly reopen is not compared with destructor-free crash recovery.

Final native tests: 1,358 passed, 2 ignored; benchmark verdict tests: 14 passed.
Fault tests cover exact merge coverage, checkpoint recovery, pinned snapshots,
owner isolation, concurrent insertion and atomic settlement/head deletion under
I/O failures and short writes. This is bounded synthetic-provider evidence,
not browser feedback, real-provider capacity or a sustained soak. Raw runs,
commands, identities and the complete operation comparison live under
`.tmp/tofudb-core-20260930/`; do not reconstruct missing raw evidence from
these rounded summaries.

## Previous write-path comparison (2026-09-30)

The source-snapshot comparison on DolphinFS (`fuse.bgfuse`, strict durability)
uses source digests `51eb5bc7ebf29f2b…` before and `7712a958e49a2c40…` after
blob staging, search scheduling, witnessed reads, and connection teardown fixes.
The final binary SHA is `a2655d187b2c4352c4dee77f3ce29089ba8124a3252ff36e0e113e612676c948`.
Two 1,000-conversation repetitions per build ran in before/after/after/before
order, with four 8-KiB streaming events per conversation, scheduled arrivals,
restart while running, and exact-content recovery. These gates remain **FAILED**:

| P99 metric (seconds) | Before, two repetitions | After, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 12.04 / 11.61 | 12.07 / 15.20 | 5 |
| Streaming including queue debt | 34.57 / 32.49 | 21.14 / 27.26 | 1 |
| Terminal settlement | 27.47 / 20.84 | 19.36 / 17.35 | 5 |

All four runs recovered 1,000/1,000 exact contents with zero unhealthy probes.
This proves the measured recovery scope, not low-latency capacity or a sustained
soak. Admission did not improve; its second final repetition regressed. Serialized
command execution and rotation waits remain material bottlenecks. Nested stage
counters overlap and are not an additive foreground latency breakdown.

All 16 operation phases completed on the final build. Search corpus readiness was
8.0 s versus sidecar SQLite 10.8 s; native large-turn append P99 was 3.72 ms.
Native shutdown fell from ~31.6 s to ~1.62 s with both listener and client-pool
cleanup fixes, but this phase has only three observations. Concurrent-write reader
P99 was 13.72 ms versus SQLite 6.24 ms, and native data size was 141.24 MiB versus
56.70 MiB. Neither dimension passes. The native process's FUSE write counter
reported only 16 KiB: it cannot certify physical write amplification.
Raw runs, exact commands, build receipts, all operation scores, and failure
verdicts are retained in the ignored `.tmp/tofudb-latest-opt-20260929/` evidence
bundle; missing artifacts must not be replaced by these rounded summary numbers.

## Write-path diagnostic accounting

`commitStages` retains the aggregate `parallel_fallback` count and partitions it into `parallel_{snapshot,preparation,commit}_{busy,rotation}`. These six fixed counters record the actual fallback branch and have zero elapsed time; `busy` means `WouldBlock`, not necessarily a business OCC conflict. Rotation uses the engine's existing classified backpressure. Unsupported operations that never enter optimistic preparation do not increment these counters. Problem: the old aggregate conflated pin admission, preparation and commit failures. Decision: attribute both phase and classified rotation without changing retries or errors. Alternatives rejected: parsing arbitrary error messages into guessed conflicts and adding per-key labels/unbounded metrics. Preparation pin saturation now waits outside authority; `parallel_snapshot_wait` records those wait intervals separately from commands that actually fall back. `parallel_commit_rehearsal` and `exclusive_rehearsal` time the reads rehearsed off the writer before a command queues for it, and `parallel_commit_refused_off_lock` counts commits that rehearsal proved refused, which therefore never queued (they are also `parallel_commit_busy` fallbacks); see [reads rehearsed before the writer](TOFUDB_WRITE_PATH_OVERLAY.md#reads-rehearsed-before-the-writer). `overlay_validate_pending_only` counts validations decided from pending overlay writes alone, which read no page; [validation from pending writes](TOFUDB_WRITE_PATH_OVERLAY.md#validation-from-pending-writes).

`worker_job.claim_next` is admitted to optimistic preparation as one existing
IR step. Queue summaries, queued/lease ranges and job documents retain their
ordinary entity OCC witnesses on a preparation replica with the live worker
clock. A queue-head change refuses commit and uses exclusive execution; source
faults, response bytes and durability behavior are unchanged. The disposable
cold-read gate in `server_worker_job_preparation_tests.rs` proves a concurrent
queue write can commit during preparation and that exact fallback ordering and
read-fault output match the exclusive reference. This certification is not a
production latency measurement or a claim that heartbeat latency is solved.

`conversation_concurrency.py` captures storage metrics once at the admission
barrier and again after streaming/settlement. A running restart resets native
process counters, so these epochs must be reported separately. Counters cover
all engines in the process, including the disposable search projection; they
are not per-authority physical-device I/O measurements.

- `walEncodedBytes`: successfully appended WAL bytes before durability barriers;
  neither device write amplification nor a count of acknowledged durable bytes.
- `overlayMergeCommits` / `overlayMergedEntries` / `overlayMergedPositions`:
  successful physical merges, distinct keys materialized, and historical delta
  positions consumed. Their ratios expose repeated-key batching failures.
- `overlayMaterializedBytes` / `overlayMaterializedBlocks`: newly staged page
  bytes and referenced blocks of successful merges. Reused durable blocks add
  references without newly staged bytes.
- `overlayWalPressureAttempts` / `overlayWalPressurePendingEntries`: hard
  backstop attempts made with a WAL above the overlay watermark and the summed
  pending-position counts seen at those attempts; attempts may fail.

Always retain the source/binary identity, resource budget, latency distribution,
correctness verdict, and original scheduled arrival model beside these counters.
Nested execution timers overlap and cannot be summed as independent elapsed time.

## What an agent harness actually does to its storage

The benchmark workload model is the repository's own operation mix, not a
synthetic KV loop. The dominant classes, in rough order of user-visible impact:

1. **Turn writes** (`turn.append_settled`, `turn.compact`, `turn.delete`) —
   the hot write path; every agent round trip lands here. Small, durable,
   strictly ordered per conversation.
2. **Sidebar / history reads** (`conversation.list`, `conversation.get`,
   windowed `turn.sync.snapshot`) — read on every UI paint and resume; latency
   here is directly perceived. Real conversations carry turn history, so the
   corpus must include history-bearing conversations, not headers only.
3. **Background state** (`record.put/get`, queue ops, timers, billing,
   scheduler) — small point reads/writes, latency-tolerant but throughput-
   sensitive under concurrency.
4. **Lifecycle maintenance** (checkpoint, reopen, GC, backup) — must be
   bounded and must not stall foreground work.

This is a **single-writer, append-heavy, read-your-writes, low-GB** workload.
It is *not* a general OLTP or analytics workload. A general-purpose database
(SQLite, PostgreSQL) pays for generality (SQL parse/plan, generic index
maintenance, multi-user concurrency control) that this workload never uses.

## The benchmark

`scripts/tofudb_perf_compare.py` drives **identical operation mixes through
each backend's production client stack** — not raw engine primitives:

The script owns adapters, engine lifetime, CLI selection and measurement/report
policy. `scripts/tofudb_workloads/` owns the ordered workload catalog and its
record, history, search, concurrency and recovery implementations. Problem:
one closure mixed every corpus and lifecycle while hiding shared captures.
Decision: explicit invocation inputs carry owner, namespace, scale and timestamps;
execution and summary ports preserve the existing adapter and clock boundaries.
The catalog retains phase order and independent Turn identities. Alternatives
rejected: separate benchmark loops would drift from the common measurement
owner; moving the complete closure intact would leave the mixed responsibilities.
The existing certification source inventory includes the workload package,
including local edits and new files. Old frozen receipts remain historical;
omitting the new source inputs cannot qualify a report as the current checkout.

`--output` is optional and names one new operation report for this invocation,
not an update or resume destination. An occupied file, directory or symlink
(including a dangling symlink) is refused before database reset, provenance
capture or engine launch. That preflight is advisory: after engine teardown,
the complete JSON is staged and synced under a short independent name in the
existing output parent, then published with an atomic no-replace hard link and
parent-directory sync. A racing writer cannot replace an accepted report.
Serialization, write, sync or link failure propagates without a replacement/copy
fallback or a success
message. A post-publication sync or cleanup failure can leave a complete report
at the target while the invocation fails; it does not establish that no engines
ran. An interrupted writer can leave an unpromoted `.partial` file. The existing
report schema and nonzero status for retained workload/startup/witness failures
are unchanged. This single-report rule does not change incremental publication
or explicit reuse in other runners.

* `tofudb` — the native production stack: either a `tofu-db serve` process over
  storage.v2 or `--native-client embedded` through the loaded C ABI and Python
  adapter. Every report records the chosen transport.
* `sqlite` — the legacy Python sidecar authority, SQLite backend
  (`lib.storage` supervisor + `lib/storage/client.py`), as personal mode runs.
* `rawsqlite` — bare in-process SQLite (WAL, `synchronous=FULL`), the same
  logical operation shapes executed as direct SQL with no supervisor,
  socket, or serialization layer. This is the **engine-floor baseline**,
  and it is mandatory in every comparison run: it bounds how much of each
  production stack's latency is its client and process boundary rather
  than its storage engine, and it retires the out-of-band micro-benchmark
  that once showed raw SQLite ~25× ahead per commit (see "Reconciling the
  raw baseline" below).

It reports per-workload P50/P95/P99, throughput, time-windowed samples and OS `/proc/<pid>/io` serving-window writes. `resources.<engine>.footprint` separately accounts for authority and external search projection: logical bytes, allocated bytes when supported, unique files, overlap, scan errors and complete/observed totals. The old `data_size_bytes` is only apparent path bytes under the original driver root; it excludes the external native projection and is not the total-footprint gate. Failed/unresolved scans cannot become complete totals. SQLite's broad project directory is not silently relabeled as a precise authority/projection inventory.
Footprints are post-workload, before-shutdown observations, not live-payload bytes or verified steady state. Maintenance/reopen evidence is retained separately; waiting for background work or seeing a checkpoint does not establish steady state. Native reports declare write-counter scope/completeness; embedded open/close/abandon/reopen windows fold once before resetting.
In-process counters cover the harness: an upper bound, not device IO or isolated database amplification. Daemon startup/shutdown tails and separate authority init are excluded.
Verified lifecycle accounting invalidates historical embedded totals reset across reopen; latency and footprint remain separate evidence.
PostgreSQL is not a benchmark dimension (see below).

Operation-runner durability qualification with `--capture-provenance` retains
`writer_configuration_at_open` from the existing sidecar metrics receipt: the
adapter reads PRAGMAs on the actual writer connection before handing it to the
writer thread. A selected factory or an observer connection is not observation
of that writer. Both before/after boundaries must observe native strict mode,
SQLite writer WAL/FULL with fastpath inactive, and raw SQLite WAL/FULL for
`measurement.equivalent_durability=true`. Missing observations yield `null`
(unknown), not evidence of unequal durability; observed mismatches yield `false`
and cannot qualify. The report keeps the raw writer receipt and derives its
SQLite synchronous/journal fields only from that receipt. No new SQL enters a
timed loop or observer connection, and no durability setting changes. This is a
measurement-control repair, not a new score or performance/default verdict.
DolphinFS comparison remains pending independent preflight; synthetic controls
do not supply mount evidence or revise prior XFS analysis.

Generic operation timing is execution evidence, not result fidelity. Within the
existing `tofu.perf_compare.v2` report, `record_get_points` now retains
`workloads.<name>.<engine>.result_evidence` with format
`tofu.seeded-record-results/v1`: namespace, seed count, original seed-write
receipts, warmup count, explicit bare/versioned response shape, and one ordered
`{iteration, request: [operation, payload, command], result}` per timed sample.
The oracle reconstructs every seeded key and complete stored value from the
existing workload inputs; versioned reads must also match the original write
receipt's version and update time. Both metadata fields must satisfy the
canonical positive-u64 domain (`1..2**64-1`), excluding booleans, as declared in
`contracts/tofudb_ir_v1.json`. Every versioned seed receipt is validated,
including keys not referenced by timed samples: exact receipt shape,
deterministic key, and positive-u64 metadata are required before sample checks.
It checks the exact timed population against `iterations` and the ordered
latency sample count. Clock evidence must be non-boolean, nonnegative and finite
within the collector's binary64 range; oversized JSON integers produce a
controlled refusal, not an integer-to-float conversion failure. Requalification
also recomputes every scalar latency/throughput summary from those retained
clocks using `scripts/tofudb_workloads/latency.py`, the same pure owner used by
the collector. Missing or inconsistent mean, percentiles, extrema or throughput
refuse qualification before either console or offline ratios. Existing rounding
and percentile-rank rules are unchanged; this work is outside timed intervals. Missing/error/unsupported,
malformed or mismatched evidence cannot admit an operation ratio. The offline
charter assessor and console speedup summary each recompute this prerequisite
from raw evidence for the particular engine pair, not from the collector's
additive `operation_result_comparison` summary. An unqualified console row
prints the refusal reason without a ratio or ranking label.

Problem: timing alone previously discarded request results. Decision: support
only this justified point-read oracle; retain full responses and compare their
finite JSON exactly, preserving types, list order, duplicates and metadata.
Object key order is not semantic. Alternatives rejected: a generic digest
would not establish correctness; stripping record envelopes or timestamps would
hide real response differences. Consequently production timestamp differences,
or the raw floor's deliberately narrower response shape, refuse exact-result
qualification rather than becoming an apparent equality. Other generic phases
(including mutations, search and custom concurrent/recovery runners) have no
new oracle and remain unqualified for operation ratios. Charter witness
collectors retain their existing behavior, including their independent raw
receipts; the assessor now also withholds witness performance verdicts until a
justified result oracle is wired through those custom collectors. Existing
failure/refusal evidence is retained, not replaced by a point-read receipt.
Footprint accounting is separate and unchanged.

Request snapshots are taken before the start clock; result retention follows
the end clock; validation/comparison follows the measured loop. Both timing arms
still contain only the same logical execution call and clock boundaries. Seed
receipt capture changes no setup operation, payload or count. Small point-read
copies add untimed collector work and retained bytes; summed-request latency
still excludes that work and is not end-to-end throughput. Synthetic tests of
this gate are not new engine measurements, a filesystem result, or a performance
or ranking claim. Existing SQLite WAL/FULL observations and the retained
DolphinFS refusal are neither replaced nor relaxed.

Disposable native authorities own a private parent containing their data
directory and the engine's default sibling backup directory. Problem: owning
only the data directory let independent runs share backup state and left
synthetic backups behind after shutdown. Decision: the existing engine lifetime
owns the parent in both daemon and embedded modes, including failed startup.
Alternative rejected: deleting the caller's shared backup directory could
destroy another authority's recovery points. Production backup policy and the
declared authority/projection footprint scope stay with their existing owners;
[storage operations](STORAGE_OPERATIONS.md) defines the cleanup verification.

Concurrent measurement sessions have one lifecycle owner. Problem: opening or
closing a per-thread session outside the worker's error boundary could leave a
partial reader population reported as successful timing. Decision: acquisition,
execution and owned-session cleanup all contribute failures to the phase;
cleanup failures retain the original operation failure as well. Workers borrow
an engine without an `open_session` port and never close that shared owner.
Every declared reader and writer operation must be sampled before a phase can
report success. Alternatives rejected: thread warnings are not a measurement
verdict, and accepting only the surviving readers changes the measured workload.
Verify `tests/tofudb/test_comparison_worker_lifecycle.py`.

`WorkloadWorkers` also owns the explicit phase carriers and start barrier.
Problem: partial startup left accepted workers behind the barrier; a join
deadline could return while sessions still borrowed the engine. Decision:
startup failure, deadline and coordinator interruption fence subsequent work,
release the barrier, and await accepted callbacks and their session finalizers
before returning or re-raising the original exception. The measurement deadline
invalidates timing; it is not permission to abandon in-flight calls, which keep
their existing operation deadlines. Completion receipts remain independent of
an interrupted thread join. A late bootstrap is fenced before session access;
an abrupt worker exit invalidates timing while retaining its diagnostic hook.
Alternatives rejected: closing a shared engine from a worker breaks ownership;
a general task pool adds scheduling and startup-retry policy to the declared
benchmark carriers. Verify `tests/tofudb/test_comparison_phase_lifecycle.py`.

### History-scaled phases

Two phases exist specifically because header-only corpora are blind to the
read paths production actually exercises:

* `open_windowed_long_history` — one conversation carrying `history_turns`
  (400 at `small`) turns; the measured op is the production open path,
  `turn.sync.snapshot` with `turn_limit: 20`. Any O(history) work behind an
  O(window) contract shows up linearly here.
* `sidebar_with_history` — `sidebar_hist_convs` conversations carrying
  `sidebar_hist_turns` turns each; the measured op is `conversation.list`
  with limit 50. Per-conversation history work inside list/hydration scales
  with summed history, not page size.

Corpus-recency discipline: list workloads return the newest conversations by
`updated_at`, so a phase's seeded conversations must carry the newest
timestamps in the run. A phase seeded with older timestamps silently
re-measures whatever newer (header-only) corpus an earlier phase left
behind — this actually shipped once (2026-09-19: `_SEARCH_TS`-based turns
ranked below `_TS_BASE`-based header seeds) and inverted a sidebar result
until caught. Any new list workload must state which conversations land in
the measured page and why.

### Cold-read families (2026-09-16/18 storm cases)

The `maintenance_reopen` phase additionally probes the boot warm-up family
set (`record.get/list`, `conversation.count/list`,
`task_results.recovery_pending`, `timer.active.count`, `credential.list`)
with a **cold read immediately after each restart and a warm read right
behind it**. The report carries per-family `cold_read`/`warm_read` latency
summaries; the timeout storm's signature is a cold read orders of magnitude
above its warm twin on a high-latency volume. An engine lacking a family is
reported as an explicit `cold_read_unsupported` gap, never a dropped sample.
Only the adapter's typed unsupported-operation signal can establish that gap,
before any sample for that optional family exists. I/O, timeout and recovery
errors fail the phase. `record.get` is mandatory in every round; a prior
round's sample cannot stand in for a failed first read. Both cold and warm
point reads must return the exact seeded JSON value; verification occurs
outside the latency clock, and missing/changed data fails the phase. A capability that
disappears after a successful sample also invalidates the phase.
The existing charter assessor treats any native/SQLite cold-read gap as
insufficient maintenance evidence, including older receipts that mislabeled
an execution failure. A favorable reopen number cannot override that gap.

The deterministic half of the same incident is gated in CI without any
wall-clock assertion: `packages/tofu-db/src/cold_read_tests.rs` counts VFS
io-ops (open/pread, i.e. potential network round trips) for a cold
`record.list`/`record.get` on a checkpointed store and asserts the ratchet
ceilings (11 / 3 io-ops, 0 warm). A regression in the cold path fails the
gate; every cold-path optimization must lower the ceilings in the same
commit. Wall latency per io-op stays environment-specific and belongs to
this harness's `cold_read` numbers, measured never gated.

The degraded-filesystem case is one command away — point the disposable
authority at the network mount:

```bash
mkdir -p data/benchmarks/fuse-parent
python3 scripts/tofudb_perf_compare.py --scale small --engines tofudb \
  --binary packages/tofu-db/target/certified/tofu-db \
  --authority-dir-parent data/benchmarks/fuse-parent \
  --output data/benchmarks/cold-read-fuse-$(date +%F).json
```

A healthy small-scale authority stays sub-millisecond even on FUSE (WAL
replay pre-warms it); the storm needs a store larger than the 32 MiB block
cache, which the Rust gate above reproduces deterministically instead of
seeding gigabytes into every benchmark run.

## Search preparation comparison boundary

Problem: periodic `turn.search.backfill` requests reset the SQL projection and
its cursor; accumulated corpora can repeatedly restart before becoming visible.
Decision: the benchmark schedules one rebuild and waits for both seeded hits
and ready complete-corpus coverage under the existing total preparation deadline.
It samples coverage before all hits appear and reports stabilization separately.
Alternative rejected: a 90-second unchanged coverage timeout does not witness a
stalled worker; diagnostic counters showed continued processing and no failures.

## Win conditions — explicit, falsifiable, per-workload

Tofu-DB is "comprehensively better" only when **all** of these hold, measured
on the same hardware in the same run, against a freshness-verified binary:

| Dimension | Gate | Why this bar |
|---|---|---|
| **Interactive write** (`turn.append_settled`, `record.put`, `conversation.create`) | P99 ≤ sidecar-SQLite P99 | Writes are the harness's hot path; we must not regress the perceived responsiveness of the agent loop. |
| **Interactive read** (`conversation.list`, `conversation.get`, `record.get`) | P50 ≤ sidecar-SQLite P50 | Read latency is what the user feels on every paint. |
| **History-scaled read** (`open_windowed_long_history`, `sidebar_with_history`) | P50 ≤ sidecar-SQLite P50, flat vs history length | The real sidebar/open corpora carry turns; wins here matter more than header-only micro-ops. |
| **Search** (`conversation.search`) | query P50 and complete-corpus visibility time ≤ sidecar-SQLite | Report indexing stabilization separately; a fast query after minutes of unmeasured preparation is not a search win. |
| **Mixed 80/10/10** | throughput ≥ sidecar-SQLite | The realistic blended load. |
| **Footprint** (complete authority + projection logical bytes; allocation/write counters reported separately) | total data size ≤ **1×** sidecar for the same logical corpus and lifecycle phase | Smaller is better, full stop: every durable byte is FUSE pain, backup time, and cache pressure, bounded against runaway growth (AGENTS.md constraint 3). A purpose-built store has no excuse for a larger footprint than the embedded engine it replaces — parity is the loosest acceptable bound, and every byte above it must carry a named structural cause in this charter. |
| **Engine floor** (`rawsqlite` baseline) | every operation comparison includes raw SQLite; P50 > 2× names its structural cause but still fails the numeric gate | The client stack is ours and the engine is ours; an unexplained gap to the bare embedded engine is unpaid debt, and it must stay visible in every baseline table instead of hiding behind sidecar comparisons. |
| **Maintenance** (checkpoint + reopen) | wall ≤ sidecar-SQLite | Bounded, non-blocking lifecycle. |
| **Degraded filesystem** (FUSE/network mount) | **strict win** | Verify locking, acknowledged recovery, and latency on the actual mount for both engines; the FUSE label alone proves neither failure nor superiority. |
| **Distracted hardware** (CPU < 4 cores, memory pressure) | no P99 blow-up vs idle | A personal PC is not a server; the OS and browser compete. |
| **Horizontal** (writer + N concurrent readers) | reads must not block writer | WAL-readers + single-writer is the harness's real concurrency shape. |

| **Swarm-write tails** | writer and reader P99 ≤ sidecar-SQLite with sufficient equal populations | Fast writer averages cannot hide reader starvation. |
| **Active-conversation capacity** | complete paired application acceptance below | Storage micro-ops do not prove 1,000 running attempts. |
A single global "N× faster" number is meaningless and is explicitly rejected:
it lets a 50× search win hide a 25× write regression. Each dimension must pass
on its own.

### Executable assessment, not another benchmark

[`storage_performance_charter_v1.json`](../contracts/storage_performance_charter_v1.json) owns the operation-to-dimension rules. `scripts/tofudb_charter_assess.py` reassesses raw receipts without launching an engine or opening a database: `python3 scripts/tofudb_charter_assess.py <operation-report.json> ... <paired-report.json> --json-output <new-assessment.json> --markdown-output <new-assessment.md>`. Exit 0 means every gate passes; 1 retains non-pass evidence; 2 refuses malformed input or conflicting output. Existing outputs are never overwritten with different content.

For new operation receipts, explicitly add `--capture-provenance --native-client embedded --charter-witnesses all` to `tofudb_perf_compare.py`; collect all three engines on both actual volumes, at least two independent same-work repeats per volume with reversed engine order. Capture occurs outside timed work. Charter witnesses run after every timed phase and before the closing source/artifact capture, on fresh disposable authorities started from the receipt's pinned executable/library under the same authority parent; each witness authority records its transport, artifact SHA-256 and volume device/filesystem, and the assessor counts it only when those match the receipt's own `native_binary_sha256`, embedded library and `resources.<engine>` volume (otherwise INCOMPARABLE). It reuses native build-info/fingerprints and the certification source inventory, plus measured benchmark, delivery, executor and resource-policy owners. Binary/library bytes, complete start/end source inventories, observed strict/FULL/profile-off policy, host/affinity, work and actual volume devices must agree; requested settings or a supplied `passed` boolean are not proof.

The five outcomes are **PASS**, **FAIL**, **INSUFFICIENT**, **INCOMPARABLE**, and **HISTORICAL**. Each dimension retains observations and reasons; no averaging erases an adverse repeat. Stable old evidence remains historical, not a current win or current regression. Missing tails, coverage or provenance remain explicit. History-length scaling, actual-volume locking and process-crash recovery, idle-versus-pressure P99 and writer-versus-reader scaling are judged from those joined witnesses (rules in [Joined charter measurements](#joined-charter-measurements)): a missing witness keeps its clause INSUFFICIENT, a failed native witness leg is a FAIL, and a failed sidecar leg leaves the comparison INSUFFICIENT. The charter sets no numeric no-blow-up budget, so complete distracted-hardware cohorts report their P99 ratio and stay INSUFFICIENT (budget requires owner decision). A policy `unautomated_criteria` entry still keeps its dimension INSUFFICIENT even when numeric subgates pass. Raw SQLite has narrower semantics: the 2× diagnostic excludes incompatible lifecycle boundaries, not lifecycle acceptance. This assessment is neither power-loss certification nor a deployment decision.

Verification: `tests/tofudb/test_tofudb_charter_assess.py`, `test_charter_witnesses.py`, `test_tofudb_footprint_accounting.py` and the conversation provenance/acceptance tests. Correctness tests use synthetic metrics, never wall-clock performance thresholds. A green test suite proves assessment behavior, not that measured product gates passed.

## Joined charter measurements

**Problem.** Four win conditions — history-scaled reads flat versus history length, locking and acknowledged recovery on the actual mount, no P99 blow-up on distracted hardware, and reads that do not block the writer — existed only as prose `unautomated_criteria` in the assessor policy. They kept `history_read`, `degraded_fuse`, `distracted_hardware` and `horizontal_reads` INSUFFICIENT whatever the numeric subgates said, and no receipt measured them on its own artifact and volume.

**Decision.** `tofudb_perf_compare.py --charter-witnesses all` writes one witness per clause into the same `tofu.perf_compare.v2` receipt (`charter_witnesses`), measured by [`scripts/tofudb_workloads/witnesses.py`](../scripts/tofudb_workloads/witnesses.py) on fresh disposable authorities beside the measured ones. Witness parameters (swept lengths, CPU count, reader sweep, workloads) are declared once in the policy contract and read by both the harness and the assessor. Every rule derives from an existing gate — the sidecar measured in the same run, or native's own repeat-measured range — and adds no numeric budget. A witness leg that raises is recorded for that engine only.

| Clause | Witness | Assessor rule |
|---|---|---|
| `history_read`: flat versus history length | `history_sweep`: the catalog's own `open_windowed_long_history` and `sidebar_with_history` corpora grown in place through 1×/2×/4× the scale's history length (at `small`: 400/800/1,600 turns in the opened conversation; 10/20/40 turns in each of the 50 listed conversations). Each length holds exactly the corpus the phase seeds at that length; one authority per engine; engines alternate at every length. | At every length native P50 ≤ sidecar P50. At every longer length native growth from the shortest length stays inside native's repeat range there, or is no steeper than the sidecar's growth in the same repeat. At least 3 increasing matched lengths; repeats must agree. |
| `degraded_fuse`: actual-volume locking and acknowledged process-crash recovery | `volume_recovery`: a second authority on the live authority (a second `tofu-db serve` on the same `--data-dir`; a second production `StorageSupervisor`, whose OS lease probe must defer), then `maintenance_rounds` SIGKILLs of the separate engine process while a `record.put` is outstanding — the receipt's pinned executable as `serve` (the production transport on network mounts) and the sidecar's storage child — each followed by reopen, exact read-back of every acknowledged value, the outcome of the unacknowledged write and one post-recovery write. | Native FAILs on an admitted second authority, a lost or changed acknowledged write, a changed unacknowledged write, a failed reopen or an unwritable recovered authority; INSUFFICIENT without a lease-attributed refusal, a separate-process SIGKILL or an outstanding write at the kill. The sidecar's witness must exist; its own failures are reported, not charged to native. FUSE receipts only. |
| `distracted_hardware`: no P99 blow-up versus idle | `distracted_cohorts`: all harness threads, the witness engine processes and one stressor per CPU confined to the 2 highest allowed CPUs (the charter's fewer-than-4 cores); the stressors stream harness-owned buffers totalling the set's last-level cache (cache and memory-bandwidth contention; the shared host's RAM is not reduced). Blocks run idle/pressure/pressure/idle, 500 iterations each of `turn_append_burst` and `conversation_open`, so each cohort holds the certification contract's 1,000 tail samples. | Confinement, stressor overlap on the same CPUs, interleaving and sample counts are validated; complete cohorts report native (and, as context only, sidecar) pressure/idle P99 ratios and stay INSUFFICIENT: **budget requires owner decision**. |
| `horizontal_reads`: reads must not block the writer | `horizontal_scaling`: `concurrent_writes` `record.put` writes with 0/1/2/4 concurrent readers in mirrored blocks (0,1,2,4,4,2,1,0); every reader has completed a read before the writer clock starts, and reads starting inside the writer window are counted. | For every N, native writer slowdown versus writer-only stays inside native's writer-only repeat range, or is no worse than the sidecar's WAL-reader slowdown in the same repeat; readers must overlap the writer. |

**Alternatives considered.** Fixed budgets such as "at most 10 % growth" were rejected: the charter defines none, and an invented number would decide the verdict. A separate witness script or receipt was rejected because joining by convention cannot prove the same artifact and volume. Embedded abandon/reopen is excluded by the charter itself; [`benchmarks/embedded_process_recovery.py`](../benchmarks/embedded_process_recovery.py) kills a quiescent embedded process after a flushed manifest, not mid-write, and stays a standalone durability check. RAM-capacity pressure through a memory cgroup is unavailable to an unprivileged process here and would reach other tenants of the shared host. A fresh authority per history length was built first; growing one corpus through the lengths yields the same per-length corpus with 4/7 of the seeding writes on the saturated local RAID. Host-memory pressure stays bounded by design: the stressors own 48 MiB on this host (the CPU set's last-level cache), released when each pressure block ends.

**Measured evidence (2026-11-12, 22:40–01:15 CST).** Lane `perf/tofudb-charter-joined-20261112` at `d8d6efb77a`; release engine built from the lane (`cargo build --release --locked --features embedded-mimalloc`) and certified with `serverctl_pkg.native_engine --prepare-only` (executable `20992a09…`, library `648ae761…`, engine source `f93e93f9…`, unchanged by this work). `--scale small --native-client embedded --capture-provenance`; two repeats per volume with reversed engine order. FUSE (`fuse.bgfuse`): the full workload suite plus all four witnesses. Local (`xfs` on the md0 RAID, saturated by other measurements: load average 49–152, 136 GB written to md0 during the second local run): the three history-read workloads plus the history, distracted and horizontal witnesses (the recovery witness is FUSE-only by policy). Assessed at `d8d6efb77a` over all four receipts; no repeat was dropped. Commit `99031a36c5` later isolates a failed history-sweep leg to its engine; it does not change completed sweeps.

| Clause | Volume | Witness verdict | Repeat 1; repeat 2 |
|---|---|---|---|
| history_read | FUSE | PASS | native `turn.sync.snapshot` P50 0.085/0.056/0.084 ms; 0.052/0.052/0.099 ms at 400/800/1,600 turns (sidecar 3.0/3.7/3.1; 1.7/1.8/1.9 ms). Native sidebar P50 0.55/0.53/0.33; 0.36/0.37/0.35 ms at 10/20/40 turns per conversation (sidecar 3.9/5.3/3.2; 1.6/1.7/2.1 ms). Every longer length stays inside native's repeat range. |
| history_read | local | **FAIL** | repeat 1: native open 0.074/0.062/0.079 ms, sidebar 0.40/0.45/0.51 ms (sidecar 1.7/2.1/7.8 and 1.7/1.9/8.0 ms). Repeat 2: a native strict `turn.append_settled` seeding the 4× corpus exceeded its 30 s request deadline at the WAL barrier (`embedded_transport_error`), ending the witness. |
| degraded_fuse | FUSE | PASS | Second `serve` refused (`another tofu-db process owns the data directory: Resource temporarily unavailable (os error 11)`); second sidecar supervisor deferred (`the project lease is held by Storage sidecar pid=…`). Per repeat 3 SIGKILLs with a write outstanding; all 302 acknowledged writes read back exactly, 0 lost; the unacknowledged write came back absent in 11 of 12 rounds and complete once (native), never changed; writable after recovery. Native daemon reopen 21–71 ms; sidecar 0.74–1.07 s. |
| distracted_hardware | FUSE | INSUFFICIENT: budget requires owner decision | CPUs 126–127. Native pressure/idle P99: `turn_append_burst` 5.71× (8.9→51.0 ms); 2.84× (22.6→64.1 ms); `conversation_open` 7.89× (0.106→0.839 ms); 7.95× (0.140→1.11 ms). Sidecar, context only: 26.5×; 3.38× and 32.3×; 36.7×. |
| distracted_hardware | local | INSUFFICIENT: sidecar leg failed | Native `turn_append_burst` 0.78× (642→499 ms); 0.44× (6,727→2,943 ms): md0 fsync, not CPU, set these tails. `conversation_open` 2.56× (0.237→0.608 ms); 4.09× (0.185→0.757 ms). In repeat 2 the sidecar did not start within its hard timeout on the pinned CPUs. |
| horizontal_reads | FUSE | PASS | Native writer ops/s with 0/1/2/4 readers 658/734/738/701; 842/834/835/729 (sidecar 88/46/71/65; 172/166/143/130). |
| horizontal_reads | local | PASS | Native 16.2/17.0/16.3/19.8; 3.7/5.6/3.6/3.3 ops/s, fsync-bound and inside its writer-only range (sidecar 15.0/12.0/13.2/9.9; 6.3/4.7/9.7/11.1). |

Dimension verdicts from the same assessment: `history_read` **FAIL** (its numeric P50 subgate passes on both volumes; the local witness fails), `degraded_fuse` INSUFFICIENT (witness PASS; all 30 observed strict P50/P99 comparisons pass, but 17 FUSE P99 cells have fewer than the certification's 1,000 tail samples at `small`), `distracted_hardware` INSUFFICIENT, `horizontal_reads` **PASS**. The local history FAIL is a write-tail finding under disk saturation, not a read-flatness regression; a repeat run cannot replace it, only add evidence. Open owner decision: the numeric no-blow-up budget for `distracted_hardware`, for example a bound on native pressure/idle P99 or native no worse than the sidecar's own ratio in the same run (which these FUSE cohorts would meet). Each witness block also records the pinned CPUs' and their SMT siblings' busy fraction, so foreign load during an idle block stays visible. Receipts, per-run `/proc/loadavg` and md0 diskstats, and the assessment JSON/Markdown are kept under `.tmp/tofudb-charter-joined-20261112/` in the shared checkout.

Reproduce (reverse `--engines` for the second repeat; local runs may restrict `--workloads`):

```bash
python3 scripts/tofudb_perf_compare.py --scale small --engines tofudb,sqlite,rawsqlite \
  --native-client embedded --capture-provenance --binary <certified>/tofu-db \
  --authority-dir-parent <volume parent> --charter-witnesses all --output <receipt.json>
python3 scripts/tofudb_charter_assess.py <receipt.json> ... --json-output <a.json> --markdown-output <a.md>
```

## What we deliberately do NOT benchmark

* **PostgreSQL as a gate.** Zero-configuration Tofu cannot require a server dependency. `--postgres-dsn`/`--postgres-reset` permit only explicitly disposable `tofu_perf*`/`tofu_bench*` databases as informal same-host, default-configuration references, never tuned/production PostgreSQL claims.
* **Raw micro-ops** (single-row insert loops, pure KV). We measure the real operation catalog. Cross-engine microbenchmarks are admissible only through one shared same-host harness with symmetric durability.
* **Averaged or dimension-dropping win claims.** Every gated dimension, both volume classes and sidecar/raw-floor baselines must hold; dropping one cannot establish victory.

## Measurement discipline

* **Attribution controls.** Calibrate sensitive workloads with balanced repeats   of identical executable/library artifacts before interpreting small deltas.   Preserve every window and failed gate. Source digests, build context, daemon   readiness and client initialization are separate witnesses; follow   [recovery qualification](TOFUDB_RECOVERY_ATTRIBUTION.md#build-and-startup-boundaries). * **Freshness gate.** Default binary discovery verifies the embedded   `--build-info` source digest against the retained checkout before measurement;   an old executable fails closed. `--binary` permits an intentional historical   comparison, whose report records `tofudb_build_identity`,   `checkout_source_sha256`, and `source_matches_checkout`. Binary SHA pins keep   a run reproducible; neither that SHA nor file mtime proves source freshness.   Build from a recorded current-source snapshot, retain its digest and the   executable SHA, then keep that executable immutable throughout the run.   Embedded runs also retain the loaded library hash and self-reported source   digest; historical ABIs without that export report identity unavailable.   `cargo build --release` from `packages/tofu-db/` uses the package's pinned C   compiler. A source edit during a run does not turn its old binary into latest.
* **Complete lifecycle.** Report shutdown, reopen, and first read separately.
  `maintenance_reopen`'s top-level latency is reopen only. The native harness
  closes its application pool before parent EOF and counts that cleanup in
  shutdown. Native connection teardown also wakes blocked reads and joins all
  workers even if a peer remains connected. Raw SQLite close/reopen and native
  embedded abandon/reopen are explicitly labeled controls, not SIGKILL crash
  certification; incompatible scopes have no speedup ratio. Process write
  counters on FUSE do not prove physical write amplification. Cold probes carry
  the same explicit owner as ordinary reads.
* **Same volume, same run.** All engines for a given comparison run on the
  same filesystem in the same process, so volume class is controlled.
* **Two volume classes.** `/tmp` (local, fast) and the repo's FUSE mount
  (slow/network) are the two deployment realities; both are measured.
* **Windows, not just percentiles.** A global P99 can hide a slowdown that
  grows with corpus size; the 10-window breakdown catches history-dependent
  degradation.
* **Load model declaration.** The harness is a closed-loop sequential client:
  that is the product's real shape (one user driving a synchronous UI), but
  closed-loop clients under-report tail latency under saturation
  (coordinated omission). The `swarm_write`/`concurrent_read` phases add
  concurrency; any future saturation claim requires an open-loop offered-load
  sweep, not this harness.
* **Durability symmetry.** Cross-engine latency is only compared at
  equivalent durability: `tofudb --durability strict` (fdatasync per commit)
  vs the sidecar's SQLite `synchronous=FULL` journal. The report records
  `equivalent_durability`; `--durability normal` numbers must never be
  compared against strict-mode baselines.
* **Stage attribution.** `TOFU_DB_FSYNC_STATS=1` makes the daemon print
  per-stage call counts and nanos at shutdown (`semantic`, `apply_batch`,
  `wal`, `checkpoint_if`, `drain_step`, `history_stage`, `pack_retire`, …).
  Every write-path optimization hypothesis must be stated in these stage
  terms and re-measured after the change.
* **No wall-clock CI gates.** This is a measurement tool, not a timing gate
  (per the script's own docstring). Results inform optimization; they do not
  fail CI.

## Structural work counts (non-timing)

Problem: cross-engine latency numbers answer "how long" without showing
whether the two stacks did the same work. Decision: measure the structural
work of one logical operation — storage statements, document decodes and
per-turn row materializations — before attributing any timing difference.
This is **work-count evidence, not a performance verdict**: no clock,
throughput, durability or latency claim follows from it.

The measured owner is `tests/storage_authority_sql/test_backend_structural_work.py`,
an executable specification over fixture-owned SQLite scratch. It drives the
real in-process SQL authority's `conversation.get` and reads three existing
seams: SELECT and mutating statement frames that reach SQLite (`sqlite3` trace
callback), the JSON-document decoder (`_common._load`, whichever module bound
it), and the per-Turn row materializer (`_conversations._turn_public`, the name
that module bound from `_turns_core` at import). The harness's own
`BEGIN`/`ROLLBACK` and the Sidecar's client/dispatch boundary are excluded, so
`select_statements` means "SELECT frames inside one in-process handler", not
client round trips. Nothing in the production path is instrumented; the port
exists only in the test.

| operation | backend | SELECT statements | mutating statements | document decodes | per-turn row materializations |
|---|---|---|---|---|---|
| `conversation.get` full, 1 Turn | SQLite sidecar | 2 | 0 | 3 | 1 |
| `conversation.get` full, 6 Turns | SQLite sidecar | 2 | 0 | 13 | 6 |
| `conversation.get` metadata only, 6 Turns | SQLite sidecar | 2 | 0 | 1 | 0 |
| `conversation.get` window of 2, 6 Turns | SQLite sidecar | 3 | 0 | 5 | 2 |
| `conversation.get`, any | TofuDB engine | unmeasured | unmeasured | unmeasured | unmeasured |

Structural reading: the sidecar issues one header `SELECT`, then either a
lane-ordered Turn scan (full read) or a per-conversation `count(*) … GROUP BY`
(metadata only); a bounded window adds a separate `COUNT(*)` before its
`LIMIT`/`OFFSET` scan. Document decodes are `1 + 2 × materialized Turns`
(conversation settings, plus settlement and projection JSON per Turn). A
window or metadata read therefore decodes only the Turns it returns, not the
Turns it stores.

The TofuDB column is **unmeasured, not estimated**, and the boundary is
per-counter: one `client.query('conversation.get')` is one request on both
stacks (client round trips), but document decodes and Turn-row
materializations on TofuDB happen inside the Rust engine with no Python seam.
The native engine is the only seam that can observe them, and no
source-matched engine is available in this offline environment (a native build
is out of scope): `verify_current` refuses the installed binary, and a root
without a source fingerprint is treated as unverifiable rather than matching.
The test pins that observed refusal reason, so a future report cannot inherit
estimated native numbers.

## Measured baseline (2026-09-19, production payloads)

Binary built from current sources (`cargo build --release`), `--scale small`,
local fast volume (`/tmp` RAID), same run vs the legacy SQLite sidecar,
`--retry-backpressure` (production retry policy). Report:
`data/benchmarks/baseline-2026-09-19.json`. All read workloads send the
payloads production actually sends (see "Payload fidelity" below).

| workload | tofu-db p50 | sqlite p50 | verdict |
|---|---|---|---|
| record_put_burst | 0.43ms | 0.67ms | **1.6× faster** |
| record_get_points | 0.19ms | 0.29ms | **1.5× faster** |
| conversation_create_burst | 0.65ms | 0.65ms | par |
| sidebar_list_page | 2.40ms | 1.27ms | **0.55× slower** (see finding S3) |
| conversation_open (`message_window: 50`) | 0.21ms | 0.36ms | **1.7× faster** |
| turn_append_burst | 1.09ms (p99 7.80) | 1.36ms (p99 1.79) | p50 faster, **p99 loses** |
| conversation_search | 1.25ms | 1.54ms | **1.2× faster** |
| mixed_80_10_10 | 0.22ms (p99 3.94) | 0.33ms (p99 1.67) | p50 faster, **tail loses** |
| open_windowed_long_history | 0.69ms | 1.45ms | **2.1× faster** |
| sidebar_with_history | 3.97ms | 1.60ms | **0.45× slower** (see finding S3) |
| transcript_bulk_hydration | 13.6ms | 22.6ms | **1.6× faster** |
| concurrent_read | writer 442 op/s, reader p50 1.15ms | writer 182 op/s, reader p50 1.78ms | **2.5× faster** |
| swarm_write | 3.84ms (aggregate 400 op/s) | 5.00ms (530 op/s) | p50 faster, aggregate loses (variance) |
| turn_append_large | 4.91ms | 4.24ms | 0.85× slower |
| maintenance_reopen | 624ms, first read 0.74ms | 1109ms, first read 6.67ms | **1.8× faster; first-read 9×** |
| crash_recovery (SIGKILL mid-load) | 339ms, zero loss | 1141ms, zero loss | **3.2× faster** (standalone tiny-scale: 10.2ms vs 1.19s, **115×**) |

Physical footprint (same run, all phases):

| metric | tofu-db | sqlite sidecar |
|---|---|---|
| data size after run | 1,327 MiB | 56.7 MiB (**23× smaller**) |
| process write volume | 1,940 MiB | 580 MiB (**3.3× less**) |
| isolated 2,000-append probe (442 KiB logical) | 369.5 MiB / 442.8 MiB written (**855×/1025×**) | 36.1 MiB (83×) |

Deterministic write-amplification ratchet (CI,
`src/write_amplification_tests.rs`): 1 VFS write op per append (block
materialization is fully deferred) and a WAL-growth sawtooth of ≈48 KiB per
append on average, 62,632 bytes peak in a 50-append window, for a ~60-byte
logical projection — every commit rides ≈12 rewritten 4 KiB CoW pages.
History-scaled read probes: windowed `turn.sync.snapshot` is O(window) and
flat on both engines (tofu-db 0.73ms vs 1.65ms @400 turns); unwindowed reads
are O(history) on both (8.9ms vs 18.4ms @400).

## Measured baseline (2026-09-20, overlay on, three engines)

Same-binary A/B (`TOFU_DB_WRITE_OVERLAY=0` vs `=1`, SHA256-pinned binary),
`--scale small`, local fast volume, `--retry-backpressure`, three engines.
Reports: `data/benchmarks/baseline-2026-09-20-overlay-default-on.json`
(overlay on, the production `.env` configuration) and
`overlay-ab-2026-09-20-off.json` (legacy path).
Certification with the overlay forced on: contracts 354 + application 36 +
browser-sqlite 10 + browser-tofudb 10 PASS (`scripts/certify_storage_backends.py`);
native differential 178/178 and application contract 36/36 re-verified.

Client-transport follow-up (same day): the production `StorageV2Client`
shed one-session-per-RPC for a bounded persistent-session pool
(`lib/storage/v2_client.py::_SessionPool`, capacity from the daemon's
readiness `maximumConnections`). Probe attribution (loopback, this host):
per-RPC session setup cost 136 µs — the adapter, not the engine or the
wire, was the hot path's dominant term. Pooled no-op RPC 177 → 73 µs;
in-daemon point-read execution is ≈6 µs vs the raw-SQLite floor's ≈5 µs,
so the engine itself is already at the embedded floor and the remaining
gap is transport. The table below is the pooled production path, report
`data/benchmarks/pooled-client-2026-09-20.json`:

| workload | tofu-db p50 (p99) | sqlite sidecar p50 (p99) | rawsqlite floor p50 | verdict vs sidecar |
|---|---|---|---|---|
| record_put_burst | 0.16ms (0.28) | 0.55ms (0.85) | 0.07ms | **3.5× faster** |
| record_get_points | 0.08ms (0.09) | 0.22ms (0.37) | 0.006ms | **2.7× faster** |
| conversation_create_burst | 0.20ms (0.29) | 0.60ms (1.11) | 0.05ms | **3.1× faster, p99 3.9×** |
| sidebar_list_page | 0.60ms (0.87) | 1.19ms (2.11) | 0.12ms | **2.0× faster** |
| conversation_open (`message_window: 50`) | 0.09ms (0.12) | 0.34ms (0.78) | 0.02ms | **3.6× faster** |
| turn_append_burst | 0.36ms (0.69) | 1.31ms (1.71) | 0.11ms | **3.7× faster, p99 2.5×** |
| conversation_search | 0.69ms (0.82) | 1.44ms (1.87) | 0.09ms | **2.1× faster** |
| mixed_80_10_10 | 0.09ms (0.71) | 0.32ms (1.70) | 0.006ms | **3.7× faster** |
| open_windowed_long_history | 0.60ms (0.70) | 1.39ms (2.65) | 0.03ms | **2.3× faster** |
| sidebar_with_history | 0.53ms (0.63) | 1.52ms (2.83) | 0.11ms | **2.9× faster** |
| transcript_bulk_hydration | 9.3ms (27.8) | 21.7ms (29.5) | 0.67ms | **2.3× faster** |
| concurrent_read | reader p50 0.96ms, writer ~700 op/s | reader p50 1.90ms (p99 32.5), writer ~185 op/s | reader p50 0.40ms | **writer 3.8×, reader 2×** |
| swarm_write | p50 1.91ms (p99 81.7), aggregate 908 op/s | p50 4.60ms (p99 16.3), aggregate 572 op/s | p50 0.13ms (p99 33.7) | **p50 2.4×, aggregate 1.6×; tail loses (see below)** |
| turn_append_large | 2.88ms (9.67) | 4.37ms (22.31) | 1.15ms | **1.5× faster, p99 2.3×** |
| maintenance_reopen | 10.7ms, first read ~0.2ms | 524ms | 0.21ms | **49× faster** |
| crash_recovery (SIGKILL mid-load) | 33.4ms, zero loss | 546ms, zero loss | 1.1ms (in-process floor) | **16× faster** |

Overlay A/B attribution (same binary, same run shape): interactive-write p99
moved create 6.63 → 0.44 ms, append 6.98 → 1.65 ms, large-turn 18.9 → 5.2 ms;
swarm aggregate 580 → 878 op/s; crash recovery 387 → 145 ms; data size
1,440 → 218 MiB; process writes 2,126 → 532 MiB.

Physical footprint (pooled-client run, all phases; the graceful-stop overlay
merge + checkpoint that shipped in the same window shrinks what the run
leaves behind):

| metric | tofu-db | sqlite sidecar | rawsqlite |
|---|---|---|---|
| data size after run | 113.5 MiB | 56.6 MiB | 19.2 MiB |
| process write volume | 321 MiB | 585 MiB | 1,376 MiB (upper bound, includes FTS merge churn) |

Historical scope of the preceding table: its build passed the listed operation
latencies and the then-measured FUSE phases. It does not certify current sources,
index readiness, complete shutdown, or 1,000 active streaming conversations.
The historical unresolved gates below remain visible; newer evidence must
reassess every dimension against a current build:

* **Footprint ≤1× (OPEN, 2.0×).** Down from 3.8× on the graceful-stop
  merge; process write volume is now the LOWEST of the three engines
  (321 vs 585/1,376 MiB). Remaining cause: content-addressed 4 KiB page
  framing plus WAL/staged-block retention until checkpoint/merge — the
  harness measures post-run size with no GC round. Closing it is the P4
  pacer/GC-rebalance work (merge-before-measure plus retention trim), not
  new write-path structure.
* **Engine floor ≤2× (OPEN, 2.7–11× by workload; one workload now BEATS the floor).** The embedded in-process transport landed 2026-09-20 (`packages/tofu-db/src/embed.rs` + C ABI + `tofu_db/embedded.py`; `--native-client embedded` in the harness): every request executes through the same extracted `server::dispatch_one_request` as the framed daemon, minus the socket. Measured (`data/benchmarks/embedded-2026-09-20.json`, small, /tmp, overlay on): record.get 44 µs (2.7× floor), record.put 120 µs (2.7×), conversation_open 58 µs (2.9×), and **concurrent_read p50 0.157 ms BEATS the raw floor's 0.357 ms** (read-replica lane + Rust execution under concurrency). Remaining gaps: search 11× (projection store vs FTS5-in-process), hydration 21× (hydration layout), create 9× (receipt/outbox envelope per op). The transport is harness-verified (differential 178/178, application contract 36/36, lib suite clean); production wiring (supervisor integration + certification) is the follow-up — the floor gate stays OPEN until the production path ships it, never by weakening the durability or ownership semantics the envelope buys.
* **Swarm write P99 (OPEN, 26–41 vs 14–18 ms; was 76–82).** Root-fixed 2026-09-20 in three parts: (1) the write fence that quiesced every commit from rotation-prepare to install-end is gone — the covered staged set is now a separate pending generation (`rotation_draining`) drained independently by the packer, so window commits flow through the encode and only the hard WAL ceiling binds, exactly as the install's final-segment design always anticipated; (2) a deferred install now parks the fully encoded (snapshot, segments) pair instead of re-encoding the covered prefix on every publication round trip, and the driver starts on a prepare signal instead of a 10 ms poll; (3) two invariant violations the load exposed are pinned by regression tests: window-era history staging is frozen for the whole parked period (the manifest hash chain can never orphan), and fresh window restaging of a covered block content-address-dedups across pending generations (the pack batch can never fail its duplicate-ID check). Three consecutive small-scale suite runs: writer p50 2.10–2.45 ms vs sidecar 4.66–5.16 (**2.2× win**), aggregate 763–952 vs 544–593 op/s (**1.4–1.7× win**), p99 26.0/40.9/37.6 ms — the residual is the bounded install window (encode+drain+swap) of a full 60 MiB rotation landing mid-burst; rawsqlite's own single-writer tail spans 9–34 ms on the same phase. p99_sample_count_sufficient is false on this phase, so the number is indicative, not gated.

  Update 2026-09-22 (install-hold + rotation-drain storms): the small-scale FUSE phase regressed to p99 415–543 ms (p50 ~1.7 ms) with every writer stalled at the same wall-clock instant. system.metrics correlation pinned one rotation checkpoint crossing the stall window (~460 ms continuous authority contention) and, in a worse run, the packer publishing 8 segments/33 MiB mid-rotation (~1.5 s): the install's fsync chain runs under the authority hold, and the quiet-gated packer lets ~32 MiB of staged blocks accumulate under a hot foreground, so the install's drain precondition inherited that whole backlog as one fenced storm. Fixes: the driver pre-creates the next-generation WAL off the mutex, install blocks write through the loose path (no dedupe probe read), and — the decisive change — the commit path requests the background drain at half the WAL window so the backlog is retired before rotation (a window pre-encode variant was measured and REVERTED: it lengthened the fenced cycle). Three reruns: swarm p99 21.9–35.6 ms, zero >100 ms stalls, aggregate 1,126–1,195 op/s (was 228–314). Full-suite head-to-head (`data/benchmarks/baseline-2026-09-22-optimized.json`): 15/16 workloads faster (1.41×–16×); record_put p99 0.54 vs 0.78 ms and turn_append p99 1.34 vs 3.00 ms now win; swarm p99 21.3 vs 15.2 ms (was 489 vs 14.75); footprint 1.76× (was 2.04×). Remaining open gaps: turn_append_large (p99 25.3 vs 13.9 ms), footprint ≤1×, engine floor, and a decisive (not weather-parity) swarm p99 close.

## Measured baseline (2026-09-23, embedded transport, floor decomposition)

Binary built from current sources (`cargo build --release`), `--scale small`,
local fast volume (`/tmp`), `--retry-backpressure`, three engines,
`--native-client embedded`. Reports:
`data/benchmarks/embedded-2026-09-23.json` (pre-fix) and
`data/benchmarks/embedded-2026-09-23-optimized.json` (this section's fixes).
Against the sidecar all 16 workloads are faster (1.45×–83×). Against the raw
floor, two workloads reached the ≤2× gate this run and every other gap
carries a named structural cause below.

| workload | tofu-db p50 | rawsqlite p50 | gap | structural cause |
|---|---|---|---|---|
| record_put_burst | 0.123ms | 0.066ms | **1.9× ✓** | — |
| turn_append_large | 2.43ms | 1.23ms | **2.0× ✓** | — |
| turn_append_burst | 0.255ms | 0.112ms | 2.3× | T1+T3 |
| concurrent_read | 0.83ms | 0.33ms | 2.5× | T1; aggregate throughput still favors tofu-db (read-replica lanes) |
| conversation_open | 0.054ms | 0.018ms | 3.0× | T1 |
| conversation_create_burst | 0.17ms | 0.052ms | 3.3× | T1+T4 |
| sidebar_list_page | 0.63ms | 0.12ms | 5.4× | T1+T2 |
| sidebar_with_history | 0.73ms | 0.11ms | 6.6× | T1+T2 |
| record_get_points | 0.039ms | 0.006ms | 6.5× | T1 |
| mixed_80_10_10 | 0.043ms | 0.006ms | 7.2× | T1 |
| conversation_search | 0.81ms | 0.097ms | 8.3× | T1+T2 (gram projection vs in-process FTS5) |
| transcript_bulk_hydration | 16.0ms | 0.78ms | 20.5× | T2 (cold per-document burst, by design; steady-state rides the sealed archive) |
| open_windowed_long_history | 1.04ms | 0.034ms | 30.6× | T2 (same cold per-doc path) |
| maintenance_reopen / crash_recovery | 24.8ms / 1067ms | 0.25ms / 1.3ms | n/a | T5 |

Named causes (the floor gate's required disclosure):

* **T1 — Python/FFI transport boundary ≈26 µs/op fixed.** cffi call,
  request/response codec, and the FFI handle's detached-worker handoff
  round trip; the engine's own in-process point read is ≈6 µs. Halved
  2026-09-23 from ≈50 µs by reusing per-thread response buffers in
  `tofu_db/embedded.py` (a fresh 1 MiB zero-filled buffer per execute cost
  ≈23 µs). Further closure requires removing the watchdog handoff, a
  correctness surface this slice deliberately did not touch.
* **T2 — serde_json `Value` materialization on document reads.**
  perf attribution (2026-09-23, 45K samples): ≈1/3 of read-path CPU is
  `Value` parse/BTreeMap-insert/re-serialize/drop — ≈30 µs per turn
  document, diffuse with no single hotspot. Attempt documents on the same
  path are now spliced as `RawValue` (2026-09-23: stored canonical bytes
  embed verbatim, sort keys lifted by a field-skipping derive; differential
  181/181 byte-identical). Turn documents themselves still parse
  (`public_turn` is a real transformation). The durable fix for bulk/window
  reads is the sealed transcript archive; the cold burst stays per-document
  by design.
* **T3 — write-path envelope** (receipt + outbox + OCC witness per op).
* **T4 — create envelope** (T3 plus the tenant-global ID claim).
* **T5 — lifecycle ops are structurally incomparable to the floor.** The
  raw baseline never leaves its own process, so reopen/crash-recovery
  "gaps" price process startup, not engine work; the sidecar comparison
  (24.8ms vs 524ms reopen) is the meaningful axis.

Corpus-fidelity note (payload discipline): the harness's
`turn.append_settled` corpus carries `currentAttemptId: null` — faithful
for ingested/human turns (`append_settled_turn` "without creating an
executor attempt"), but it means assistant-turn attempt materialization
(the T2 splice) is invisible to `open_windowed_long_history` today. A
corpus extension seeding attempt-bearing turns is the follow-up. The
default-transport decision landed 2026-09-23: `TOFUDB_TRANSPORT=auto`
resolves embedded everywhere except observed network mounts
(`docs/STORAGE_TOFUDB.md`), so production personal deployments on local
volumes now ride this embedded path by default.

## Payload fidelity (2026-09-19 correction)

Two harness defaults silently measured non-production contracts and have been
fixed; every future workload must state which production call site it models:

* `conversation.list` **defaults `include_messages` to true** at the engine
  boundary — full per-conversation transcript hydration (O(Σhistory)). The
  production sidebar always passes `include_messages: false`
  (`lib/conversations/catalog.py`). The harness's sidebar workloads sent
  neither flag for months, so "sidebar" numbers were really hydration
  numbers. Sidebar ops now pass `include_messages: false`; the bulk contract
  is measured separately as `transcript_bulk_hydration`.
* `conversation_open` used bare `conversation.get` (unbounded history).
  Production opens a bounded window (`repository.get(message_window=…)`);
  the workload now sends `message_window: 50`.

## Findings (2026-09-19, evidence-backed)

* **S2 (shipped): windowed message reads.** `legacy_messages` read the full
  main lane to serve any window. It now reads only the windowed lane-index
  range (order, never ordinal arithmetic; the maintained lane count supplies
  `total`; any count/lane disagreement falls back to the exact historical
  full read). `conversation.get message_window=20` at 400 turns: **3.25ms →
  0.33ms (9.9×)**; equivalence/fallback ratchets in `turn_tests.rs`.
* **S3 (RESOLVED 2026-09-19): per-owner recency directory.** The
  structural fix landed as `docs/TOFUDB_RECENCY_DIRECTORY.md`
  (commit 05f99685f): one derived head-128 document per owner maintained
  in the same transaction as every recency-affecting mutation, with
  coverage-gated fallback to the exact legacy path. Sidebar p50 moved
  2.35–2.42 → 0.72–0.73 ms (`sidebar_list_page`) and 3.97–4.07 →
  0.75–0.76 ms (`sidebar_with_history`) on both volume classes — from
  0.55×/0.45× behind the sidecar to 1.7×/2.1× ahead, with ≤0.07 ms
  per-append maintenance cost. Reports:
  `data/benchmarks/recency-s3-2026-09-19-*.json`.
* **S4: burst writes can escape rotation backpressure to the user.** A
  ~500-append seed burst exhausts the client's bounded retry budget and
  surfaces `database_busy` (WAL hard ceiling while a rotation is in flight).
  Production policy must keep classified-retryable retries on
  (`--retry-backpressure` in the harness); the overlay proposal removes the
  cause (WAL grows ≈4× slower when commits stop carrying page copies).
* **W1 (ROOT-FIXED 2026-09-20): entity CoW write amplification.** ≈36
  content-addressed pages per turn append; every block byte written ≥3
  times (WAL-carried, loose staged, packed segment); 855×/1025×
  space/write amplification vs the sidecar's 83×. All red cells in the
  write rows derive from this. The architectural fix
  (`docs/TOFUDB_WRITE_PATH_OVERLAY.md`, measured below) ships in
  production via `TOFU_DB_WRITE_OVERLAY=1`; default-enable gates on the
  lib-suite mode triage in "Next steps".

## Optimization targets (ordered by measured pain × leverage)

1. **Write-path overlay ("the WAL is the database")** — IN PRODUCTION
   VIA OPT-IN (`TOFU_DB_WRITE_OVERLAY=1`) 2026-09-20, see
   `docs/TOFUDB_WRITE_PATH_OVERLAY.md`. Commit =
   WAL append; entity mutations serve reads from a bounded overlay;
   checkpoint batch-merges. Targets: write p50 → ~0.2ms, p99 → near p50,
   WAL bytes/commit ≥4× down, footprint to the sidecar's order,
   backpressure escapes gone.
2. **Sidebar cardinality path (S3).** Post-overlay namespace consolidation:
   one recency-directory document per owner instead of per-append updated-
   index rekeys (no tombstone history, no read-all+sort).
3. **`turn_append_large` (0.85× → 1.3× repetitive, 2026-09-19).** The carried-block spill path is NOT the cost (`put_deferred` 6.6 µs, `put_stage` 0.6 µs); the deficit is inline-JSON payload processing, split by stage: admission (outer parse + deep clone + canonical re-encode of the nested projection/settlement documents) and execution (projection parse, search chunking, document store) in `turn.rs`. The admission half is fixed: `compile_turn` now splits `projection`/`settlement` off as `RawValue` slices before the typed parse (   `semantic_executor.rs`), normalizes top-level keys without unescaping/cloning/re-escaping megabyte-class strings, and preserves the producer's exact bytes (semantic-identical to the legacy normalization, proven by tests in `semantic_executor_tests.rs`). The receipt canonical bytes/digest/outbox are computed over the intact raw map (`canonical_request_raw`), byte-identical to the typed path for the compact wire producers — receipt replay/conflict fencing is unchanged (differential 178/178 both flag states). Measured p50 with the receipt-correct build: repetitive 3.20 → **3.07 ms (beats the sidecar's 4.24 ms)**, escaped ≈13 ms (par; the run-to-run spread on this shared host is ±10%, and the admission share was ~0.3–0.4 ms of the total). Remaining cost is execution-side (escaped pattern: ir_steps ≈5 ms) plus wire escape inflation (up to 1.5 MB for adversarial payloads) — the structural fix is routing oversized payload fields through the owner-bound document/blob codec instead of the inline IR literal, assigned to the `turn.rs` codec owner (the file is under active concurrent work as of this writing).
4. **Write p99 (7.8ms vs 1.8ms).** The foreground deficit-pacing tax
   (`checkpoint_if` ≈79µs/commit, `drain_step` ≈2.2ms on threshold
   crossings); subsumed by target 1.

The benchmark's job is to tell us *where* the engine's fixed costs live. The
earlier hypotheses have each been tested and retired:

1. ~~Double durability barrier~~ — the strict-mode group WAL barrier now
   shares one fsync across concurrent commits (wait outside the authority
   mutex); p50 write latency confirms the barrier is amortized.
2. ~~Group-commit sequencer wait~~ — the sequencer now skips the batching
   window when no submission is in flight, so single-writer streams do not
   pay it.
3. **Rotation pacing on the commit path** — the surviving cost, per the
   stage attribution below: bounded history-drain steps and checkpoint
   installs ride the same commits that cross the rotation thresholds.

Each optimization is gated by the existing certification matrix (durability
must not shrink) and re-measured here. The sequence is: profile → make one
bounded change → re-run the fault matrix → re-run this benchmark → compare.

## Write-path overlay, measured (2026-09-19, target 1 landed)

Same binary, same host, back-to-back A/B (`TOFU_DB_WRITE_OVERLAY` off vs
on), `--scale small --retry-backpressure --engines tofudb`, on BOTH volume
classes. Reports: `data/benchmarks/overlay-ab-2026-09-19-*.json`.

| workload | local OFF | local ON | FUSE OFF | FUSE ON |
|---|---|---|---|---|
| record_put_burst p50 | 0.41ms | 0.33ms | 0.56ms | 0.47ms |
| conversation_create p50/p99 | 0.59/2.42ms | 0.34/1.23ms | 0.80/2.26ms | 0.57/0.71ms |
| turn_append_burst p50/p99 | 1.19/4.65ms | 0.58/1.17ms | 1.52/4.87ms | 0.76/1.00ms |
| swarm_write | 4.07ms, writer 127 op/s | 1.62ms, writer 443 op/s | **ERROR (S4 escape)** | 2.08ms, writer 338 op/s |
| turn_append_large p50 | 5.21ms | 3.69ms | **ERROR** | 5.25ms |
| maintenance_reopen | 624ms | 359ms | **ERROR** | 362ms |
| crash_recovery | 463ms | 408ms | **ERROR** | 404ms |
| data size (post-run) | 1,391 MiB | 192 MiB (**7.2×**) | 984 MiB | 167 MiB (**5.9×**) |
| process writes | 1,745 MiB | 246 MiB (**7.1×**) | — | — |

Reads hold or improve everywhere (the read lane is untouched); the sidebar
rows (S3) are flat by design pending the post-overlay consolidation.

The headline is the last column against the second: **the overlay on FUSE
beats the legacy engine on the local RAID across the write workloads** —
turn_append_burst p50 0.76 vs 1.19 ms and p99 1.00 vs 4.65 ms, swarm_write
2.7× throughput, maintenance_reopen 1.7× faster, with 8.3× smaller
footprint. The four workloads that errored on FUSE-off (S4 backpressure
escapes and the deadline-derived failures) all complete on FUSE-on. This
is the degraded-volume fluency gate the "Next steps" demanded, and it is
the evidence for "the WAL is the database" fixing W1 at the root.

Target check: write p99 → near p50 (HIT: 1.17 vs 0.58 local, 1.00 vs 0.76
FUSE); ≥4× fewer WAL bytes per commit (HIT: 7.9×, flat with history);
footprint to the sidecar's order (HIT: 192 MiB vs sidecar's 56.7 MiB same
run, vs 1,391 MiB legacy); backpressure escapes (HIT: gone on FUSE).
Write p50 → ≈0.2 ms is a PARTIAL (0.58 ms local): the remaining cost is
semantic execution and the envelope, not tree I/O — a P4/profile topic,
not a correctness one.

## Reconciling the raw baseline

A prior out-of-band micro-benchmark (single-row `INSERT` via Python `sqlite3` vs `tofu-db certify-resource-gate`) showed SQLite ~25× faster per commit because it bypassed the client stack and exercised a different durability policy. That comparison is **retired as evidence**: the in-harness `rawsqlite` engine now measures the bare-SQLite floor on the *same* logical operation mix, the *same* machine, and *symmetric* durability (WAL + `synchronous=FULL`), in every comparison run. The two baselines answer two different questions and both are first-class: the sidecar baseline prices the production path personal mode ships today; the raw baseline prices the embedded engine itself and bounds the client-stack tax both production stacks pay. A "why does Tofu-DB exist" review must quote both, plus the dimensions raw SQLite cannot price: FUSE/network-mount correctness (POSIX-lock/mmap soundness), crash-recovery wall time, and the cold-read ratchet (`packages/tofu-db/src/cold_read_tests.rs`).

## Write-path tail latency, current state (2026-09-21, post-P1 series)

Problem. The 2026-09-10 baseline left `turn_append` creation/append p99 trailing SQLite's, and the certification contract listed "performance superiority at P99" as uncovered. Decision. Close the tail with runtime feedback instead of operator knobs: dynamic group-commit window (the coalescing window closes immediately once four requests queue), adaptive pack pacing with a self-calibrating EWMA baseline and a 2 ms absolute pain floor (the 10 ms floor never fired on fast mounts), pack quiet-gating (defer ordinary batches while the foreground is hot; 32 MiB pressure or drain overrides), pain-bounded pack batch sizes, 30 s boot grace and crash-safe bounded stop joins for both background drivers, and GC mark traversals that check the stop flag every 4,096 visits. Alternatives considered: barrier pipelining (WAL append of group N+1 overlapping the fsync of group N) — defeated for now: measurement showed the tail is coalescing-window/queue dominated at this scale, not barrier-serialization dominated, and pipelining is a much larger correctness surface; fixed operator pacing rates — defeated: they cap throughput below what each machine can deliver and go stale per mount.

Measured (`tofudb_perf_compare.py`, small scale, FUSE, application client, strict durability): record_put_burst 1.52× faster mean (2473 vs 1627 op/s; p99 2.42 vs 2.09 ms), conversation_create_burst 1.11× faster (p99 1.02 vs 1.66 ms), turn_append_burst 1.01× faster (p99 2.80 vs 3.18 ms); maintenance_reopen 119.6× faster; concurrent_read 2.73× faster. The pre-P1 state on this checkout measured record_put 0.90×, conversation_create 0.48×, turn_append 0.44× slower. Certification-grade repeats (medium scale, 1,000 samples per workload — the certification contract's own tail-sample floor): mean superiority is stable (record_put 1.38–1.88×, turn_append 1.01–1.05×, conversation_create 0.87–1.84×), p99 is competitive with mount-weather variance (7–8 of 12 independent samples favour Tofu-DB; the recurring ~15 ms outliers were traced to concurrent background fsync storms and driven the global quiet-gating of every background write stream — packer, pack adoption, search projection, online GC, online backup — behind one shared mount-aware hotness gate in `fsync_stats`). Remaining known gaps, pre-existing and out of this slice's scope: read-heavy setup-dominated workloads (sidebar_list_page 0.31–0.34×, transcript_bulk_hydration 0.34×) and turn_append_large 0.11× (identical at the pre-P1 baseline; large-value write path).

**Soak wedge root cause (2026-09-21).** A 120-minute medium-scale soak
wedged the authority permanently at ~6.5 minutes: the 2,048-entry overlay
merge chunk over a deep, scattered tree produced more written pages than
the transaction block-reference budget, so every merge attempt failed
closed. The merge now halves its chunk until the produced page set fits
(`merge_overlay_chunk` adaptive sizing; regression pinned in
`entity_overlay_tests::overlay_merge_adapts_chunk_size_to_scattered_key_
fanout`), and the rerun soak reports zero errors with the engine's peak
RSS inside the personal-computer budget.

## Read-path analysis (2026-09-22, measured)

Problem. Read-heavy workloads trail the SQLite sidecar: sidebar_list_page 0.31–0.34×, transcript_bulk_hydration 0.34×, turn_append_large 0.11×. Decision on record. turn_append_large moved to 0.16× by eliminating the settled-turn semantic chain's double full-projection parse/escape (the compiler's canonical RawValue escape is spliced verbatim; the compiler also extracts `projection.timestamp` into the IR step so the hot path never re-parses). The gate also now covers read/query activity, not just write commits, in the shared quiet-gate hotness signal. What remains is structural and must not be micro-fixed: per-document hydration cost is diffuse (measured ~190 µs/turn doc, content-size independent, spread over envelope decode, typed document parse, public-turn rebuild, legacy-message construction, key encodings and allocation churn — no single hotspot), because bulk hydration materializes ~500 turn documents per 50-conversation page while the sidecar reads ~50 per-conversation archive columns. The durable fix is an archive-backed hydration path: maintain a compact per-conversation transcript projection (like the sidecar's `messages_json` archive) updated incrementally at append time, so bulk reads hit ~50 compact records; full-document materialization stays the certified fallback. Alternatives considered: per-doc zero-copy splices (bounded win — the cost is per-doc fixed overhead, not content scans), read-side quiet-gating alone (necessary but not sufficient), and batch tree probes (scan already amortizes lane indexes).

## Transcript archive (2026-09-22, shipped)

Problem. Bulk hydration (`conversation.list/get` with full transcripts) materialized every turn document at ~190 µs each, so a 50-conversation page cost ~95 ms. Decision. A live per-conversation transcript archive (`transcriptArchives` in the IR contract): the `tofu-db-archive-seal` daemon worker seals pending legacy-message windows into immutable, digest-witnessed page documents — materialization on a pinned read replica (never the authority mutex), publication in a short `try_lock` hold with a fresh lane-count fence. Reads fuse sealed pages with the exact bounded lane read of the unsealed tail; every non-append lane mutation tombstones the archive, and any count/shape mismatch falls back to the exact per-document read, so an untracked mutation can never serve a wrong answer. Measured: a 50-turn conversation's full hydration drops 37.4 ms → 10.0 ms on FUSE (3.7×), and larger conversations scale with page count, not document count (frozen-history audit showed ~6.7 messages/page). The harness's `transcript_bulk_hydration` phase (seed then measure instantly) measures the cold burst path, which stays per-document by design; steady-state hydration (conversations sealed by the background driver inside ~1 s) is what the archive accelerates. Alternatives considered: live-frozen pages (immutability vs projection-update/reparent/tombstone — defeated), append-time tail rewrites (WAL amplification — defeated), per-document zero-copy splices (the cost is per-doc fixed overhead, not content scans).

Rejected follow-up: rebase attribution measured 400.5→277.8 µs rebase and 672.0→558.9 µs commit under an observer, but default-off embedded settlement P99 mean regressed 18.3%; immutable-root point-validation reuse remains unintegrated.
## Snapshot ownership without capacity admission

Problem: fixed total/reader pin quotas refused work or sent it through a second
capacity FIFO before authority capture. Decision: register every live root and
count its owners without a count quota. Writer/GC exclusion and published-view
read guards still protect capture; immutable replica leases retain each root
until the final transaction or buffer drops. The old reader pressure hints,
capacity claims, release notifications and capacity FIFO are removed. Exact byte
accounting, owner checks, OCC and durability fences remain. Request deadlines,
connection close and authority/registry poison retain their error boundaries.

Alternatives: larger or launch-derived slot counts retain refusals; capturing roots outside GC exclusion risks premature deletion; permanently pinning the published view retains obsolete roots beyond request lifetimes. Verification: 96 simultaneous distinct root owners rendezvous before release; writer and root repinning remain eligible. Published-query and dispatch tests retain 512 readers while new work completes, including 32 queries with authority/FIFO writer guards held. Escaped leases and overflow tests verify exact teardown and atomic registration. Existing recovery, GC and fault matrices remain required. Controlled registration measurement: 96 concurrent synthetic root owners plus one root and writer reach the ownership barrier in 4.0 ms on this host. No root is released to admit the writer; teardown removes every registration. This measures root metadata/thread startup, not page I/O, peak RSS or durable throughput. A separate 96-version real-page test retains every historical value and GC root across subsequent writes, then releases all root registrations. Historical `tofudb_reader_admission_20261006.json` measures the retired queue; its latency values do not certify this change or universal SQLite parity.

## Active conversation acceptance

The [source-qualified b96f3847 local XFS campaign](evidence/storage-local-xfs/current1000.md) completed two reversed-order 1,000-conversation pairs. All six rows qualified, but all four legs missed the 1 s scheduled-stream P99 ceiling: **scale FAIL**. Native stream ratios were 0.441 and 0.539. Separate diagnostic legs indicate substantial delay beyond native RPC and SQLite transaction commit timings, without isolating its cause. This is not DolphinFS evidence.

The [formal 1,000-conversation, two-repeat XFS comparison](evidence/storage-local-xfs/scale1000.md) **failed scale acceptance**: each leg exceeded the 1 s scheduled-stream P99 ceiling. The pinned runner marked the rows comparable (native/SQLite stream ratios 0.426 and 0.546; repeat-0 settlement ratio 1.021), but the historical receipts do not independently establish SQLite signal exit or loaded-library stability across both repeats. Exact same-store reopen/readbacks and source inventory are independently verified; this is neither an overall win nor DolphinFS evidence.

The paired workload now requires a verified owned-child SIGKILL exit for daemon crash cycles; a clean stop/reopen is not a substitute. Receipts name the killed and reopened children and the unchanged authority directory at the all-running-before-stream-release and after-settlement cutpoints. Neither tests mid-stream crash or power loss. Native daemon logs survive the final verdict, including latency-only failure. Current v5 report admission requires both signal-exit/same-store reopen receipts and a consistent process chain; a verified kill with failed reopen is retained without granting coverage. Replay historical reports with their pinned original checker, not as current qualification. New process witnesses do not retrofit old evidence.

Problem: plain reports could qualify instrumented timings, foreign delivery implementations, absent source witnesses or equal filesystem labels. Decision: acceptance checks requested and observed client/native/delivery profiling at every captured boundary, including productive start. The existing pipeline manifest binds coordinator/benchmark aliases to the full source inventory; direct/pipeline and barrier/queued modes cannot be pooled. Physical authority `stat().st_dev` witnesses must match on this same-host runner. Each leg must have a distinct execution ID, lexically normalized authority path and captured device/inode pair; replay never resolves or stats paths supplied by reports. These are run receipts, not authentication of an untrusted report.
Current admission requires identical complete source inventories before/after and against a freshly captured checkout, including the actual executor/resource owners, plus unchanged declared/pinned executable and loaded-library build identities. For SQLite, current admission separately requires SHA-256 witnesses of the SQLite and coalescing VFS files behind the owned child's Linux mappings at launch, both reopens and measurement boundaries. A path whose device/inode does not match the mapping, an absent witness, or a changed build refuses current qualification and relative ratios. The paired checker also binds each backend's qualified build across repeats; a changed native executable or SQLite/VFS hash suppresses all ratios for the mixed-build campaign. Coverage requires native `walDurability=strict` at workload metric boundaries. SQLite reports the actual writer connection's WAL/FULL settings and VFS selection at open in each incarnation; the selection probe checks the connection's main database I/O methods, not merely the loaded extension or URI. Boundary receipts bind these open-time settings to the measured process. This is not continuous configuration surveillance, per-ACK sync evidence, a Python import-origin witness, or a power-loss test. The source/artifact receipt is a provenance witness, not a new native identity implementation. Stable old binaries/coordinators retain `frozen_build_comparable` diagnostic value but not `current_source_qualified`; missing, contradictory or changed evidence refuses qualification. The paired checker requires the recorded leg sequence to alternate in runner order and the workload and physical authority device to stay fixed across repeats. SQLite writer-configuration copies at measured boundaries must agree with the source metrics and their PID/incarnation. Unqualified campaigns and rows emit no formal ratios; raw per-leg P99s remain descriptive. Admission requires explicit workload modes, consistent readiness counters and examples, and finite, nonnegative, ordered P50/P95/P99/maximum observations. Sample totals must fit the bounds implied by those statistics at the producer's exact ranks. Disabled terminal barriers must have no terminal metric or origin. Enabled terminal metrics cannot precede the last scheduled stream submission; the live barrier waits for all stream calls to return. Foreground maxima must fit their clock windows: admission through all-running, stream release through join (or the terminal snapshot start), and settlement through join from no earlier than the last scheduled arrival or, for the call clock with a barrier, snapshot completion. Queued pipeline `stream_submission` clocks are foreground calls and end no later than snapshot start when the barrier is enabled; queued `stream` clocks are push callbacks and may extend through caller join. Direct stream clocks remain foreground clocks. Scheduled settlement starts at the last scheduled arrival and includes the mandatory wait through snapshot completion in addition to the settlement call. Current summaries retain the minimum as well as the percentile ranks, maximum, count and total. Replay validates observed workload summaries, including queued aliases, and checks scheduled stream ranks and total against the per-round time available. For complete populations, scheduled stream, submission, push and settlement summaries must dominate their corresponding call or callback summaries in every retained rank and in total, including the settlement wait floor when present. Admission's enclosing total must cover the three serial accept, bind and start call totals; its rank bounds account for each other call's minimum without assuming their ranks align. Binary64 comparisons allow only operand rounding error. These are aggregate consistency bounds, not a reconstruction of individual calls or callback pairings. Only SQLite reports carry mapping witnesses, and only at the collector's declared boundaries; none is emitted at the terminal snapshot. A successful mapping witness cannot also carry a capture error. Replay requires positive Linux `pid_t` values, absolute NUL-free paths with the collector's distinct SQLite and VFS basenames, and a boolean timeout flag when present. The checker independently derives the deterministic synthetic payload hashes by index and checks the retained recovered hashes against them; it does not reopen archived stores. At least two eligible repeats are required per metric. A normal acceptance exit 1 is distinct from abnormal process exit or timeout, and an ineligible comparison is not an observed relative loss. Alternatives rejected: trusting a wrapper's optimistic verdict, file mtime, the thin executor re-export or equal FUSE labels. `tofudb_benchmark_feedback_20261014.json` remains historical.

Problem: single-report assessment scanned the entire checkout even when a
missing or unsupported provenance receipt made qualification impossible.
Decision: `conversation_acceptance.assess` captures current sources only for
the supported receipt format; otherwise the existing qualifier rejects the
receipt without that unrelated I/O. A supported receipt still gets a fresh
complete inventory on every call unless the caller supplies its independent
snapshot. Capture errors still refuse current qualification, and neither
source drift nor frozen/current identity rules change. Alternatives rejected:
caching a prior checkout inventory (could hide local edits), accepting a report's
own inventory as current, or disabling provenance checks in pipeline tests.
The acceptance specifications cover early refusal, successive captures that
observe a newly added source, unreadable sources and all existing identity
failures. Fixture measurements live in `TESTING_STRATEGY.md`.

Problem. Storage-operation microbenchmarks do not certify interactive capacity. The panel and `tofudb_perf_compare.py` cover at most eight writers; `tofudb_bench.py` covers up to 64 ingestion writers. Settled Turn appends and generic event appends omit the live Attempt/projection/event path.
A completion/recovery pass alone permits a 47-second stream P99; a throughput geometric mean hides losing tails. The old comparison's authority-directory option only relocated TofuDB; SQLite could remain on the system temp volume.

Decision. `benchmarks/conversation_compare.py` is the paired application-workload
acceptance entry; `conversation_concurrency_contract.json` owns its machine-readable
coverage and engineering latency budgets. Default: 1,000 simultaneous running
attempts, four unique 8 KiB chunks each at fixed one-second arrival intervals,
two paired repeats with reversed backend order, and two SIGKILL/reopen
cutpoints: all Attempts running before synthetic stream release, and after settlement. It uses the production executor, Turn facade and
client; the provider is synthetic. Every acknowledged final text is compared
exactly. SQLite and native authorities use the same explicit scratch volume,
native Strict and SQLite WAL/FULL with the coalescing VFS selected, and no SQLite fastpath relocation. The raw-SQLite floor stays
in the separate operation suite because it cannot implement application attempts.

Latency starts at submission for admission, at scheduled provider arrival for stream persistence (including backlog behind preceding events), and at entry to the timed settlement `record_task_event` call for `settle`. That call clock excludes prior scheduling and barrier wait; the separate `settle_scheduled` clock starts at the last scheduled provider arrival. Formal settlement parity uses `settle`. RPC service latency remains a separate diagnostic. Failed/timed-out calls stay in distributions; missing observations fail acceptance. Recovery coverage requires measured running/final restart durations, the full requested peak and running readback, an explicit crash mechanism, and successful integrity verification. Problem: a requested restart may be skipped after partial admission. Decision: require completed phase evidence. Alternative rejected: trusting request flags would certify an unexecuted crash/reopen phase. Reports also retain `phase_ms`: productive work through settlement, recovery readback, executor shutdown, sampler shutdown, verification, final freeze and temporary-authority cleanup. `verification_phase_ms` separates native freeze from offline integrity traversal, or records SQLite's online integrity check. Nested verification timings must not be added again to their parent duration; `wall_seconds` remains the complete run. `restart_ms` is separate from productive load unless the restart intentionally occurs while attempts are running. The resource-observation scope ends after readback, before offline verification and cleanup. A fast streaming percentile cannot hide a slow verification/teardown phase. Older reports without these fields cannot identify such an outlier's cause; retain the observation without inventing an attribution. Initial budgets: admission P99 ≤ 5 s, scheduled stream P99 ≤ 1 s, settlement P99 ≤ 5 s; these are requirements, not claims that the engine currently meets them. Correctness, coverage, absolute latency and per-metric SQLite parity are separate verdicts. No average score offsets a failure. Readiness failures retain startup/productive phase and bounded component details. The CLI saves each failed leg and returns nonzero for incomplete/slow evidence. Unit CI tests verdict behavior, not machine timing. Standalone embedded SIGKILL: `python3 benchmarks/embedded_process_recovery.py --binary /path/to/tofu-db --directory /scratch --output /tmp/embedded-recovery.json` verifies running/final state; it does not upgrade in-process coverage or certify latency. Success cleans temporary state; `--retain-authority` preserves it. Run operator acceptance:

```bash
python3 benchmarks/conversation_compare.py --binary /path/to/tofu-db \
  --directory /path/on/dolphinfs/scratch --output /tmp/conversation-paired.json
```

### Local checkpoint correctness control (not scale acceptance)

The [four-conversation XFS receipts](evidence/storage-local-xfs/README.md) did not exercise a checkpoint (SQLite peaked at 740/773 frames). Use two complementary controls: the existing paired runner with 64 conversations and `--carry-stream-events --repeats 1 --leg-timeout 90` on fresh private XFS, and `tests/storage_authority_sql/test_sqlite_checkpointer.py::test_continuous_writes_restart_the_wal`. Keep four 8-KiB chunks, one-second arrivals, the 30-second RPC contract, no fastpath relocation, native Strict and SQLite WAL/FULL with coalescing VFS. The test's two producers cross the real 4,096-frame threshold behind a pinned reader; it requires actual partial backfill, a stable reader snapshot, complete backfill after release, WAL restart on the next write, exact rows and `integrity_check=ok`. A counter alone is insufficient: writer pass counters record PASSIVE attempts and `writer_frames` charges the requested remainder, not necessarily frames copied. Neither control kills during a live checkpoint or simulates power loss.

[Compact execution receipt](evidence/storage-local-xfs/checkpoint-control.json) and [original 64-conversation evidence archive](evidence/storage-local-xfs/active64-original.tar.gz), observed on `/dev/md0p1` XFS, Python 3.12.13, at `e0fa2da38cb6a9f5e641e99540606ef7723336b1` (tree `a27bcd01f50374ff778e08f5382e46cc499ca50d`):

| Control | WAL / checkpoint evidence | Recovery and integrity |
|---|---|---|
| Paired SQLite | Before productive writes: 0/0 background/writer passes. Before final reopen: peak 4,454 frames; 4 background passes, 4,228 frames backfilled, 2 writer attempts (278 requested frames), 0 failed passes, empty last error | 256 stream events; 64/64 running and 64/64 completed after the respective reopens; 64/64 exact texts and carried replays; `ok`, 0 workload errors |
| Paired TofuDB | Effective `strict`; `walEncodedBytes=18,293,442` at the corresponding pre-final-reopen boundary (bytes, not SQLite frames) | Same counts and exact texts/replays; offline `verify` exit 0, `wal_tail_truncated_bytes=0`, `missing_blocks=0`, 0 workload errors |
| SQLite pinned reader | WAL index `(5390,349)` under the pinned reader; eventual full backfill at 5,394 frames, then index `(4,0)` after restart; 19 background passes, 5,394 frames backfilled, 2 writer attempts, 0 failed passes, empty last error | Exact 4,666 ordered `(id,payload)` rows, IDs 0–4,665 with 3,500-digit zero-padded decimal payloads; payload concatenation SHA-256 `4972bcd6fad15f82f1ccf674b83b0f75c28aa3b785a5610226c3c827359e6c75`; `integrity_check=ok` |

Historical SQLite signal delivery and exit at both cutpoints are unknown; the v2 runner could stop cleanly and reopen. The exact same-store readbacks and checkpoint counts do not distinguish those mechanisms. Actual historical SQLite writer settings are unknown; WAL/FULL is the runner's policy label. Separately from the zero workload counters, the 64-conversation native console records 64 settlement-drain failures; the SQLite console records one retry and 64 deferred wakes. Their eventual outcomes are unknown, and these logs do not negate the checkpoint observations or exact readbacks.

The historical runner marked both legs current-source qualified, but the pair and both legs exited 1: 64 < 1,000 and one repeat < two; the aggregate's “did not meet SQLite P99” messages are eligibility failures (`comparable=false`, null ratios), not observed relative losses. Keep the formal verdict unchanged and draw no performance winner from this pair. Exact-state and replay counts are the original readback oracle results; the paired disposable authorities were cleaned up, not archived. An exploratory 32-conversation run was also below threshold (2,720 frames) and failed source stability while edits overlapped it; it is not qualified evidence. The paired runner emits hashes of **recovered** content by fixture index. An independent fixture generator (no runner import) used `SHAKE-256(ASCII("index:sequence")).digest(4096).hex()` for sequences 0–3; all 64 per-index hashes matched both runs. Its sorted compact JSON digest is `2baa6e6a67e3ffc76b8fa8cc42a072c3749a38330285d2da3c5c6ea4976ba9ab`. Runner SHA-256 `08ff673ab01aaf4fec2a4861d03cb78b60e720442a4df58ab4b4f172819eb999`; stable canonical source inventory SHA-256 `5c9b86972d5bc71fff2a59c6385a8ce573fc78eb5f7d003c0d32b66d7a845617`. Reused native bytes `566b212bf7aafa30db7f5d2dee6b0f4aadd12cdbb7fb4c0dae76de360be42c1c` matched the prior attestation; the compiled and independently recomputed checkout fingerprint both were `443fdee4ce2136e2fbf90e64e0a0316e9667a898d22645e06106fde1f62b78e1`. Coalescing VFS SHA-256 `dd34ae99db3617770a565b11467784e69a77f2e8e2da4684e964e63424ce9d17`.

For `scripts/tofudb_load_test.py` (a separate workload), the real re-exec uses the declaring checkout's Python source, a private cwd/data/project and source-admitted pinned engine before offline seed or supervisor launch. Ordinary children remove `FAULTFS_*` and the standard `faultfs.so` preload entry; `--faultfs` explicitly restores them for intentional emulation. Preserve `LIBBGFS_*` and other `LD_PRELOAD` entries so this fix does not silently switch the DolphinFS route. The flag governs child staging, not libraries already loaded into the invoking shell/process; start a local control with a clean parent loader environment. Separate explicit fault fixtures remain unchanged. The local staging probes supplied sentinel fault and connection variables: neither actual measuring child nor authority retained them. The SQLite sidecar ran `-m lib.storage_authority_sql`; the native launcher exec'd the pinned bytes (verified via `/proc/<pid>/exe`) and passed the private authority directory. Both staging probes exited 0; ancillary dispatch drains logged unavailable at teardown, so these probes establish route/isolation, not zero errors. The focused suite passed 192 tests, including explicit FaultFS temporary-file and I/O-count fixtures. Local child route and checkpoint controls do not establish DolphinFS allocation, routing or interference safety.

The ordinary `test_launched_synthetic_arm_binds_actual_private_roots_and_staging`
specification adds launch evidence without a new report receipt or admission rule.
Both backend labels execute the identical harmless Python fixture under the load
runner's child environment, not either storage engine. It observes open-directory
inodes, a bound private Unix endpoint, an actual temporary file and its staging
parent, and the live child's PID/executable/command through the existing identity
observer, with a separate equality check between the fixture's PID and the
observer's PID. A forged matching requested/Popen command still fails against the
actual child, including when it omits trailing empty arguments. The observer
removes exactly one cmdline terminator, preserving empty arguments. The tested
missing `cmdline` and `executable` fields are rejected; this does not assert
validation of every collected identity field. Existing environment and path
checks alone would not exercise that boundary. This is executable local
preparation parity, not a report that qualifies timing, durability, operations or
relative performance; the existing report qualifiers remain authoritative.
No fresh DolphinFS disposable allocation and coordination window is established
by this test or the prior route preflight. A real comparison still requires that
independent allocation fact; no campaign or production observation is performed.

The settings panel remains a bounded microbenchmark: P99/max/sample/error counts
are visible, small samples are labeled indicative, and capacity is always
“not assessed”, including persisted old reports with a headline score. Neither
suite certifies browser send feedback, real-provider limits or a sustained soak;
these gaps stay explicit in machine-readable reports. Alternatives rejected:
raising timeouts, lowering offered load when requests stall, discarding failed
samples, comparing different filesystems, or treating correctness as speed.

## Retained performance experiments

The user-requested [experiment decisions](TOFUDB_PERFORMANCE_EXPERIMENTS.md)
and `tofudb_performance_20260930.json` retain rejected
WAL suffix copying, permanent snapshots and fair query admission, including
regressions, source identities, attribution limits and retry conditions.
The runtime retains single FIFO admission; no publication snapshot or query
priority lane from those experiments is enabled. Performance conclusions must
include embedded queue coverage and application latency, not dispatch alone.


Indexed history now supplies durable carried payloads without requiring a second
ordinary-payload copy before WAL retirement. Pruning relocates bytes outside the
authority mutex and preserves the entity replay cursor. The
`tofudb_history_payloads_20260930.json` keeps
rejected locking pruning, empty-WAL recovery and mixed-shard GC assumptions,
versioned candidate comparisons, default-off profiling controls and process
SIGKILL readback. Root-path improvement does not constitute capacity acceptance
or SQLite parity: remaining queues, operator verification cost and losing tail
metrics remain separate verdicts.


Recovery attribution adds fixed `history_decode`, `history_lookup`, `history_range`, `overlay_replay`, overlay selection/carried-budget/publication, and complete checkpoint spans. Embedded shutdown attributes each driver join and checkpoint; opt-in `TOFU_DB_FSYNC_STATS` emits fixed phase names/times. The benchmark splits client close from embedded abandon or process kill/join. Verification reports sequential calling-thread phases with elapsed nanoseconds and history decode attempts, source bytes and validated transaction counts; early fatal returns retain completed/failed phase timings, including verify authority teardown (salvage keeps its authority open). Attempts/bytes include failed decodes. Process totals also appear in `system.metrics.durability`; optional request work uses the existing bounded collector. Totals and inclusive spans overlap and must not be added as disjoint latency. These counters retain no identifiers or payloads and perform no I/O. `tofudb_replay_costs_20261001.json` distinguishes the instrumented diagnostic control from the existing production baseline and retains observer controls, correctness and remaining capacity failures. Pin diagnostics separate registry acquisition, byte reservation and byte repayment; work counts acquisitions, budget operations and waiter notifications. `tofudb_pin_accounting_20261003.json` retains exact quotas, rejected alternatives and default-off results. Reply acquisition/recycle anchors are global caller-edge spans; reuse/create work is attached to worker requests. `tofudb_reply_reuse_20261003.json` retains fresh-channel lifetime, three rejected reuse/mailbox designs, observer costs and default-off comparisons. `pack_filter_incomplete` counts conservative probes when the Bloom filter does not cover the installed catalog; it is an event count, not elapsed time. `overlay_prepare_capture`, `overlay_prepare_encode`, and `overlay_prepare_publish` distinguish brief authority holds from private tree preparation outside authority. `overlay_prepare_stale` is a rejection event, not a duration. Private pages account actual transaction memory without an aggregate quota and release after each slice; retention and resource peaks must accompany comparisons. `overlay_prepare_budget_wait` remains a compatibility counter for retired quota deferrals and receives no new increments; it does not classify physical I/O errors. Results and limitations live in `tofudb_overlay_preparation_20261001.json`. Projection probes `projection_head_materialize`, `projection_head_checkpoint`, `projection_head_patch_load`, and `projection_head_patch_apply` separate reconstruction work; inclusive and child durations overlap. Checkpoint/patch bytes count valid materialized JSON; applied patches may precede a later rejected head. Global counters include background consumers; optional request work attributes the caller, with existing bounded retention. Ownership and admission rules live in [JSON materialization](TOFUDB_JSON_MATERIALIZATION.md); `tofudb_projection_reconstruction_20261001.json` retains observer controls and limits. WAL/page attribution lives in [barrier scheduling](TOFUDB_WAL_BARRIER_SCHEDULING.md). Block branch attribution lives in [verified reads](TOFUDB_BLOCK_READ_PATH.md). Rotation ownership and attribution live in [window preparation](TOFUDB_ROTATION_WINDOW.md). The source-pinned 2026-10-05 matrix, named remaining raw-SQLite gaps and structural causes are retained in the receipt-proof retest. Fresh `tofudb_point_ingress_20261005.json` separates application input-classification gains from removing unused benchmark read UUIDs; raw-SQLite, delivery and remaining coverage gaps remain open. Publication lock placement and measured slow-I/O isolation follow [publication latency](TOFUDB_PUBLICATION_LATENCY.md).


## Entity transaction byte ownership

Problem: a fixed 160 MiB aggregate quota rejected valid reads and writes and deferred private overlay encoding despite available host memory. Decision: transaction leases account actual retained bytes without an aggregate admission ceiling. Checked arithmetic and poison fences remain authoritative; drop and replacement repay ownership exactly. OCC witnesses, source I/O failures and per-operation protocol validation retain their existing behavior. Alternatives: raising the quota only moves the refusal point; disabling accounting obscures retained memory and leaks. Snapshot counts are also ownership observations without admission quotas. Verify concurrent lease accounting above the retired threshold, real reads/writes and overlay publication under that observation, plus atomic overflow and poisoned-registry tests. Accounting-only large reservations do not measure real allocation capacity or certify behavior under OS memory exhaustion.

Controlled staging measurement: 32 simultaneous transactions each retain 700
8-KiB values: 183,500,800 real payload bytes and 185,707,880 accounted bytes,
created in 150 ms on this test host. Dropping all transactions leaves zero
retained bytes and zero handles. This measures in-memory staging, not durable
commit throughput, peak RSS, or behavior under OS memory exhaustion.


## Provider catalog and OCC witness ownership

Problem: the SQL/native provider domain rejected a 33rd provider, and the
native list path could return only a fixed 33-row scan. Larger catalogs also
hit 6,144 point-witness and 1,024 range-witness transaction quotas. Decision:
owner/label counts are exact integrity observations, with checked arithmetic,
not admission limits. SQL creation drops the quota-only owner lock and count
probe; global ID uniqueness, receipts and rollback remain authoritative. Native
listing traverses every created-index page on one snapshot, retains every OCC
witness and redacts ciphertext. Page size, per-document validation, wire output
validation and single-range page/format validation retain their separate contracts.

Alternatives: larger quotas merely move the refusal point; fresh transactions
per page lose snapshot consistency; discarding earlier witnesses can miss late
updates and phantoms. Verification: 7,000 native providers cross index pages
and the retired point-witness threshold; mutating the oldest listed provider
still aborts a dependent write. Owner/label filtering, duplicate global IDs,
secret redaction, deletion counts, count overflow and complete teardown remain
checked. SQL repository tests retain all 65 providers. Entity tests record 6,145
distinct point reads and 2,050 forward/reverse range predicates, then detect
late updates/phantoms and release all reservations. No provider model is added
or made callable by these tests; model registration keeps its admission owner.

Controlled native catalog measurement: 7,000 provider rows produced 1,907,671
public bytes in 3.03 seconds including grouped creation and list traversal.
This measures an isolated temporary native authority, not remote provider I/O,
peak RSS or full application latency. The late-document OCC check aborts the
dependent write; teardown returns retained transaction bytes and handles to zero.
SQLite/private-PostgreSQL share the same 65-row ownership/redaction contract
test; unavailable local PostgreSQL binaries skip that lane explicitly.


## Range traversal and witness memory

Problem: a fixed 1,024-leaf witness quota refused small valid results in
churn-heavy ranges. Masked scans repeatedly re-read larger prefixes until an
internal row ceiling failed. Every range also precharged two full hypothetical
leaf vectors. Decision: retain all visited leaf references using fallible vector
growth; account retained leaf-vector capacity, key lengths and the existing
header allowance. Masked scans move forward/reverse key cursors through bounded
base windows on the original snapshot, with the corresponding raw-window OCC
witness for every visited window. Later failure truncates only newly captured
witnesses and repays their reservations; earlier transaction reads survive.

Alternatives: larger caps postpone refusal; unlimited prefix expansion retains
repeated work and large temporary row buffers; dropping previous windows loses
conflict/phantom evidence. Page/format validation, explicit result-page sizes,
physical I/O errors, allocator failure and transaction ownership retain their
separate authorities. Tombstone reclamation still reduces actual storage and
scan cost. Verify both directions beyond 1,024 leaves, 14,000 masked base rows,
exact-key exhaustion, late conflict detection, actual capacity accounting and
rollback of a later-window accounting failure in `entity_range_demand_tests.rs`.

Controlled traversal measurement: two 1,099-leaf dead-prefix walks found their
forward/reverse tails in 4.37 ms combined. With 14,000 staged masks, 15 bounded
windows returned 1,000 visible rows in 13.62 ms forward / 7.43 ms reverse. Small
exact-key reads account less than 4 KiB per witness instead of a hypothetical
full leaf quota. These are deterministic-VFS traversal timings, not real-volume
throughput or peak RSS. Actual I/O-fault and later-window accounting-fault cases
retain prior reads and repay new reservations.


## Timer admission and complete recovery

Problem: timer definitions were refused at fixed live/saved counts, process
registration repeated the live quota, and restart restored only a capped prefix.
Decision: live/saved counts observe ownership without admission limits; legacy
capacity overrides cannot reject work. Internal active-feed limit=0 traverses
all native index pages or all SQL rows, while positive request limits remain
explicit projections. Recovery starts every eligible definition. Duplicate IDs,
owner filtering, leases, readiness obligations, poll budgets, cancellation and
startup-failure rollback retain their existing authority. Alternatives: larger
limits postpone refusal; skipping overflow until another restart leaves active
work unserviced; dropping leases can duplicate continuations.

Verification: 65 real watcher threads rendezvous despite old knob 1 and repay
all leases; restart reads 265 definitions despite old resume/live knobs 1. Native
creation and full recovery feed retain 650 active definitions with compatibility
capacity 1 in 555 ms on the temporary test volume. This measures definition
creation/readback, not model polling, peak RSS or real application latency. Each
watcher now uses the shared wait runtime described below. Generated contracts and owner-scoped terminal/fault tests
cover the same transition boundaries.


## Timer wait and physical work ownership

Problem: removing admission quotas exposed the cost of one sleeping daemon thread per active watcher. Decision: one lazy process-owned asyncio loop keeps independent timer waits; synchronous storage and polling calls use demand workers with zero idle retention. Independent work starts without a worker/queue ceiling. Stop wakes waits and fences subsequent calls. Cancellation drains physical work; lease release is an awaited cleanup step. Production shutdown joins the runtime before task quiescence and closing HTTP clients, runtime leases or storage; an unsettled join keeps those dependencies live. Alternatives: a larger fixed pool queues independent polls, and cancelling a proxy Future can release a lease while its callback still runs. Per-watcher sleeping threads retain unnecessary idle stacks.

Measurement: 512 simultaneously waiting watchers retain one wait thread and zero blocking workers. With 32 blocked callbacks, a healthy 33rd finishes within two seconds; shutdown remains pending beyond 1.1 seconds until callbacks return, then all callbacks and worker threads retire. Explicit stop and task cancellation retain ownership through a blocked call. These are controlled local lifecycle measurements, not provider throughput, production peak RSS or volume benchmarks. No new durable records, polling backlog or per-watcher OS thread is introduced; coroutine/handle state scales with live durable watcher definitions and retires on completion. The shared loop retires on joined process shutdown. Gates: `tests/scheduler/test_timer_wait_runtime.py`, `test_timer_live_capacity.py` and `tests/server/test_server_shutdown_lifecycle.py`.


## Scheduled-task definitions and complete dispatch feeds

Problem: 100-task owner quotas rejected creation and migration; 1,000-row runtime reads stranded accepted definitions, while native integrity/adoption scans reused the quota and response/list caps imposed further refusals. Decision: counts observe integrity with checked representability/overflow; creation retains exact identity/system-key claims without quota-only owner locking. Internal limit=0 traverses every matching row; positive limits are caller-selected projections. Native index windows share one snapshot and retain all OCC witnesses. Built-in adoption checks the complete catalog. Migration keeps resumable cursor pages, and due claims preserve occurrence fencing. Alternatives: larger count/response caps postpone refusal; a partial feed leaves work unserviced; dropping claims or OCC allows duplicate effects and stale writes.

Controlled native measurement: 2,105 same-owner definitions and their complete 9,835,556-byte response take 1.54 seconds for grouped creation plus readback on the temporary volume. A 2,001-row projection succeeds; the full cross-owner feed, late-window built-in adoption, exact deletion count and late OCC conflict retain authority. Migration reads all 130 restored records across its 128-row cursor pages and repeated restore remains idempotent. SQL creation/listing keeps all 105 same-owner records; runtime dispatch handles 1,105 due definitions instead of its old 1,000-row prefix. No thread, durable history or retained cache is added. Result vectors live only for their call and scale with selected output; complete feeds still materialize responses, so these measurements do not prove production peak RSS, arbitrarily large transport frames or provider throughput. Gates: native scheduler demand/semantic/migration tests, SQL storage contract, paired SQLite/private PostgreSQL operation lane and scheduler reconciliation.


## Credential catalog admission and complete projections

Problem: both authorities rejected the 1,001st live credential for an exact owner/tenant boundary; native list traversal and aggregate encoding repeated that quota. SQL creation also scanned the boundary to count every accepted key. Decision: retain exact active counts as integrity observations with checked representability/overflow before any native document, hash or index mutation. SQL keeps exact ID/hash conflict locking and account ownership guards without count admission. Native lists traverse all bounded index windows in one snapshot and retain every OCC witness, owner/tenant check and secret redaction. Per-record response validation is separate from aggregate list output. First-key bootstrap still checks atomic owner-boundary emptiness; account inactivity, expiry, disabled/revoked state and exact secret-hash uniqueness remain authority. Alternatives: increasing counts/aggregate bytes postpones refusal; truncated lists hide administered credentials; weakening bootstrap or authentication fences can grant unintended authority.

Controlled native measurement: grouped creation and full readback retain 1,801 live credentials and 11,356,207 public bytes in 2.43 seconds on the temporary volume. Last-window changes still abort dependent stale writes; revocation repays the active count and prevents authentication. Retiring all transactions repays retained byte/handle observations to zero. SQL admits 1,001 same-boundary keys with complete order, cross-owner/tenant exclusion, duplicate-secret refusal, bootstrap non-creation and revoked-key rejection. No new worker, durable audit or retained cache is added. Output vectors scale with the administered catalog and retire after the call; the measurement does not certify arbitrary transport-frame sizes, production peak RSS or provider throughput. Gates: native credential tests, credential demand SQL/private-PG lane, bearer/account contracts and differential credential demand scripts.

## Centralized offline performance catalog

**Problem:** measured storage results are distributed across benchmark receipts
and experiment evidence, making release-to-baseline comparison difficult and
encouraging comparisons between incompatible workloads.

**Decision:** `scripts/db_performance_catalog.py` provides an offline operator
interface for importing immutable source snapshots, listing version-labelled
results, and exporting detailed SVG, Markdown and JSON comparisons. The
machine-readable authority is `contracts/db_performance_catalog_v1.json`.
Catalog data lives under `data/benchmarks` with an explicit owner namespace;
this is operator measurement storage, not a second application database.
The tool never opens a live authority or changes backend selection.

A baseline is selected explicitly. Scenario, environment, metric, unit and
optimization direction must agree before results are compared. Source digests,
version labels, raw window measurements, aggregation method, correctness,
coverage gaps and absolute latency budgets remain available with the chart.
Median window P99 is never labelled pooled request P99; missing evidence is
not a passing gate. Historical evidence certifies only its recorded build and
scope, not the current checkout. A faster result can still fail its budget.

**Alternatives rejected:** one universal speedup hides regressions and mixes
engine-floor SQLite with application-semantic SQLite; a new live-database table
would couple offline reports to runtime availability and expand authority;
rerunning every benchmark to redraw a chart wastes resources and destroys the
stable evidence needed for release decisions. Imports retain source snapshots
rather than relying on mutable external paths. Retention is explicit: no
background worker, automatic eviction, or deletion of measurement evidence.

Verification: `tests/tofudb/test_db_performance_catalog.py` exercises imports,
idempotency, provenance/corruption refusal, comparison boundaries and exports.

### Catalog operator interface

From the primary checkout:

```sh
python3 scripts/db_performance_catalog.py import --owner db-team --version review-20261010 \
  --source PATH/TO/tofudb_direct_sqlite_pair_20261008.json  # retired evidence/ artifact; recover from git history
python3 scripts/db_performance_catalog.py list --owner db-team --version review-20261010
python3 scripts/db_performance_catalog.py report --owner db-team --version review-20261010 \
  --id RUN_SHA256 --baseline sqlite --candidate native
```

Import prints a run ID; substitute it for `RUN_SHA256`. Reports default to
`data/benchmarks/performance/OWNER/VERSION/reports/`. Source snapshots are
owner-deduplicated; report SVG, Markdown and JSON are immutable and
content-addressed. Listing returns bounded summaries with `--limit`/`--offset`.

For release-to-release native comparisons, import schema-conforming
`db-performance-catalog/v1` files recording identical fixture, scenario and
environment identities and different native build versions. Run `report` with
`--version NEXT_RELEASE --id CANDIDATE_SHA256 --baseline native --candidate native
--baseline-version BASELINE_RELEASE --baseline-id BASELINE_SHA256`.
Historical adapters establish an environment only within their recorded pair:
unknown environment refuses cross-source comparison. Generic evidence enables
new reproducible cohorts; relabelling cannot create a missing witness.

The default is checkout-local. With several worktrees, pass the same canonical
`--catalog /absolute/path/to/data/benchmarks/performance` in every checkout.
Owner namespaces organize offline results, not application authentication;
roots must be operator-controlled. Inputs are regular files bounded to 16 MiB;
exports/listings are bounded. No background worker or automatic deletion is
added. Persistent growth occurs only on explicit imports/exports; retain or
reclaim these operator artifacts deliberately, separately from live user data.

## Native comparison-card exports

`db_performance_catalog.py report` also emits a `rich.json` payload conforming
to `contracts/rich_response_v1.schema.json`. It carries original measured values,
direction, window aggregation, matching absolute budgets, recorded source/build
provenance and coverage limits. Failed correctness/coverage/budget gates outrank
relative improvement; an absent gate remains unknown. Payloads use bounded
comparison cards and evidence blocks, not generated scripts or filesystem image
URLs. Full raw windows remain in the adjacent report JSON. Producers may publish
the payload through the native rich-response tool; publication and owner checks
are distinct from the offline catalog's measurement identity.

## Production-shaped load test

**Problem:** the comparison harness above measures operation classes on
synthetic corpora; it cannot answer how the authority behaves when the
production Agent fleet grows 3-5x, because the fleet's real traffic
(streamed Turn events with tool rounds and telemetry, durable-worker claims and
heartbeats, browser sync polling, execution-host bookkeeping reads, sidebar,
search and pruning) never runs together against one authority.

**Decision:** `scripts/tofudb_load_test.py` starts one isolated authority
through the production `StorageSupervisor` and drives that mix through the
application's own storage code (`lib.turn_lifecycle` create/dispatch/start/
record/settle, `lib.durable_worker.StorageWorkerJobStore`,
`lib.conversation_sync` changes/snapshot, `lib.conversations` list/search,
`lib.tasks_pkg.event_log.prune_task_event_retention`) at multiples of a peak
measured on production. Its `PRODUCTION_PROFILE` records each input next to
its source:

- the conversation census (`GET /api/v4/conversations?meta=1`);
- running attempts (execution-host executor threads);
- the durable event rate (engine `commitStages` `turn_event_store`);
- the API-host read mix (`tofu_storage_rpc_seconds` interval deltas);
- execution-host bookkeeping reads (engine listener request rate minus the API
  host's share);
- payload byte shares from inspector exports.

A scale multiplies concurrency and every rate. Agents keep production's
admission rule of one running Turn per conversation lane. The seed is
synthetic: same conversation census and per-conversation shape, no user
content.

Retention runs the maintenance loop's own sweep, so prune pages keep
production's cutoffs (6 h streaming, 30 d structural) and page size, looping
on `has_more`. Until 2026-11-12 the harness sent one 500-row prune with a 1 h
cutoff. On a root whose events were older than an hour, that delta outgrew the
overlay envelope and merged the overlay under the writer: about 2 s per sweep,
with every writer queued behind it. Production never issues that page. A run
that finished within an hour of its seed never pruned under the old harness,
so fresh-seed results are unaffected.

**Alternatives rejected:**

- Restoring a production backup (`tofu-db restore`) to get a byte-exact copy.
  It is not feasible on this host. Restore is a single-threaded traversal with
  a file fsync plus a directory fsync per block. The backup held ~38M block
  files, and its newest complete generation was 18 days old (2026-11-08).
  Restore also writes its pin into the source backup directory.
- Replaying wire payloads captured from production. No request capture exists.
  Turn-event payloads carry projection patches and worker-claim fences that
  are valid only for their original attempt.
- Driving a full isolated server with a mock model, for this harness. Its
  question is the authority's capacity under the measured storage mix; HTTP,
  executor and model pacing would only add noise to that answer, and this
  harness keeps that scope. The owner reversed the rejection for the Agent
  process itself on 2026-11-12: see
  [Agent-loop load test](#agent-loop-load-test).

Run it from a checkout. `--engine` defaults to `$TOFUDB_BIN`; pass the admitted
production binary to measure the running build. Engine admission requires the
binary to match the checkout's engine source. To measure an older build, run the
script from a `git archive <commit> | tar -x` export of the commit it was built
from.

```sh
# first run: seed the production census, then 1x/3x/5x for 5 minutes each
python3 scripts/tofudb_load_test.py --root /tmp/tofudb-load --engine "$TOFUDB_BIN"
# later runs against the same seeded authority (e.g. before/after a fix)
python3 scripts/tofudb_load_test.py --root /tmp/tofudb-load --reuse-seed --scales 1,5 --duration 300
# production medium: a private directory on the dolphinfs mount, outside any checkout
python3 scripts/tofudb_load_test.py --root /mnt/your-fs --durability normal
# the same mix against the SQL Sidecar (SQLite, synchronous=FULL); needs no engine
python3 scripts/tofudb_load_test.py --root /mnt/your-fs --backend sqlite
```

The child runs with production's storage RPC deadline
(`TOFU_STORAGE_RPC_DEADLINE_S=20`, from the live hosts' environment) for both
backends. Under the 5 s product default, the Sidecar's seed failed on a loaded
host.

Each phase writes `ROOT/results/<run-id>/report.json` and `report.md`:

- exact per-operation n, rate, p50/p90/p99/max and error codes;
- settlement split: `turn.event.record:terminal` holds Turns that reached their
  planned length; `turn.event.record:terminal:phase_stop` holds the Turns the
  phase stop settles all at once (one per agent). That herd waits on the one
  client process, so it is not per-Turn settlement latency. Reports before
  2026-11-09 mix both under `turn.event.record:terminal`; at 1x for 180 s, 21
  of its 29 samples were the herd;
- event outcomes (carried vs coalesced);
- engine `system.metrics` interval deltas (authority wait, command execution,
  WAL and barrier sync, projection materialization);
- engine CPU and RSS;
- WAL and block-device bytes per user-visible change (streamed event or
  settled Turn).

`--durability` pins the WAL tier. It defaults to production's probed `normal`;
without it a local volume would probe `strict` and compare unlike tiers.

Safety: `ROOT` is created 0700. A root inside a live Tofu data directory is
refused. The measuring child runs with a scrubbed environment whose data root,
`HOME` and `TMPDIR` are under `ROOT`, so it neither reads nor writes production
state. The engine is pinned by copy and SHA-256, and the harness stops the
authority it started. Delete `ROOT` when done.

Limitations:

- Execution-host traffic it cannot issue with valid domain payloads
  (`project.dispatch.*`, `integration.workspace.*`, `raw_archive.*`,
  `swarm.agent.*`, `log_aggregate.flush`) is modelled as equal-rate point
  reads.
- The seed reproduces the census, not production's total bytes (about 1 GB
  against a ~0.5 TB backup), so the cold-read and GC-mark costs of a
  multi-terabyte catalog are absent.
- WAL and materialization per event (re-measured 2026-11-12 from the live
  engine's `system.metrics`, read through the authenticated control operation,
  which never takes the authority mutex). Synthetic events write 160-230 KB
  of WAL per change. Production wrote 155 KB of WAL per event over the
  daemon's first 2.75 h, and 421 KB in a calm 5-minute window. Both figures
  are all WAL bytes divided by `turn_event_store` calls, so they are upper
  bounds. The 16 KB per event in
  [large-history event persistence](STORAGE_OPERATIONS.md#large-history-event-persistence)
  describes a text-tail-only stream; tool rounds and other commits still
  cost the same order as the old ~290 KB. The old "decodes a ~4 MB checkpoint
  per event" is superseded by committed live-head residency: production
  materialized a head for only 8% of events (7,635 of 94,595) and 14% in the
  calm window, at 3.7-5.6 MB each. That averages 0.3-0.8 MB of decoded
  checkpoint per event. 1x remains somewhat optimistic for WAL bytes.
- All clients share one Python process. The production execution host is
  likewise one process (0.93 cores average on 2026-11-08). Before 2026-11-12
  the harness's sync viewers never advanced their cursor (see the 2026-11-12
  re-measurement), which made the client GIL-bound from 3x. With the fix it
  uses 0.62 cores at 5x.
- Every agent starts its first Turn at the phase start, so most
  create/dispatch/claim/start samples of a short phase are one herd too.

Baseline, 2026-11-08 (production engine `892b231a`, WAL `normal`). Columns
are p50/p99 in ms. 1x is 21 attempts and 15.3 events/s. Seed: 1,177
conversations and 8,468 Turns. No operation failed in any phase.

| operation | dolphinfs 1x | dolphinfs 3x | dolphinfs 5x | md0 HDD 1x | md0 HDD 3x | md0 HDD 5x |
|---|---|---|---|---|---|---|
| turn.event.record | 3.6/275 | 58/492 | 809/2,148 | 17/2,097 | 166/6,123 | 970/3,224 |
| turn.sync.changes | 12/58 | 88/610 | 847/2,395 | 13/48 | 82/833 | 991/2,724 |
| point reads (turn.get metadata) | 0.4/12 | 31/407 | 725/1,990 | 0.4/9 | 26/514 | 796/2,162 |
| worker_job.heartbeat | 0.9/18 | 86/531 | 673/1,963 | 0.7/325 | 147/2,713 | 919/2,172 |
| turn.create_pair | 17/54 | 48/279 | 90/1,415 | 53/85 | 76/961 | 444/1,685 |
| worker_job.claim_next | 8/37 | 64/175 | 171/1,652 | 9/70 | 163/747 | 811/2,626 |
| authority_wait avg (ms) | 0.5 | 1.5 | 13.9 | 47 | 138 | 52 |
| completed operations/s | 332 | 578 | 224 | 334 | 554 | 206 |
| events / sync reads, % of target | 97 / 98 | 93 / 79 | 59 / 34 | 91 / 98 | 75 / 75 | 53 / 31 |
| client CPU (cores) | 0.26 | 1.05 | 1.07 | - | - | - |

At 1x the harness completes ~333 operations/s, close to the ~350 requests/s
the production engine served at the same time. From 3x the single client
process is GIL-bound. At 5x completed operations fall below the 1x rate on both
media and every operation queues. The 2026-11-12 re-measurement traced this
collapse to a harness defect, not to the engine: the sync viewers' cursor never
advanced. On md0 the WAL barrier sync averages 110 ms,
against 0.56 ms on dolphinfs, so the local HDD is the worse medium for the
authority.

Re-measurement, 2026-11-12. Engine source: checkout `214650e681`, engine
source digest `0f6c7f2e`, binary SHA-256 `ecd483be`. Medium: dolphinfs, WAL
`normal`, 300 s per phase, fresh seed for each harness. The run covers the
engine commits after 11-08, including:

- no projection copy per event;
- validation from the pending overlay;
- validation rehearsed before the writer;
- the overlay drain thread;
- worker claims and heartbeats prepared off the writer.

Rerunning the old harness reproduced the 11-08 collapse on the new engine.
The `requestProfile` explained it. The harness read
`syncSequence`/`nextAfter` from `turn.sync.changes` pages, but those pages
carry `head`, `hasMore` and `events[].syncSeq`. Its cursor never advanced, so
every poll replayed the conversation's whole change history. Each call moved
5.3 MB at 3x and 6.4 MB at 5x (`ir_result_moved_bytes`), with 26-30 ms of
`ir_steps`. That was 60-64% of all engine dispatch time. The Python client
then decoded ~300 MB/s of JSON under one GIL. Production's API host serves
`turn.sync.changes` in 7.3 ms mean, with 83% under 5 ms.

The harness now keeps one viewer per conversation, as the browser does. The
viewer opens from a snapshot (`turn.sync.snapshot:open`) and advances past the
last delivered change, or to `head` once `hasMore` is false. The 11-08 table
measured that harness defect, not engine capacity. Each phase also writes the
engine's full `system.metrics` at its quiescent edges to
`metrics-<scale>x.json`. `--request-profile` records every request in
`requestProfile` (on the fixed harness at 5x it cost 3% throughput and
dropped 0.1% of samples).

Columns are p50/p99 in ms. "old harness" is the 11-08 harness on the
11-12 engine; "fixed" is the corrected harness. No operation failed in any
phase.

| operation | 11-08 1x | 11-08 3x | 11-08 5x | old harness 1x | old harness 3x | old harness 5x | fixed 1x | fixed 3x | fixed 5x |
|---|---|---|---|---|---|---|---|---|---|
| turn.event.record | 3.6/275 | 58/492 | 809/2,148 | 1.9/19 | 67/757 | 924/2,098 | 1.7/23 | 1.9/30 | 2.3/50 |
| turn.sync.changes | 12/58 | 88/610 | 847/2,395 | 13/48 | 103/952 | 1,013/2,412 | 0.32/2.1 | 0.40/4.8 | 0.55/7.9 |
| point reads (turn.get metadata) | 0.4/12 | 31/407 | 725/1,990 | 0.37/10 | 33/549 | 893/2,009 | 0.30/0.69 | 0.38/2.8 | 0.48/4.6 |
| worker_job.heartbeat | 0.9/18 | 86/531 | 673/1,963 | 0.84/6.4 | 99/684 | 851/2,253 | 0.80/3.0 | 0.92/22 | 1.2/42 |
| turn.create_pair | 17/54 | 48/279 | 90/1,415 | 7.2/28 | 33/394 | 59/1,763 | 7.0/28 | 6.1/37 | 8.1/171 |
| worker_job.claim_next | 8/37 | 64/175 | 171/1,652 | 3.0/30 | 47/500 | 135/1,578 | 2.4/27 | 2.9/37 | 4.2/213 |
| authority_wait avg (ms) | 0.5 | 1.5 | 13.9 | 0.036 | 0.031 | 0.10 | 0.028 | 0.12 | 0.13 |
| wal_barrier_wait avg (ms) | - | - | - | 0.36 | 0.77 | 1.2 | 0.67 | 0.83 | 1.2 |
| completed operations/s | 332 | 578 | 224 | 336 | 515 | 199 | 341 | 1,013 | 1,679 |
| events / sync reads, % of target | 97 / 98 | 93 / 79 | 59 / 34 | 100 / 98 | 90 / 73 | 56 / 31 | 100 / 101 | 101 / 99 | 100 / 99 |
| client CPU (cores) | 0.26 | 1.05 | 1.07 | 0.28 | 1.05 | 1.07 | 0.10 | 0.34 | 0.62 |
| engine CPU (cores) | - | - | - | 0.47 | 1.59 | 2.03 | 0.22 | 0.62 | 1.04 |

With the fix, 5x offers and completes its full target: 1,679 operations/s,
5.0x the 1x rate. The client is no longer saturated. Engine dispatch for
`turn.event.record` is 2.96 ms mean at 5x, against a client p50 of 2.1-2.3
ms. On the old harness at 5x it was 3.75 ms against a client p50 of
924-1,683 ms, so that latency was client queueing. Authority holds total
5.4 s per 300 s at 5x (under 2% writer duty).

At 5x the profiled fixed run (`--reuse-seed`, so the authority already held
one full run of growth) spent 1,553 s of dispatch wall time in 300 s. Its
largest engine-side costs:

1. **Cold reads on the FUSE volume.** `vfs_open` 543 s, `vfs_read` 507 s and
   `vfs_metadata` 237 s are 83% of dispatch time. Nearly all of it is in
   `turn.create_pair` (2.9 s mean; 191 of 293 calls over 134 ms; slowest
   7.3 s, 3.2 s of it `vfs_open`) and in opening snapshots of newly watched
   conversations (1.5 s mean). These reads run off the writer: create_pair
   holds it for 0.2 ms per call. The unprofiled fresh-seed 5x shows the same
   tail at a smaller size: create_pair p99 171 ms, claim_next 213 ms,
   snapshot open 228 ms.
2. **FIFO admission.** `fifo_wait` totals 80.5 s (5.2%), almost all in
   `turn.create_pair` (271 ms per call). Creations queue behind each other's
   cold-read-bound execution.
3. **WAL barrier acknowledgement.** `wal_barrier_wait` totals 20.9 s: 0.87
   of `turn.event.record`'s 2.96 ms and 1.6 of `worker_job.heartbeat`'s
   1.87 ms. Its average grows from 0.67 ms (1x) to 1.2 ms (5x), while
   `wal_barrier_sync` stays at 0.33-0.44 ms.

Authority wait, materialization (`projection_head_materialize` 2.4 ms,
1,084 calls per 300 s) and `ir_steps` (0.14 ms per call) are not 5x
bottlenecks. md0 was not re-measured.

TofuDB against the SQL Sidecar on this load, 2026-11-12. This is the first
comparison of the two production stores on the production-shaped mix.
Earlier sidecar comparisons were operation micro-suites, or capacity runs
whose sidecar leg had a shorter deadline.

Setup:

- the fixed harness, `--backend tofudb|sqlite`, with the same seed (1,177
  conversations, 8,468 Turns, 1.49 GB);
- dolphinfs, 300 s per phase, the 20 s RPC deadline for both backends;
- TofuDB engine `ecd483be` (source `214650e681`) at WAL `normal`;
- the Sidecar at SQLite WAL with `synchronous=FULL`, the only tier it
  ships. That tier is the stricter of the two, so this leg pays more
  fsync work than TofuDB `normal`.

The legs ran one after the other on a shared host (load average 16-39). The
TofuDB leg reproduces the independent "fixed" run above within noise (340 /
1,014 / 1,680 against 341 / 1,013 / 1,679 operations/s). Columns are p50/p99
in ms.

| operation | TofuDB 1x | Sidecar 1x | TofuDB 3x | Sidecar 3x | TofuDB 5x | Sidecar 5x |
|---|---|---|---|---|---|---|
| turn.event.record | 1.8/23 | 23/1,872 | 1.8/25 | 137/3,557 | 2.3/61 | 2,006/9,444 |
| turn.event.record:terminal | 42/520 | 253/2,716 | 40/74 | 629/3,683 | 42/263 | 3,729/9,964 |
| turn.sync.changes | 0.4/2.3 | 0.9/154 | 0.4/3.7 | 1.1/39 | 0.6/8.8 | 6.3/472 |
| point reads (turn.get metadata) | 0.3/0.8 | 0.7/42 | 0.4/1.8 | 0.8/18 | 0.5/5.1 | 5.0/187 |
| worker_job.heartbeat | 0.8/23 | 18/897 | 0.9/12 | 141/3,065 | 1.2/32 | 1,131/5,086 |
| turn.create_pair | 6.3/42 | 158/640 | 5.2/37 | 653/2,086 | 7.5/75 | 2,776/4,480 |
| worker_job.claim_next | 6.3/24 | 355/498 | 2.3/39 | 1,423/1,567 | 5.6/79 | 2,443/4,128 |
| worker_job.complete | 4.4/32 | 334/11,460 | 3.2/100 | 3,862/12,373 | 4.3/187 | 4,006/10,166 |
| conversation.list | 7.5/10 | 34/648 | 7.4/31 | 31/172 | 9.1/54 | 204/1,143 |
| completed operations/s | 340 | 315 | 1,014 | 933 | 1,680 | 1,290 |
| streamed events (% of TofuDB) | 4,587 | 4,121 (90%) | 13,902 | 10,431 (75%) | 23,115 | 7,589 (33%) |
| settled Turns | 46 | 40 | 133 | 101 | 223 | 87 |
| failed requests | 0 | 0 | 0 | 0 | 0 | 28 |
| store CPU (cores) | 0.24 | 0.27 | 0.65 | 0.80 | 1.02 | 1.67 |
| store RSS at phase end (GiB) | 2.5 | 0.41 | 4.9 | 0.41 | 5.8 | 0.37 |

At 5x the Sidecar's 28 failures are `database_timeout` on the phase-stop
settlement herd. Its throughput falls on the write path:

- the single SQLite writer serializes event, claim, heartbeat and completion
  commits;
- at 5x it streams a third of TofuDB's events, with event p50 at 2 s;
- its point reads stay fast only because they bypass the writer;
- seeding the fixture took 1,049 s, against TofuDB's 46.5 s.

The reported native latency cells were lower and throughput cells higher;
this was Normal versus SQLite FULL, not an equal-durability result. Native
resident memory was 6–16× the Sidecar's RSS. The earlier micro-suite results
still hold: about 2x the Sidecar's durable footprint, and point-read P99
above raw SQLite.

Not covered:

- a TofuDB `strict` leg, which would match the Sidecar's durability tier;
- md0;
- repeated Sidecar runs.

Verification: `tests/tofudb/test_tofudb_load_test.py` covers environment
scrubbing, live-root refusal, exact percentiles, the phase-stop settlement
split, interval accounting, the request-profile switch, sync-cursor
advance, production-shaped retention pages, and the Sidecar leg's environment
and engine-free launch.

## SQL Sidecar on DolphinFS (2026-11-15)

**Problem (owner, 2026-11-15):** the TofuDB-vs-Sidecar comparison above
understated a well-configured SQLite. Audit of the Sidecar and the harness
found these handicaps (decisions in [STORAGE.md](STORAGE.md#sqlite)):

| Handicap | Measured before | After |
|---|---|---|
| H1 harness: children lost `LD_PRELOAD`/`LIBBGFS_*`, so both legs ran kernel FUSE, not the userspace DolphinFS client production uses | child env had no preload | preload, `LIBBGFS_*`, `FAULTFS_*` forwarded (`test_child_environment_keeps_the_medium_client_for_both_backends`) |
| H2 harness: Sidecar (`synchronous=FULL`, sync per commit) compared with TofuDB `normal` (deferred barrier) | unequal durability | comparison of record uses TofuDB `--durability strict`; help text says so |
| H3 one TCP connect + accept + handler-thread start per RPC (storage.v1) | `record.get` 1 thread 0.36 ms p50 / ~1,200 RPC/s accept ceiling | keep-alive pooled sockets: 0.065 ms p50, 11,400 ops/s; 32 threads 5,077 ops/s (was 432-667) |
| H4 read-pool churn: a release above the idle hint closed the connection; each new reader re-parses the 255-object schema (~0.8 ms) under SQLite's global mutex | with H3 fixed alone, 3x phase: 7.2 store cores, 371 events (collapse) | 3x: 0.41 cores, 4,050 events, event p99 73 ms (plain /dev/shm) |

Rejected after measurement: a background PASSIVE checkpointer (no 1x/3x win;
one 5x run had event p99 3.1 s and a 25,011-frame WAL), `synchronous=NORMAL`
(weaker acknowledged durability), `locking_mode=EXCLUSIVE` (no concurrent
readers), local-disk placement (no persistent local volume). Durability is
unchanged: WAL, `synchronous=FULL`, inline `wal_autocheckpoint=4096`. No
setting, env var or threshold was added.

**Medium.** The DolphinFS pool this account may write measured 72-77 ms per
4 KiB `fdatasync` on 2026-11-15 (preload and kernel FUSE alike; `/tmp` 0.05
ms), and production's volume is off-limits for benchmark I/O. The recorded
runs therefore emulate production's DolphinFS barrier (0.77 ms per
`fdatasync`, [placement measurements](TOFUDB_FASTPATH.md#automatic-selection-and-capacity-governance-2026-10-27))
on `/dev/shm` with `benchmarks/agent_storage/faultfs.c`; both stores paid it
(36-38 k injected barriers each). Cold-read and metadata latency of the real
volume are not emulated.

Reproduce the 2026-11-15 session. This predates the tmpfs-root refusal; use
the 2026-11-17 command below for new runs:

```sh
cc -O2 -shared -fPIC -o /tmp/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
for b in "--backend sqlite" "--backend tofudb --engine $TOFUDB_BIN --durability strict"; do
  R=/dev/shm/sidecar-cmp-$(date +%s); mkdir -p $R
  LD_PRELOAD=/tmp/faultfs.so FAULTFS_ROOT=$R FAULTFS_FSYNC_US=770 FAULTFS_STATS=$R/faultfs.stats \
    python3 scripts/tofudb_load_test.py --root $R $b --seed-scale 0.2 --scales 1,3,5 --duration 90
done
```

Results, p50/p99 ms; 0.2x seed (1,404 Turns, 229 MB), 90 s phases, engine
`4036b5a6` (production), Sidecar source = this change. "Before" is the
pre-change Sidecar on the same medium; sidecar A/B ran before/after the
TofuDB leg.

| op (1x / 3x) | Sidecar before | Sidecar after (A) | TofuDB strict |
|---|---|---|---|
| turn.event.record | 3.1/397 · 3.3/162 | 2.2/147 · 2.5/50 | 1.9/44 · 1.9/56 |
| turn.sync.changes | 1.05/65 · 0.90/57 | 0.47/3.1 · 0.47/4.1 | 0.31/2.0 · 0.30/7.2 |
| record.get | 0.62/64 · 0.53/55 | 0.26/1.3 · 0.22/3.6 | 0.21/0.66 · 0.21/0.87 |
| worker_job.heartbeat | 1.7/57 · 2.1/78 | 1.3/2.8 · 1.4/7.2 | 1.1/1.4 · 1.2/2.6 |
| worker_job.claim_next | 22/92 · 52/80 | 17/56 · 44/54 | 6.3/10 · 22/36 |
| worker_job.complete | 69/204 · 99/856 | 100/139 · 101/494 | 16/18 · 56/64 |
| phase-stop settlement herd | 232/263 · 2,367/2,444 | 229/355 · 1,245/1,346 | 34/48 · 100/120 |
| completed ops/s | 297 · 939 | 339 · 1,006 | 340 · 1,014 |
| failed requests | 0 · 0 | 0 · 0 | 0 · 0 |

At 1x/3x the optimized Sidecar matches TofuDB throughput, removes the
~60 ms p99 floor on every point read (the per-RPC connect), and stays within
1.1-2x of TofuDB p50 on reads; TofuDB still wins writer-bound claims,
completions and the settlement herd. The 5x leg of that session (host load
40-258) was inconclusive and is superseded by the record below.

### Comparison of record at 1x/3x/5x (2026-11-17)

Host: 180-CPU, 203 GB CodeLab, load average 2.8-8.6 through every leg
(sampled every 5 s; per-leg min/median/max below). Medium: local XFS
(`$TMPDIR`) plus `faultfs` injecting 0.77 ms per `fdatasync`; the harness
now refuses tmpfs roots (`_refuse_memory_root`), so `/dev/shm` is no longer
used. Sidecar = this source (WAL, `synchronous=FULL`); TofuDB = engine built
from this source (`source_sha256` 5cb942c407fe, `--durability strict`,
certified with `serverctl_pkg.native_engine --prepare-only`). Seed 0.2x
(1,404 Turns, 229 MB), 90 s phases, fresh root per leg, legs ABAB:
Sidecar r1, TofuDB r1, Sidecar r2, TofuDB r2. Values are p50/p99 ms; each
cell is r1 · r2.

| leg | load1 min/median/max | injected barriers |
|---|---|---|
| Sidecar r1 | 2.8 / 4.2 / 6.1 | 15,306 |
| TofuDB r1 | 3.3 / 4.4 / 8.6 | 31,984 |
| Sidecar r2 | 2.8 / 3.3 / 5.5 | 15,336 |
| TofuDB r2 | 2.9 / 4.6 / 8.2 | 32,198 |

| op | scale | Sidecar | TofuDB strict |
|---|---|---|---|
| turn.event.record | 1x | 2.39/90 · 2.31/102 | 2.00/48 · 1.97/46 |
| | 3x | 2.42/109 · 2.40/53 | 2.02/74 · 2.03/65 |
| | 5x | 2.68/108 · 2.70/156 | 2.08/112 · 2.08/108 |
| turn.sync.changes | 1x | 0.57/2.3 · 0.58/1.9 | 0.36/1.3 · 0.34/1.3 |
| | 3x | 0.62/24 · 0.59/2.9 | 0.36/2.3 · 0.35/1.8 |
| | 5x | 0.64/10 · 0.65/5.6 | 0.38/2.3 · 0.38/2.2 |
| record.get | 1x | 0.26/0.69 · 0.25/0.59 | 0.19/0.52 · 0.19/0.51 |
| | 3x | 0.23/2.4 · 0.22/1.3 | 0.21/0.68 · 0.21/0.64 |
| | 5x | 0.23/3.4 · 0.23/3.1 | 0.23/0.85 · 0.23/0.85 |
| turn.get:metadata | 5x | 0.32/5.0 · 0.32/4.7 | 0.32/1.5 · 0.31/1.6 |
| worker_job.heartbeat | 5x | 1.73/13 · 1.68/16 | 1.38/3.2 · 1.35/2.9 |
| turn.create_pair | 1x | 27/35 · 27/57 | 18/23 · 19/22 |
| | 5x | 74/118 · 69/124 | 71/83 · 69/81 |
| worker_job.claim_next | 1x | 13/17 · 16/34 | 6.5/11 · 8.2/11 |
| | 3x | 42/52 · 43/78 | 22/32 · 22/27 |
| | 5x | 89/101 · 74/98 | 39/61 · 36/71 |
| worker_job.complete | 1x | 31/134 · 15/122 | 16/20 · 16/19 |
| | 3x | 75/213 · 70/561 | 59/64 · 59/63 |
| | 5x | 279/482 · 104/865 | 105/109 · 104/109 |
| conversation.list | 5x | 7.4/20 · 6.4/7.6 | 2.6/3.2 · 2.7/3.6 |
| phase-stop settlement herd | 1x | 173/204 · 187/194 | 42/87 · 37/82 |
| | 3x | 575/603 · 983/1,016 | 117/132 · 108/127 |
| | 5x | 629/922 · 1,400/1,450 | 181/217 · 185/219 |
| completed ops/s | 1x / 3x / 5x | 340/1,006/1,679 · 341/1,010/1,673 | 341/1,016/1,690 · 341/1,017/1,690 |
| events (5x) | | 6,953 · 6,946 | 6,967 · 6,978 |
| failed requests | every phase | 0 · 0 | 0 · 0 |
| store CPU cores (5x) | | 0.43 · 0.93 | 0.41 · 0.42 |

Verdict:

- **Throughput: tie at every scale.** Both stores keep up with the offered
  load through 5x (1,670-1,690 completed ops/s, 109 Turns) with zero
  failures; neither saturates on this host.
- **Point reads (`record.get`, `turn.get:metadata`): tie at p50; TofuDB
  wins p99** (0.85 vs 3.1-3.4 ms at 5x). `turn.sync.changes` p50 is
  0.38 vs 0.65 ms.
- **Streamed events: TofuDB is ~20% faster at p50** (2.1 vs 2.7 ms at 5x);
  the p99 is the same, 108-156 ms in both.
- **Writer-bound steps: TofuDB wins clearly.** `claim_next` p50 is about
  half, `complete` p99 109 vs 482-865 ms, and the end-of-phase settlement
  herd takes 181-219 ms vs 629-1,450 ms. `create_pair` ties at p50, and
  TofuDB's p99 is lower.
- **The Sidecar wins no operation.** Its closest result is parity on
  point-read p50, `create_pair` p50 and throughput. TofuDB does this while
  paying 2.1x the durable barriers (about 32 k vs about 15.3 k injected).
  The emulated medium charges only barrier latency, not cold reads or
  metadata, so the real volume's read costs are not covered. On the real
  volume ([rerun below](#the-same-comparison-on-the-real-dolphinfs-volume-2026-11-17)),
  the Sidecar's per-write cost makes it fall behind at 3x and 5x, so the
  throughput tie holds only at 1x.

**The failed 2026-11-15 5x Sidecar run ("B": 1,351 failures, 36 s writer
stall) was host CPU starvation, not a Sidecar defect.** Its raw reports were
on a volatile `/dev/shm` of the previous host and are gone. What survives is
its summary (event p50 34 ms / p99 8.2 s, `sync.changes` p99 719 ms) and the
same session's four interleaved 5x repeats. All four failed with
`StorageError: Storage request timed out`, the pre-change Sidecar
(`/tmp/sc-before`) and the final one alike, at load averages 95-258 with 1-3
writer stalls each. Evidence:

1. The same Sidecar source passes 5x with 0 failures in four fresh-root
   legs on this host: r1/r2 above, and two earlier legs at load
   8-123 (6,932 and 6,958 events).
2. Controlled starvation reproduces the signature. The Sidecar 5x leg plus
   32 busy loops, all pinned with `taskset` to 8 CPUs: event p50/p99
   175 ms / 10.2 s, `sync.changes` 45 ms / 9.9 s, `record.get` 23 ms / 3.4 s,
   6,139 events. With 96 loops on the same CPUs: 2 `worker_job.complete`
   `database_timeout` failures after 26 s (plus 2 retried query timeouts).
3. Throughout both starved runs, the writer's own commit latency stayed
   normal: p50 33 ms, max 364-424 ms, 0 `stall_interrupts`. The time is lost
   to request and agent threads waiting for a CPU, not to SQLite, the WAL or
   the writer queue.
4. The failure is independent of this change: the pre-change Sidecar failed
   the same way on the same overloaded host.

Not reproduced: run B's specific 36 s writer-watchdog stall. The starved
runs here never crossed the 15 s stall grace. No Sidecar code change
follows from this diagnosis.

Reproduce the record. Run it from a checkout, with `$TOFUDB_BIN` built from
that checkout's engine source and certified as above. The root must be on
disk:

```sh
cc -O2 -shared -fPIC -o $TMPDIR/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
for b in "--backend sqlite" "--backend tofudb --engine $TOFUDB_BIN --durability strict" \
         "--backend sqlite" "--backend tofudb --engine $TOFUDB_BIN --durability strict"; do
  R=$(mktemp -d); LD_PRELOAD=$TMPDIR/faultfs.so FAULTFS_ROOT=$R FAULTFS_FSYNC_US=770 \
    FAULTFS_STATS=$R/faultfs.stats .venv/bin/python3 scripts/tofudb_load_test.py --root $R $b \
    --seed-scale 0.2 --scales 1,3,5 --duration 90 --run-id rec
done
```

The starvation reproduction runs the same Sidecar command with `--scales 5`
under `taskset -c <8 CPUs>`, with N `sh -c 'while :; do :; done'` loops
pinned to the same CPUs.

### The same comparison on the real DolphinFS volume (2026-11-17)

The record above emulates DolphinFS with 0.77 ms per barrier on local XFS.
This rerun puts both stores' data on the real volume production uses
(`fuse.bgfuse`, `/mnt/your-fs`), in a
scratch directory `FMG/your-username/tofu-dfsbench-scratch-20261117/leg-*`,
next to the live checkout but not inside it. Each leg reached it through
production's preloaded userspace client (`LD_PRELOAD` plus `LIBBGFS_*`, as in
the live engine's environment). Each leg's root was deleted after its report
was copied, and the scratch directory was removed at the end. The rest
matches the local record: same host, same seed (0.2x, 1,404 Turns, 229 MB),
90 s phases at 1x/3x/5x, fresh root per leg, ABAB legs (Sidecar r1, TofuDB
r1, Sidecar r2, TofuDB r2). The Sidecar used its optimized defaults (WAL,
`synchronous=FULL`). TofuDB used the same engine source as the local record
(`source_sha256` 5cb942c407fe, built with the FMG toolchain and certified
with `--prepare-only`) and production's engine settings. The harness requested
`--durability strict`, but the pre-fix launcher dropped an explicit Strict
setting. Effective TofuDB durability is **unverified (likely Normal)**: the
automatic tier on DolphinFS selected Normal in the later reproduction, and
these per-leg reports are gone. This is not an equal-durability comparison.
`faultfs` ran with `FAULTFS_FSYNC_US=0`, so it
injected nothing and only counted in-scope barriers and the time spent in
the real call (`medium_ns`).

**DolphinFS defect found: I/O on an unlinked open file fails.** In the first
ABAB run, the Sidecar fell back from group commit on almost every batch: 50,
384 and 1,070 fallbacks out of 53, 388 and 1,076 batches at 1x/3x/5x. Its
turn-search projection also logged 735 `disk I/O error`s. Both stores ran
with 0 such errors on local disk. The causal chain:

1. With the SQLite cause logged, every fallback was `SQLITE_IOERR_WRITE`
   inside a job (`turn.event.record`, `create_pair`, ...). SQLite had
   already rolled the transaction back, so `ROLLBACK TO tofu_gc` failed with
   `no such savepoint`.
2. An interposer on the write calls showed the failing call: `pwrite64` of
   61,504 bytes to `<TMPDIR>/etilqs_* (deleted)` returned `ENOENT`. That is
   one of SQLite's temporary files, which SQLite unlinks as soon as it opens
   them.
3. A minimal probe on the volume confirmed it. After `open`+`unlink`,
   `pwrite`, `pread` and `fsync` return `ENOENT`, while `fstat` works. This
   happens through both the preload client and kernel FUSE. POSIX requires
   that I/O on such a file succeeds.

The Tofu defect was the harness: it set the child's `TMPDIR` under the
measured root, so SQLite's temporary files landed on DolphinFS. Production
sets no `TMPDIR` (checked in `/proc/<pid>/environ` of the API host,
execution host and engine), so its temporary files are host-local. The
harness now gives the child a private host-local `TMPDIR` that it removes
after the run
(`test_child_temporary_files_stay_off_the_measured_medium`). The batch
fallback warning now names the SQLite cause instead of only the generic
`SQLite operation failed`. After the fix, the rerun below had 0 fallbacks,
0 I/O errors and 0 tracebacks in the Sidecar legs. TofuDB was unaffected in
both runs. The Sidecar itself now places SQLite's temporary files in the
host temporary root whatever `TMPDIR`/`SQLITE_TMPDIR` it inherits, and
refuses a root that cannot serve unlinked files
([STORAGE.md](STORAGE.md), `test_sqlite_temporary_files.py`).

**Medium.** `benchmarks/storage_medium_probe.py`, run twice on the scratch
directory with production's preload, against local XFS (p50/p99 ms):

| probe | DolphinFS | local XFS |
|---|---|---|
| 4 KiB append + `fdatasync` | 0.53/1.06 · 0.40/0.90 | 0.037/0.10 · 0.040/0.09 |
| 4 KiB append, no sync | 0.18/0.34 · 0.19/0.28 | 0.001 |
| 64 KiB append + `fdatasync` | 1.03/3.05 · 1.01/3.01 | 0.07/0.17 |
| publish 4 KiB block (tmp, fsync, rename, dir fsync) | 3.5/4.3 · 3.3/4.2 | 0.15-0.17 |
| file create · rename | 1.7 · 0.80 | 0.045 · 0.018 |
| cold 4 KiB random read | 0.10/0.19 · 0.11/0.53 | 0.09/0.10 |

A bare 4 KiB `fdatasync` costs 0.22-0.26 ms p50. The 72-77 ms measured on
2026-11-15 is not this volume's behavior today.

Results after the temporary-file fix, p50/p99 ms, r1 · r2:

| leg | load1 min/median/max | in-scope barriers | time in barriers | mean barrier |
|---|---|---|---|---|
| Sidecar r1 | 11.7 / 29.1 / 47.4 | 11,131 | 11.6 s | 1.04 ms |
| TofuDB r1 | 8.2 / 10.7 / 31.8 | 26,184 | 10.1 s | 0.39 ms |
| Sidecar r2 | 7.8 / 27.8 / 43.4 | 11,633 | 12.5 s | 1.07 ms |
| TofuDB r2 | 10.6 / 25.2 / 71.6 | 25,651 | 18.7 s | 0.73 ms |

| op | scale | Sidecar FULL | TofuDB (effective tier unverified) |
|---|---|---|---|
| turn.event.record | 1x | 11.4/281 · 11.3/211 | 1.46/62 · 1.60/64 |
| | 3x | 13.5/469 · 13.3/942 | 1.48/66 · 1.52/122 |
| | 5x | 28.8/1,596 · 24.0/891 | 1.58/98 · 1.59/129 |
| turn.sync.changes | 1x | 0.72/3.5 · 0.66/2.6 | 0.40/1.6 · 0.41/1.7 |
| | 5x | 0.73/13 · 0.66/6.6 | 0.40/2.7 · 0.42/3.1 |
| record.get | 1x | 0.29/0.75 · 0.29/0.71 | 0.22/0.61 · 0.23/0.64 |
| | 3x | 0.27/1.9 · 0.25/1.5 | 0.23/0.74 · 0.24/0.72 |
| | 5x | 0.30/4.6 · 0.27/2.7 | 0.25/0.93 · 0.25/0.99 |
| turn.get:metadata | 5x | 0.40/6.3 · 0.36/4.7 | 0.34/1.7 · 0.35/1.8 |
| worker_job.heartbeat | 5x | 62/807 · 38/786 | 0.70/11 · 0.71/17 |
| turn.create_pair | 1x | 164/205 · 134/140 | 21/24 · 20/23 |
| | 5x | 566/594 · 441/620 | 67/95 · 77/97 |
| worker_job.claim_next | 1x | 116/126 · 113/114 | 8.8/14 · 6.9/9.1 |
| | 3x | 218/234 · 243/251 | 21/54 · 22/36 |
| | 5x | 393/405 · 349/379 | 29/295 · 41/63 |
| worker_job.complete | 1x | 177/5,277 · 94/4,444 | 12/15 · 13/17 |
| | 3x | 4,971/5,773 · 205/10,974 | 63/73 · 129/159 |
| | 5x | 10,959/13,036 · 3,660/10,254 | 111/124 · 94/119 |
| conversation.list | 5x | 7.9/15 · 6.5/19 | 2.6/3.5 · 2.9/21 |
| phase-stop settlement herd | 1x | 5,288/5,303 · 4,582/4,600 | 44/65 · 46/58 |
| | 3x | 8,967/13,935 · 12,434/12,468 | 115/136 · 112/249 |
| | 5x | 10,325/17,600 · 10,151/16,665 | 202/254 · 290/345 |
| completed ops/s | 1x / 3x / 5x | 335/955/1,482 · 337/952/1,545 | 341/1,017/1,686 · 340/1,016/1,689 |
| events · Turns (5x) | | 6,298 · 93; 6,486 · 108 | 6,961 · 109; 6,962 · 109 |
| failed requests | | r1 5x: 15 `database_timeout` (settlement herd); otherwise 0 | 0 · 0 |
| seed (229 MB) | | 103 s · 100 s | 6.9 s · 4.8 s |

The first run, with the temporary-file defect, reported Sidecar `claim_next`
at 1x of 193/372 · 183/234 ms and 5x throughput of 1,432 · 1,428 ops/s;
TofuDB reported 8.8/12 · 7.3/29 ms and 1,689 · 1,689 ops/s. Host load
also differed: median 7–15 in the first run, versus 28–29 for Sidecar and
11 and 25 for TofuDB in the second.

These are historical observations as reported, not independently verified
raw cells. One Sidecar 5x leg reported 15 `database_timeout` failures during
settlement. No backend ranking follows: Normal can acknowledge before the
WAL sync that SQLite FULL waits for. Use the qualified November 18 1x
cohort below for the matched acknowledged-commit boundary.

**Measured sync cost against the 0.77 ms model.** The average in-scope
barrier cost 0.39-0.73 ms for TofuDB and 1.04-1.07 ms for the Sidecar under
load, so barrier latency alone brackets the model. A bare 4 KiB `fdatasync`
is cheaper than the model (0.22-0.26 ms); a Sidecar fsync also flushes the
WAL frames written before it. Sidecar barriers took 11.6-12.5 s over the
whole leg (seed plus three 90 s phases). These totals do not decompose
client latency. The model charged only barriers and omitted write calls. On DolphinFS
a 4 KiB append costs 0.18 ms even without a sync, against 0.001 ms
locally. A 60 s 1x phase per store, counted with a diagnostic interposer
on the in-scope calls:

| store | write calls | time in writes | barriers | time in barriers | reads |
|---|---|---|---|---|---|
| Sidecar | 119,497 `pwrite` (2.4 KB avg) | 25.8 s (0.22 ms each) | 1,198 `fsync` | 1.3 s | 252,810 `pread`, 2.9 s |
| TofuDB | 2,521 `write` (106 KB avg) | 2.7 s (1.07 ms each) | 2,833 | 1.2 s | 15,767, 1.5 s |

The diagnostic Sidecar leg spent 25.8 s in counted writes versus 1.3 s in
counted barriers. The 0.77 ms barrier-only model does not include that
write work. These instrumented call totals are not a decomposition of
client quantiles or a controlled comparison of backend durability costs.

Not covered: the rerun shared the volume (not the directory) with the live
production engine. Nothing under the live checkout was opened, but
production-engine latency during the legs was not monitored. Leg logs (`run.log`) contain
shutdown-time `[RouterWatchdog] ... Storage sidecar is unavailable`
tracebacks after the last phase in one TofuDB leg; they postdate every
measured phase.

Reproduce (from a checkout, with `$TOFUDB_BIN` built from it and certified;
`S` is a fresh directory on the DolphinFS volume outside any Tofu checkout;
run from a shell carrying production's `LD_PRELOAD`/`LIBBGFS_*`):

```sh
cc -O2 -shared -fPIC -o $TMPDIR/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
for b in "--backend sqlite" "--backend tofudb --engine $TOFUDB_BIN --durability strict" \
         "--backend sqlite" "--backend tofudb --engine $TOFUDB_BIN --durability strict"; do
  R=$S/leg-$(date +%s); LD_PRELOAD="$TMPDIR/faultfs.so $LD_PRELOAD" FAULTFS_ROOT=$R FAULTFS_FSYNC_US=0 \
    FAULTFS_STATS=$TMPDIR/$(basename $R).stats .venv/bin/python3 scripts/tofudb_load_test.py --root $R $b \
    --seed-scale 0.2 --scales 1,3,5 --duration 90 --run-id dfs
  cp -r $R/results $TMPDIR/$(basename $R)-results && rm -rf -- "$R"
done
```

### SQLite write calls on DolphinFS (2026-11-17)

**Problem:** the Sidecar's cost on DolphinFS is per write call (above:
119,497 `pwrite` in a 60 s 1x phase, 40% of the writer's time). Can a SQLite
setting or the commit path change that structurally?

**Method.** `benchmarks/sqlite_write_pattern.py` seeds an 80,000-event
database (400 Turns, a unique `(turn, seq)` index and an indexed Turn row)
on local disk, copies it to the medium, and replays 1,500 event-append jobs.
Each job is three event rows plus a Turn update inside a savepoint. The
writer uses the Sidecar's connection settings: WAL, `synchronous=FULL`,
64 MiB cache and `wal_autocheckpoint=4096`. `faultfs` (`FAULTFS_COUNT_IO=1`)
counts the in-scope calls and the time inside them. Each variant changes one
thing. The scratch directory was
`FMG/your-username/tofu-sqlite-writes-scratch-20261117/` on the production
volume, through production's preload client, outside every checkout; it was
removed afterwards. There were two alternating rounds at host load 7-18; the
counts were identical in both rounds and wall times within 5%.

| variant (DolphinFS, r1 · r2) | write calls / commit | time in writes | wall | commit p50/p99 ms | barriers (time) |
|---|---|---|---|---|---|
| base | 14.2 | 4.11 · 4.09 s | 5.70 · 5.72 s | 3.25/5.51 · 3.22/5.28 | 1,508 (1.0 s) |
| `page_size=65536` | 8.3 | 3.77 · 3.87 s | 5.82 · 5.95 s | 3.64/6.14 · 3.69/6.60 | 1,505 (1.4 s) |
| `mmap_size` over the whole file | 14.2 | 4.27 · 4.34 s | 5.94 · 6.11 s | 3.37/5.74 · 3.45/5.88 | 1,508 (1.0 s) |
| no checkpoint | 13.0 | 3.95 · 3.85 s | 5.44 · 5.40 s | 3.36/5.63 · 3.39/5.43 | 1,502 (1.0 s) |
| 8 jobs per COMMIT | 59.8 (7.5 per job) | 2.14 · 2.18 s | 2.78 · 2.82 s | 11.2/34.4 · 11.3/37.6 | 193 (0.2 s) |

The same runs on local XFS take 0.17 s (base), with 0.02 s in writes.

- **Two calls per WAL frame.** Without checkpoints, 9,732 frames are
  19,489 calls and 38.2 MiB, which is 2 x 9,732 + 25 calls of
  (4,096 + 24) bytes. SQLite writes each frame header and page separately.
  A checkpoint adds one call per page, which is 8% of the calls here.
- **`page_size=65536`** (SQLite's maximum; DolphinFS reports a 512 KiB
  `st_blksize`, beyond it). It needs fewer pages per commit, but a 64 KiB
  call costs more than a 4 KiB one, so p50 is 12% worse at 8.7x the bytes.
  Rejected.
- **mmap** only replaces reads. SQLite still writes through `pwrite`.
  Rejected.
- **Grouping** halves the calls and the wall time per job, because jobs
  share dirty pages. The writer already does this whenever a backlog exists
  (`_take_batch` drains it without a window). Grouping more would need a
  wait or a cap, which are tuned knobs. Rejected.

**Coalescing a commit's frames** (`--variant frames`: one commit's frames
written as SQLite does, or as one call, into a preallocated region like a
reused WAL, each followed by `fdatasync`; 300 alternating commits per run):

| frames per commit | 2 calls per frame, p50/p99 ms (r1 · r2) | one call, p50/p99 ms (r1 · r2) |
|---|---|---|
| 7 | 3.61/4.91 · 3.90/5.71 | 0.98/1.38 · 0.96/1.63 |
| 30 | 12.51/18.87 · 12.62/18.24 | 1.27/1.75 · 1.21/1.63 |

The split 7-frame case reproduces the SQLite base commit (3.2-3.3 ms p50).
One call per commit would cut it 3.7x, and the 30-frame settlement-sized
commit 10x. That is the only structural lever found. SQLite has no setting
for it, so the Sidecar now loads a VFS shim that does it
([STORAGE.md](STORAGE.md), "The Sidecar coalesces each commit's WAL
writes"; end-to-end effect in the next section).

Reproduce (`S` is a fresh directory on the volume; run from a shell carrying
production's `LD_PRELOAD`/`LIBBGFS_*`):

```sh
cc -O2 -shared -fPIC -o $TMPDIR/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
for v in base page64k mmap group8 nockpt; do
  LD_PRELOAD="$TMPDIR/faultfs.so $LD_PRELOAD" FAULTFS_ROOT=$S FAULTFS_COUNT_IO=1 \
    .venv/bin/python3 benchmarks/sqlite_write_pattern.py --root $S --variant $v
done
for f in 7 30; do
  .venv/bin/python3 benchmarks/sqlite_write_pattern.py --root $S --variant frames --frames $f --jobs 300
done
```

### The WAL coalescing VFS on DolphinFS (2026-11-17)

**Problem:** does writing each commit's WAL frames in one call
([STORAGE.md](STORAGE.md), "The Sidecar coalesces each commit's WAL
writes") close the Sidecar's write-bound gap end to end?

**Method.** Same harness, host, volume and shape as "The same comparison on
the real DolphinFS volume" above: `scripts/tofudb_load_test.py --backend
sqlite --seed-scale 0.2 --scales 1,3,5 --duration 90`, a fresh root per leg,
production's preload client, and `faultfs` with `FAULTFS_FSYNC_US=0
FAULTFS_COUNT_IO=1`, which counts calls but injects nothing. The legs ran
ABAB: stock r1, coalescing r1, stock r2, coalescing r2. Stock is the parent
revision (`600c5bb559`) exported to a host-local directory; coalescing is the
revision that adds the shim. Nothing else differs. The scratch directory
`FMG/your-username/tofu-sqlite-coalesce-scratch-20261117/` sat outside every
checkout and was removed. Host load (1-minute, median) was 4.5 · 7.2 for
the stock legs and 6.2 · 10.1 for the coalescing legs, so the coalescing
legs ran on the busier host. No TofuDB legs ran (no engine on production's
volume); the TofuDB column repeats the earlier same-host, same-volume
results with unverified effective durability. It is historical context, not a
paired comparison with coalescing. All four Sidecar legs had 0 failed requests.

p50/p99 ms, r1 · r2:

| op | scale | Sidecar stock | Sidecar coalescing | TofuDB (effective tier unverified, above) |
|---|---|---|---|---|
| turn.event.record | 1x | 11.3/223 · 11.1/199 | 2.82/149 · 2.82/156 | 1.46/62 · 1.60/64 |
| | 3x | 12.5/730 · 12.3/771 | 3.00/329 · 3.07/250 | 1.48/66 · 1.52/122 |
| | 5x | 22.5/1,003 · 25.7/878 | 3.51/528 · 4.02/597 | 1.58/98 · 1.59/129 |
| worker_job.complete | 1x | 1,688/2,667 · 92/4,480 | 24/1,863 · 35/1,915 | 12/15 · 13/17 |
| | 3x | 234/5,285 · 192/5,779 | 82/2,297 · 1,978/1,997 | 63/73 · 129/159 |
| | 5x | 4,168/14,035 · 3,223/10,004 | 2,468/5,462 · 1,739/9,090 | 111/124 · 94/119 |
| phase-stop settlement herd | 1x | 1,176/2,795 · 4,532/4,539 | 816/1,930 · 1,951/1,971 | 44/65 · 46/58 |
| | 3x | 11,129/11,158 · 11,627/11,655 | 2,518/4,795 · 2,481/4,390 | 115/136 · 112/249 |
| | 5x | 14,844/18,861 · 10,395/18,150 | 4,652/8,082 · 9,269/11,010 | 202/254 · 290/345 |
| worker_job.claim_next | 1x | 106/110 · 118/125 | 28/35 · 23/29 | 8.8/14 · 6.9/9.1 |
| | 5x | 420/434 · 391/402 | 121/136 · 138/173 | 29/295 · 41/63 |
| worker_job.heartbeat | 5x | 47/945 · 59/7,892 | 2.4/162 · 3.4/312 | 0.70/11 · 0.71/17 |
| turn.create_pair | 1x | 106/155 · 154/165 | 444/471 · 517/539 | 21/24 · 20/23 |
| | 5x | 569/742 · 326/529 | 133/208 · 186/236 | 67/95 · 77/97 |
| record.get | 5x | 0.28/3.3 · 0.29/3.4 | 0.28/2.9 · 0.32/4.5 | 0.25/0.93 · 0.25/0.99 |
| completed ops/s | 1x / 3x / 5x | 334/973/1,509 · 332/967/1,537 | 335/995/1,619 · 336/993/1,595 | 341/1,017/1,686 · 340/1,016/1,689 |
| events · Turns (5x) | | 6,402 · 108; 6,365 · 108 | 6,789 · 108; 6,756 · 108 | 6,961 · 109; 6,962 · 109 |
| seed (229 MB) | | 95 s · 98 s | 36 s · 38 s | 6.9 s · 4.8 s |
| in-scope write calls (whole leg) | | 1,303,443 · 1,306,057 | 477,607 · 485,081 | |
| time in write calls | | 257 s · 263 s | 104 s · 110 s | |

The settlement herd is `turn.event.record:terminal:phase_stop`, the same
row as "phase-stop settlement herd" above.

Verdict:

- **Coalescing is a large, structural win for the Sidecar on DolphinFS.**
  Streamed events are 4-6x faster at p50 (2.8-4.0 against 11-26 ms) with
  paired p99 reductions of 21.6–67.6% (1x r2: 199 → 156 ms). `claim_next` is 2.8-5x faster, heartbeats at 5x
  14-25x, and the 3x/5x settlement herd 1.1-4.7x. Seeding is 2.6x faster. Write calls fall
  2.7x and the time inside them 2.4x. What remains is mostly checkpoint
  writes, which copy scattered pages into the database file one call each,
  and frame writes split by reads that spill back. The shim leaves both to
  stock SQLite on purpose.
- **Throughput:** +2-3% at 3x and +4-7% at 5x, with about 6% more events at
  5x, comparing the two Sidecar builds only.
- **`worker_job.complete` varied by round.** p50 improved at 1x and 5x but was
  1,978 ms in coalescing r2 at 3x (stock 192-234 ms).
- **`turn.create_pair` at 1x regressed**, 444-517 against 106-154 ms p50
  in both rounds.
Diagnostic legs observed inline checkpoints holding the writer. This is a
possible mechanism, not an attribution for the tabulated legs. That would
require same-leg, timestamp-joined evidence separating checkpoints from
unrelated host contention. The November 17 raw cells remain unverified.

The TofuDB column is retained only as historical context. It came from a
separate campaign with unverified durability and does not establish a backend
ranking or the cause of a cross-backend gap.

Not covered: the legs shared the volume (not the directory) with the live
production engine. One production `conversation.search` query was already
retrying `database_unavailable` before the first leg started (15:23, first
leg 15:26) and kept retrying through the legs. No other storage error
appeared in production's `app.log` during the window.

Reproduce (from a checkout; `S` is a fresh directory on the volume, `A` a
host-local export of the parent revision with the checkout's `.venv`
linked; run from a shell carrying production's `LD_PRELOAD`/`LIBBGFS_*`):

```sh
cc -O2 -shared -fPIC -o $TMPDIR/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
for C in $A $PWD $A $PWD; do
  R=$S/leg-$(date +%s); (cd $C && LD_PRELOAD="$TMPDIR/faultfs.so $LD_PRELOAD" FAULTFS_ROOT=$R \
    FAULTFS_FSYNC_US=0 FAULTFS_COUNT_IO=1 FAULTFS_STATS=$TMPDIR/$(basename $R).stats PYTHONPATH=$C \
    .venv/bin/python3 scripts/tofudb_load_test.py --root $R --backend sqlite --seed-scale 0.2 \
    --scales 1,3,5 --duration 90 --run-id dfs)
  cp -r $R/results $TMPDIR/$(basename $R)-results && rm -rf -- "$R"
done
```

### Checkpoint page runs: the two regressions (2026-11-17)

**Problem:** why did coalescing regress `turn.create_pair` at 1x and leave
`worker_job.complete` bimodal?

**Diagnosis.** Three diagnostic legs (seed 0.2, scales 1/3, 90 s, same
volume) logged every writer COMMIT (duration, batch, faultfs call deltas)
and every in-scope `pread`/`pwrite`/`fsync`/`fcntl` lock/`mmap` with its
duration. Findings:

- **No checkpoint runs in the background.** With the fast path off the
  writer sets `wal_autocheckpoint=4096`, so SQLite checkpoints inside the
  COMMIT that crosses 4,096 frames, on the writer thread. Nothing is "still
  checkpointing" after the seed; the stated hypothesis was wrong in that
  form.
- **The seed leaves checkpoint debt to whichever commit crosses next.** The
  seed's frame stream is identical in both arms, but the Turn search
  projection's `outbox_ack` commits interleave with it asynchronously, so
  the crossing commit moves. In one coalescing leg and one stock leg it was
  a seed `record.put` 0.4-0.5 s before the phase (create_pair p50 47 and
  142 ms). In another coalescing leg it was the first `turn.create_pair`
  batch: that COMMIT took 340 ms with 1,817 database-page writes and three
  syncs, and all 21 create_pairs waited (p50 378 ms, p99 406). Faster seed
  commits made the coalescing arm leave the crossing to the burst more
  often. The stock arm does it too: stock r1 below shows 516 ms.
- **The checkpoint's cost is its page writes.** It copies one page per
  `pwrite` at ~0.2 ms each. Its WAL reads cost ~3 µs each, its syncs
  under 30 ms, and locks and mmap took no measurable time. A 3,000-page
  checkpoint held the writer 0.6-0.9 s; one after the 3x herd copied 12,293
  pages and held it 6.2 s (2.7 s of `pwrite`).
- **`complete` is bimodal for the same reason.** Completions queue behind
  the phase-stop herd. When the herd's batch crosses the threshold, every
  completion behind it waits for the checkpoint (p90 7.6 s against p50
  90 ms in one leg).
- Ruled out: checkpoint starvation (every large checkpoint backfilled
  thousands of frames), lock waits (`fcntl` on the -shm took ~0 ms), shm
  growth (16 one-byte writes per new region, ~1 ms) and writer-queue
  fairness (the stalled jobs were running or queued behind a single COMMIT,
  not reordered).
- Not explained: in the first diagnostic pair, large COMMITs of both arms
  spent 1.2-3.3 s between the commit's WAL sync and the checkpoint's with no
  in-scope I/O logged. The later legs, which also timed locks and mmap, did
  not show such a gap.

**Fix.** The shim joins a checkpoint's adjacent page writes into one call
([STORAGE.md](STORAGE.md), "The shim also writes each checkpoint run of
adjacent pages in one call"). In the diagnostic legs the slowest COMMIT
fell from 2.4-6.2 s to 0.86 s, and the first-burst checkpoint from 1,817
write calls (340 ms) to 502 (133 ms).

**ABAB.** Same harness and shape as the section above but 60 s per scale:
stock (`50c81c53da`) r1, fixed (`fc985a1b2c`) r1, stock r2, fixed r2, in
`FMG/your-username/tofu-sqlite-regress-scratch-20261117/` (removed). Host
load (1 min) was 6.9-16.4. All legs had 0 failed requests, and production's
`app.log` showed no `authority is unavailable` retry before or during any
leg (the 15:23 `conversation.search` retry last logged at 15:42:55).

p50/p99 ms, stock r1 · fixed r1 · stock r2 · fixed r2:

| op | scale | stock r1 | fixed r1 | stock r2 | fixed r2 |
|---|---|---|---|---|---|
| turn.create_pair | 1x | 516/521 | 39/82 | 111/157 | 40/84 |
| | 3x | 330/350 | 64/158 | 170/430 | 317/343 |
| | 5x | 518/672 | 156/216 | 540/709 | 178/213 |
| worker_job.complete | 1x | 75/3,635 | 403/435 | 161/3,897 | 27/106 |
| | 3x | 297/328 | 125/677 | 1,150/1,174 | 134/1,152 |
| | 5x | 1,782/6,463 | 188/1,979 | 3,325/4,939 | 1,234/1,799 |
| phase-stop settlement herd | 1x | 3,754/3,770 | 143/537 | 3,975/3,988 | 169/175 |
| | 3x | 7,619/7,643 | 1,325/1,355 | 6,260/7,227 | 1,501/1,522 |
| | 5x | 5,815/12,292 | 2,405/2,526 | 6,202/11,125 | 1,592/2,611 |
| turn.event.record | 1x | 11.4/211 | 2.9/155 | 11.4/164 | 2.9/143 |
| | 3x | 13.6/575 | 2.9/183 | 13.7/487 | 2.9/141 |
| | 5x | 24.2/1,273 | 3.2/419 | 23.1/1,560 | 3.1/432 |
| worker_job.claim_next | 5x | 386/518 | 129/138 | 388/1,317 | 145/158 |
| worker_job.heartbeat | 5x | 40/1,364 | 2.3/182 | 39/358 | 1.9/80 |
| record.get | 5x | 0.3/4 | 0.3/4 | 0.3/4 | 0.3/5 |
| completed ops/s | 1x / 3x / 5x | 334/981/1,496 | 341/1,003/1,652 | 335/973/1,512 | 341/1,003/1,642 |
| events (5x) | | 4,206 | 4,567 | 4,151 | 4,588 |
| seed (229 MB) | | 97 s | 17 s | 99 s | 15 s |
| in-scope write calls (leg) | | 1,068,275 | 261,805 | 1,064,986 | 251,983 |

Verdict:

- **`turn.create_pair` at 1x is fixed:** 39-40 ms against stock's 111-516.
  At 3x and 5x the fixed arm is at or below stock in three of four cells;
  fixed r2 at 3x (317 ms) is inside stock's 170-330 range.
- **`worker_job.complete` still varied.** The largest reported fixed p50
  was 1,234 ms, against 3,325 ms for stock and 1,978 ms in the separate
  prior coalescing campaign. Retained quantiles do not establish modality
  or attribute the spread to checkpoints.
- **The earlier gains hold and grow:** events 4-8x faster at p50 with p99
  12.8–72.3% lower across paired cells (1x r2: 164 → 143 ms), the settlement herd 2.4-26x faster, `claim_next` 2.7-3x,
  heartbeats 17-21x at 5x, seeding 6x, throughput +2-3% at 3x and +8-10%
  at 5x, and write calls 4.1x fewer than stock. Reads are unchanged.
Inline checkpoints can hold SQLite's single writer, as observed in the
diagnostics. The displayed cells alone do not establish the cause of the
remaining stalls.

Reproduce: as in the section above, with `--duration 60`; the diagnostic
commit log and call log were throwaway patches and are not in the tree.

### Checkpoints beside the writer (2026-11-17)

**Problem:** `worker_job.complete` still waited behind the inline
checkpoint a phase-stop herd's commit triggered (1x fixed r1 above: 403 ms
against stock 75-161).

**Design and what the diagnostic legs showed** (seed 0.2, 60 s, per-commit
log with monotonic times, per-thread `pread`/`pwrite`/sync/lock/mmap log,
`py-spy dump --native` of the writer; decision in
[STORAGE.md](STORAGE.md), "Checkpoints run beside the writer"):

- A writer-thread checkpoint taken when the queue is empty cannot help:
  completions start about 1 ms after their settlement commit is
  acknowledged (+0.473 s commit, completions from +0.474 s in the earlier
  leg), so the writer is idle exactly when they arrive.
- First version, a `PASSIVE` checkpointer on its own connection copying one
  page per call: the WAL never restarted during the seed and reached
  168,751 frames. The writer then blocked in `walIndexAppend` (a page
  fault on the -shm mapping) each time it entered a new 32 KiB wal-index
  region: commits that crossed a region boundary took 1.08-1.18 s, all
  others p50 1.4 ms, and `turn.create_pair` at 1x took 1.16-1.27 s in
  both ABAB rounds. This is the 2026-11-15 rejection's failure.
- With the shim coalescing every checkpoint (concurrent passes 12x
  faster) the WAL still grew to 168,751 frames: SQLite restarts it only at
  a write transaction that begins fully backfilled, and a back-to-back
  writer always begins while the checkpointer copies its previous commit.
- With the writer finishing a due checkpoint's remainder (< 4,096 frames)
  between batches, the WAL peaked at 20,490-40,962 frames (single herd
  batches) and restarted throughout; `create_pair` at 1x fell to 40 ms.
- What remains: the concurrent checkpoint's writes and the writer's
  database reads contend in the DolphinFS client. In one 1x leg the
  completion batch held the writer 404 ms (193 ms of COMMIT) while a
  588 ms, 4,407-frame pass ran, and single 128 KiB checkpoint writes took
  up to 0.66 s at host load ~150. Writer-side remainders took up to
  0.53 s.

**ABAB** (stock `50c81c53da` against this change), 1x/3x/5x, 60 s, in
`FMG/your-username/tofu-sqlite-ckpt-scratch-20261117/` (removed). Rounds
alternated order: a1 stock-new, a2 new-stock, a3 stock-new, a4
new-stock. Host load (1 min) moved between 5 and 211 during legs, far above
the 7-16 of the section above, so single cells are noisy. All legs had 0
failed requests except stock a2 at 3x (18 `database_timeout`). p50 ms:

| op | scale | stock a1 / a2 / a3 / a4 | new a1 / a2 / a3 / a4 |
|---|---|---|---|
| turn.create_pair | 1x | 324 / 469 / 642 / 171 | 31 / 41 / 37 / 41 |
| | 3x | 162 / 1,326 / 191 / 389 | 55 / 61 / 396 / 384 |
| | 5x | 529 / 3,172 / 302 / 442 | 227 / 154 / 446 / 250 |
| worker_job.complete | 1x | 94 / 1,287 / 93 / 117 | 49 / 35 / 142 / 434 |
| | 3x | 1,199 / 3,984 / 1,324 / 246 | 465 / 242 / 139 / 673 |
| | 5x | 1,766 / 3,172 / 4,238 / 5,235 | 1,083 / 597 / 189 / 664 |
| phase-stop settlement herd | 1x | 2,185 / 7,916 / 2,056 / 1,130 | 290 / 153 / 338 / 184 |
| | 3x | 6,527 / 19,695 / 6,333 / 7,702 | 1,018 / 501 / 959 / 1,675 |
| | 5x | 8,165 / 7,062 / 7,855 / 6,143 | 1,828 / 923 / 2,165 / 2,592 |
| turn.event.record | 1x | 15 / 66 / 11 / 11 | 2.8 / 20 / 2.9 / 4.1 |
| | 3x | 14 / 689 / 13 / 13 | 3.0 / 2.9 / 3.1 / 87 |
| | 5x | 27 / 1,697 / 25 / 23 | 281 / 3.1 / 3.3 / 191 |
| worker_job.claim_next | 5x | 467 / 4,281 / 373 / 386 | 131 / 134 / 226 / 815 |
| worker_job.heartbeat | 5x | 39 / 1,349 / 47 / 48 | 69 / 2.1 / 2.0 / 156 |

Verdict:

- **`turn.create_pair` at 1x is 31-41 ms in every round** against stock's
  171-642.
- **The settlement herd is 2.4-52x faster in every cell, and
  `worker_job.complete` at 5x is 1.6-22x faster in every round.**
- **`worker_job.complete` at 1x and 3x is not yet a clean win:** it beat
  the paired stock leg in five of eight cells, but a3 1x (142 against 93),
  a4 1x (434 against 117) and a4 3x (673 against 246) are worse. Its
  spread (35-673 ms) is smaller than stock's (93-3,984) but it is still
  not flat. Diagnostic legs observed checkpoint copies and writer-side
  remainders; they do not establish causes for these tabulated cells.
- Streamed-event p50 improved in nine of twelve paired cells, by
  2.68–547.42×. Excluding the entire stock a2 block leaves six of nine
  improvements at 2.68–7.58×. The three slower new cells (a1 5x, a4 3x/5x)
  coincided with high host load. Checkpoint activity and unrelated host
  contention can both delay the writer; distinguishing them requires
  same-leg, timestamp-joined evidence.
- The comparison with the previous coalescing build (`dd9df40406`) was
  not run: host load stayed at 230-310 afterwards and production showed
  storage retries from 20:01.
- Production `app.log`: no `authority is unavailable` retry recurred.
  Storage retries appeared at 18:57:09 (`event.append_batch`, during stock
  a2), 19:22:25 (after new a4) and 19:33:28-36 (during a diagnostic leg),
  and from 20:01 with no run of this change active, all at host load
  150-300. The pre-leg log gate skipped two legs at 18:59.

Reproduce: the ABAB legs as above with `--duration 60 --scales 1,3,5`.

### Low-load (1x) completion and settlement, 600 s legs (2026-11-18)

**Problem:** the earlier 1x `worker_job.complete` legs were short and ran
on a busy host. Terminal recording at phase stop is a separate outcome from
natural settlement; completion itself was not split in the pinned harness.

**Method.** `scripts/tofudb_load_test.py --scales 1 --duration 600
--seed-scale 0.2 --request-profile`, a fresh root per leg in
`FMG/your-username/tofu-1x-scratch-20261118/` on the production volume
through production's preload client (removed afterwards; no live root or
process was touched). Arms: the tuned Sidecar (WAL, `synchronous=FULL`,
coalescing VFS, checkpoints beside the writer, host-local temporary files)
and TofuDB `--durability strict`. These match at the acknowledged-commit
boundary: SQLite FULL syncs the WAL before committing a transaction (a
writer batch can share one transaction); TofuDB Strict waits for WAL sync
covering the acknowledged commit (physical syncs may cover multiple commits).
The arms need not issue the same number of syncs or use the same physical
transaction grouping. Sidecar timings include its production `storage.v1`
keep-alive RPC; native timings include its daemon client/transport. The
client clock spans the storage call through its returned result; terminal
`record_task_event` also includes caller-side trace/projection work. It is
not a provider-to-browser or whole-Agent measure. Both ran from one
`git archive 32892b97b3` export (the commit of the certified engine
`44455223d8c6`, binary `ef62515fe1c4`) plus the launcher fix below.
Alternating legs (ABBA after a round boundary), one driver
(`output/tofudb-1x-completion-20261118/campaign.py`). Validity rule, fixed
before the first leg: exit 0, 1-minute `/proc/loadavg` sampled every 5 s
never above 64 (about a third of the 180 CPUs), and, from 16:47, no other
storage load test on the host. A leg started only after load1 < 30 for
60 s. The numbered campaign ran from 09:03 to 18:12; six legs qualify for
the Strict/FULL comparison, plus one supplementary Normal leg:

| excluded | reason |
|---|---|
| 01, 04-08, 10-12, 14-16, 22 | load1 peak 104-223 (bursts of 170-220 runnable `python` threads from other agents' test fan-outs, ~15 s each; this campaign's own legs held 0-3 R/D threads) |
| 02 | engine admission refused: a server restart re-certified `$TOFUDB_BIN` for newer source mid-campaign (no measurement) |
| 19, 20 | excluded for overlap with another worker's `tofudb_load_test` on the same volume (`tofu-bench-dfs/`); 20 also overlapped this campaign's local-disk caller probe |
| 23 | both foreign storage load-test overlap and load1 peak 64.36 > 64 |
| aborted 12-normal | killed when the driver was replaced (no measurement) |
| 03, 08, 10 | supplementary `--durability normal` arm (production's automatic tier on DolphinFS); dropped from the rotation at 13:15, only 03 valid |

For leg 20, use the ledger start of 16:36:32; the exclusion note says
16:35:35, which is not corroborated. The foreign sampler records overlap
from 16:37:27, after either start. The qualifying comparison order was
native 09, SQLite 13/17/18, native 21/24: not balanced ABBA after exclusions.
Validity is not evidence of an interference-free leg (see leg 09 below).

Two server restarts happened during the campaign (not caused by it); no
legs were in flight. The engine split is TofuDB's
`system.metrics.requestProfile` (dispatch per operation, phase-edge
deltas). The Sidecar has no per-operation server profile; its writer
COMMIT latency is cumulative from the seed. The retained native phase metrics
independently report `walDurability: strict` for legs 09, 21 and 24. The
harness's SQLite `wal_durability` field is a configured label, not a live
PRAGMA read; verify WAL/FULL against the pinned writer setup, not that field
alone. Each qualifying leg seeded the same 1,404 Turns (228,969,071 bytes)
and used the same workload profile; timed execution and counts can differ.

p50/p90 ms per qualifying leg, load1 before/median/peak/after. This is an
instrumented configuration comparison: `--request-profile` sets
`TOFU_DB_REQUEST_PROFILE_EVERY=1` only for native; the SQLite branch returns
before that setting. Native profiling adds observer overhead; a profile-off
claim would need a small matched control. The ops/s denominator is elapsed
wall time from before thread start until after all join attempts, including
phase-stop drain, not a fixed 600 s.

| leg | arm | load1 | mixed `worker_job.complete` | natural `turn.event.record:terminal` (n) | phase-stop herd p50 | ops/s |
|---|---|---|---|---|---|---|
| 09 | TofuDB strict | 5.7 / 6.7 / 17.5 / 7.0 | 1.99 / 8.28 | 26.4 / 60.0 (53) | 54 | 340 |
| 13 | Sidecar | 15.8 / 15.3 / 37.3 / 20.4 | 3.90 / 60.4 | 18.3 / 63.6 (52) | 471 | 331 |
| 17 | Sidecar | 19.9 / 17.7 / 28.8 / 20.4 | 4.48 / 86.7 | 19.4 / 66.4 (53) | 222 | 337 |
| 18 | Sidecar | 21.9 / 27.3 / 44.5 / 49.6 | 5.39 / 583 | 21.6 / 157 (51) | 669 | 336 |
| 21 | TofuDB strict | 21.1 / 19.9 / 34.2 / 19.9 | 2.97 / 18.9 | 31.2 / 966 (51) | 56 | 336 |
| 24 | TofuDB strict | 26.5 / 27.4 / 34.3 / 26.4 | 1.99 / 12.5 | 27.0 / 43.9 (52) | 56 | 332 |
| 03 | TofuDB normal (suppl.) | 14.7 / 11.8 / 18.5 / 15.2 | 1.22 / 10.9 | 26.6 / 52.5 (51) | 54 | 338 |

Per arm, min-max over the three valid legs:

| metric | Sidecar | TofuDB strict |
|---|---|---|
| mixed `worker_job.complete` p50 | 3.90-5.39 | 1.99-2.97 |
| mixed `worker_job.complete` p90 | 60-583 | 8.3-18.9 |
| natural settlement p50 | 18.3-21.6 | 26.4-31.2 |
| natural settlement p90 | 64-157 | 44-966 |
| phase-stop herd p50 | 222-669 | 54-56 |
| `turn.create_pair` p50 | 4.0-5.1 | 3.5-4.9 |
| completed ops/s | 331-337 | 332-340 |

**Observed in this cohort:** the retained mixed completion and phase-stop
herd cells are lower for TofuDB, and the retained natural-settlement p50
cells are lower for SQLite. This is descriptive, not a backend-wide verdict.
The pinned harness splits terminal recording but not completion: each leg
contains 21 phase-stop settlements and 72–74 completions in total. Natural-only
completion quantiles cannot be recovered; synchronized-stop writer contention
is a competing explanation for tails. Tag completion at the same natural/stop
boundary and compare matched blocks before drawing a natural-completion conclusion.
Regression status is unknown: the November 9 comparison lacks raw data and a
durability tier. The retained November 18 quantiles are reported summaries,
not the original samples.

**Pooled profile, not per-settlement attribution.** Phase-edge request-profile
deltas give the following means: subtract the before snapshot from the
after snapshot in exclusive stage nanoseconds, then divide by the operation
request-count delta and by 10⁶. Completion's server denominators are
73 / 69 / 71, not its client sample counts of 74 / 72 / 73. Native
`event.list` includes 74/72/73 requests versus 53/51/52 natural terminals.

| Native leg | completion dispatch | `wal_barrier_wait` | `event.list` `vfs_read` | `event.list` `vfs_open` |
|---|---:|---:|---:|---:|
| 09 | 1.746 | 1.278 | 32.140 | 7.140 |
| 21 | 38.058 | 37.521 | 20.201 | 602.921 |
| 24 | 1.538 | 1.128 | 12.283 | 1.410 |

The completion means do not decompose client p50. SQLite's per-leg writer
COMMIT maxima (13/17/18: 15,090.682 / 440.879 / 1,229.150 ms) include seed
and are not joined to completion calls; they do not explain its p90 by
themselves. Both backends' settlement caller can issue `event.list` while
folding the trace. Native's roughly 153–158 `vfs_read` instrumentation calls
per `event.list` request are not verified physical DolphinFS reads. In leg
21, mean open time substantially exceeded read time. Neither these aggregates
nor concurrent packing establish that relocated payloads caused the gap.
A causal comparison needs per-settlement caller spans joined to server
operations, separating queue, open, routing, payload reads and projection.

**Foreground read scope.** `indexed_stream::list`
selects positions then reads their stream segments; `stream::read_persisted_positions`
shares a read for positions in the same segment but reads each distinct
verified segment separately. Selective projection avoids omitted body
materialization, not the segment read that verifies its type and timestamp.
The existing [history routing slices](TOFUDB_BLOCK_READ_PATH.md#history-routing-slices-2026-11-13)
reduce routing bytes per lookup, not these payload reads. The GC traversal
batch shares routing work, but is not a foreground range reader. Packed
blocks are ordered by content ID, not event sequence. A deterministic VFS
fixture commits four one-event stream blocks and packs them with one
unrelated block. It takes two two-block selections from the actual pack
offsets: one same-source contiguous run and two same-source gapped runs.
The fixture drives the same persisted-position reader as `event.list`, with
entity directory warmed, the block/index caches disabled, and segment
read handles cleared. Its verified point-read assertions are one open,
two index-entry reads and two payload reads in either case. The adjacent
payload-only probe (resolved offsets supplied as setup) checks two separate
reads versus one combined read, with one open either way. Splitting the
combined bytes recovers each verified block and its exact content ID; a
changed byte fails the ID check. The retired pack's stale range returns
not-found, and the ordinary reader resolves the replacement and preserves
sequence and event bytes. The confirmed run recorded one open and zero
reads at the stale locator, then one open and eight reads on the verified
replacement path. These are VFS counts in a constructed fixture,
not physical DolphinFS counts, a frequency estimate for adjacent ranges,
or latency evidence. No production pack batching change was made.

**Filtered pages: decode only selected index values.** The exact/prefix
reader still uses bounded scans per type, but merges using sequence numbers
in the ordered keys before decoding values. Only selected values are
validated against their key; physical stream integrity and projection
checks still run. This avoids up to `T × L` typed value decodes for `T`
dense types and a page of size `L`, without changing the scan count or
adding a tuning parameter. In the skewed limit-2 fixture, two scans return
four rows but only two values decode. Malformed B/11 does not fail the
first page; explicitly paging to it fails, as does a selected key/value
sequence mismatch. Exact/prefix, cursor and selective payload projection
are checked against scratch SQLite and native. A one-row-at-a-time merge
was not needed; it would trade away the existing range-scan efficiency.
This does not explain the historical 1x settlement result: the trace caller
uses `payload_types` without a type filter.

Focused verification (disposable fixtures; no deployment or benchmark):

```sh
python3 scripts/remote_cargo.py -C packages/tofu-db test --locked --offline --lib -- \
  indexed_stream:: selected_stream_blocks_have_counted_physical_adjacency_and_retirement_fallback event_list_ --nocapture
python3 scripts/remote_cargo.py -C packages/tofu-db \
  --after-build debug/tofu-db scripts/tofudb_filtered_page_oracle.sh \
  build --locked --offline --bin tofu-db
```

The after-build check exercises identical native-daemon and file-backed
SQLite Sidecar requests for ordering, continuation, literal prefix matches,
and selective projection. Corruption is injected separately at each backend's
own decode seam; the SQLite fault concerns JSON and the native fault concerns
an index value, not a claim that their on-disk representations are identical.

**Launcher defect found and fixed.** `native_launch._durability_args`
turned `TOFU_DB_DURABILITY=strict` into no flag, so the engine's automatic
tier served `normal` on DolphinFS: a `--durability strict` smoke leg here
reported `walDurability: normal`. An explicit value now reaches the daemon
(`test_native_launch_durability_unset_keeps_engine_tier_and_strict_is_explicit`).
Production sets no `TOFU_DB_DURABILITY` and is unaffected. The November 17
real-DolphinFS columns therefore do not claim verified Strict; the older
reported values are retained unchanged. The local-disk comparison of record
probes `strict` and is unaffected.

Not covered: production latency was not monitored during the legs. Its
`app.log` shows storage retries (`database_unavailable`, waits up to 21 s)
in bursts at 12:12-12:13, 12:35-12:36 and 13:57-13:58, 40-170 s into
TofuDB legs 07, 09 and 15 (seed and phase start), and also at 09:32,
10:23, 12:23, 13:11 and 14:20 with no leg running; none during TofuDB legs
03, 05, 12, 19, 21 and 24 or after 14:30. The legs may have contributed
on a loaded host; that is not established.

Raw evidence: `output/tofudb-1x-completion-20261118/` has 23 phase reports
(`<leg>/report.json`), with phase metrics and per-leg load/run logs where
recorded. Admission failure 02 and `aborted-12-tofudb-normal` have no phase
report. Keep `legs.jsonl`, `exclusions.json`, `rd_samples.log`, and
`foreign_samples.log` for validity and interference checks. Git delivery
`108f60ac9bdf218f605b6076dc4bc8cefbd2287c` binds assignment
`bec9bb18-27ff-514d-b310-19d484fef23a` to checkpoint
`6cf72f55ad6ddb7e3b03010a151588e8fa8a61f4` and records this evidence location.

**Replay the retained analysis, without launching storage.** From a checkout
with the registered shared `output:` root mounted, set `E` to its
`tofudb-1x-completion-20261118` directory (not a private worktree's `output/`
if that is not the registered root). Copy only the analyzer and its
inputs to fresh host-local scratch. The analyzer writes both summaries in
the copy; the original evidence stays read-only:

```sh
E="${SHARED_OUTPUT_ROOT:?set to the registered output root}/tofudb-1x-completion-20261118"
R=$(mktemp -d /tmp/tofudb-1x-replay.XXXXXXXX)
(
  set -eu
  cp "$E/analyze.py" "$E/legs.jsonl" "$E/exclusions.json" "$R/"
  for report in "$E"/*/report.json; do
    leg=$(basename "$(dirname "$report")")
    mkdir "$R/$leg"
    cp "$report" "$R/$leg/"
    metrics="$E/$leg/metrics-1x.json"
    if [ -f "$metrics" ]; then cp "$metrics" "$R/$leg/"; fi
  done
  python3 "$R/analyze.py" > "$R/replay.log"
)
```

Read `$R/summary.md` and `$R/summary.json`. The analyzer's rate sums all
operation rows, including `:error` rows when present; the six qualifying
Strict/FULL legs had none and zero stragglers. In ledger order their counts
were 204,296 / 198,538 / 202,430 / 201,953 / 201,337 / 199,320.

The analyzer trusts the ledger and manual exclusions, transcribes stored
quantiles, and repeats the stale leg 20 exclusion text. It does not verify
validity, reconstruct percentile ranks, or calculate the stage table. Run
`python3 scripts/check_tofudb_1x_evidence.py "$E"` for a read-only check
of the retained cohort, counts and stage table against the source JSON.

**Historical launch inputs, not a replay command.** The RPC deadline is
20 s per attempt. Each thread gets a separate 120 s join timeout; that is
not a global drain bound. Base checkout/export:
`32892b97b3063ec731535c04d1bf5e34a441bc43` with the Strict passthrough fix
recorded in `91c13e72c14d73a80d9f86d622301984f5af73a5`. The retained
`tofu-db.pinned` SHA-256 is
`ef62515fe1c486ca8cc36511ace8254a2ff5b58f1eb048d1d436a16c24afbeff`.
Do not invoke retained `campaign.py` to replay: it uses the existing
ledger, binary and fixed volume root and can launch more legs. A fresh
DolphinFS campaign would require separate approval, a new driver/output and
scratch root, and a current interference check.

## TofuDB hot paths on the production DolphinFS client (2026-11-15)

**Problem:** decide where TofuDB loses time on the production-shaped load
now that the harness keeps the userspace DolphinFS client (H1 above).

**Profile.** `scripts/tofudb_load_test.py --scales 1,5 --duration 150
--request-profile`, WAL `normal`, real DolphinFS, engine source = shared
head `4776702ffc` (binary `047be923`). Engine dispatch time by exclusive
stage, the one leg whose host load stayed below the CPU count (A2, max
load 71.7 on 77 CPUs):

| stage | 1x (88.3 s total) | 5x (208.0 s total) |
|---|---|---|
| `wal_barrier_wait` | 69.3 s (78.5%) | 162.4 s (78.1%) |
| `fifo_wait` | 11.0 s (12.4%) | 12.6 s (6.1%) |
| `ir_steps` | 1.0 s (1.1%) | 4.9 s (2.4%) |
| `vfs_read` + `vfs_open` | 1.5 s (1.7%) | 2.9 s (1.4%) |

`turn.event.record` and `worker_job.heartbeat` own nearly all of the
barrier wait. A2 is not typical of the volume, though: it is the leg with
multi-second WAL write stalls. All three base legs, valid or not:

| base leg | commits per physical write | WAL wait share of engine time | `turn.event.record` p50/p99 ms | `worker_job.heartbeat` p50/p99 ms | completed ops/s |
|---|---|---|---|---|---|
| A1 1x / 5x | 1.02 / 1.04 | 33.5% / 26.4% | 1.71/112 · 1.69/94 | 0.72/2.3 · 0.80/6.9 | 341 / 1,689 |
| A2 1x / 5x (valid) | 1.08 / 1.15 | 78.5% / 78.1% | 1.75/1,981 · 1.87/563 | 0.78/40 · 1.02/399 | 333 / 1,579 |
| A3 1x / 5x | 1.01 / 1.04 | 29.8% / 25.5% | 1.68/49 · 1.66/97 | 0.69/2.2 · 0.78/3.4 | 342 / 1,690 |

The WAL writer is busy 3-16% of wall time (26-82 physical writes/s at
0.85-5.0 ms each, 50-60 KB per frame). Point reads stay at 0.03-0.11 ms
engine mean.

The earlier profile of this session ran on kernel FUSE (the H1 defect):
there `event.list` (Request Inspector history pages) cost 98-115 ms per call,
about 110-130 serial cold reads at ~0.6 ms each. On the userspace client the
same call costs 9-19 ms (88-117 reads at ~0.06-0.11 ms). Kernel-FUSE read
costs are not production evidence.

**Rejected: overlapping a batch's cold block reads.** Candidate
`BlockStore::get_shared_many` read the uncached segments of
`stream::read_persisted_positions` on one scoped thread per block (verified,
ordered, first-error semantics), plus single-flight file opens for history
sources and payload segments. Unit specifications passed and the full lib
suite matched base. Alternating base/candidate legs (A1 B1 A2 B2 A3 B3, each
1x+5x, fresh seed) on the production client:

| `event.list` engine mean | base | candidate |
|---|---|---|
| valid pair A2/B2, 1x | 9.1 ms | 26.5 ms |
| valid pair A2/B2, 5x | 16.1 ms | 222.1 ms |
| median of all six legs, 5x | 18.7 ms | 194.5 ms |

The candidate's time was spent waiting for its read threads (215 ms of 222
ms exclusive in `block_shared_read` at 5x): on a loaded host, spawning and
scheduling about 80-100 threads per call costs far more than the
~0.06 ms reads it overlaps. Single-flight opens showed no measurable gain
(history-source opens are not among the base's top stages on this client).
Both were reverted. A pooled reader would add a pool-size knob (principle 5);
it is not pursued while reads are under 2% of engine time. Legs A1, B1, A3
(max load 84-94) and B3 (589) exceeded the CPU count and are not evidence;
with them the other operations' medians differ only within noise.

**Group commit is already the WAL design; no rewrite.** Problem: about 1.1
commits per physical write suggested missing group commit. It is not
missing. Commits capture their WAL witness under the authority mutex and
wait outside it (`server.rs`, dispatch and parallel-commit paths), so later
commits queue frames while a write is in flight. The Strict barrier worker
(`run_wal_barrier`) takes every queued frame as one vectored write and one
`fdatasync`, then acknowledges them all. The Normal writer
(`run_wal_writer`) takes the whole queue as one write and acknowledges at
the written frontier, the tier's documented loss window. Neither has a
batch size, linger timer or cap.
`wal::buffer_tests::strict_commits_queued_behind_a_write_share_one_write_and_one_sync_and_wait_for_it`,
`normal_commits_queued_behind_a_write_share_it_and_are_acknowledged_after_it`
and `a_torn_group_write_acknowledges_none_of_its_commits_and_recovers_the_acknowledged_prefix`
pin it with the real driver threads and concurrent committers. Commits held
behind one write go out as one write; none is acknowledged before its tier's
point. Recovery after a crash returns every acknowledged commit in commit
order, and a torn group write acknowledges none of its commits and recovers
to the acknowledged prefix.

The low ratio follows from utilization: with the writer busy 3-16% of the
time, few commits arrive during a write. The ratio rises exactly when writes
stall (A2: 1.15). Raising it further would need a delay window that holds
commits back (rejected, principle 5). Requiring an `fdatasync` before
Normal acknowledgements would change that tier's contract, not optimize
it. The WAL wait share tracks the medium's write-latency tail (A2 p99 0.4-2
s, the other legs 49-112 ms), which no batching shortens.

**Maintenance bytes in the foreground WAL: measured, not moved.** Problem:
foreground commits share one WAL writer with maintenance, so a commit queued
behind a large maintenance frame waits for that whole write (head-of-line
blocking). The authority's own WAL (`walWriteBuffer.writtenBytes`; the
process-wide `walEncodedBytes` also counts the search projection index's
separate WAL, which shares no writer) held, per 150 s phase:

| base leg | authority WAL | request-attributed | background | overlay-merge pages (merges) |
|---|---|---|---|---|
| A1 1x / 5x | 243 / 752 MB | 13% / 21% | 212 / 595 MB | 205 / 580 MB (510 / 328) |
| A2 1x / 5x | 221 / 716 MB | 13% / 21% | 191 / 568 MB | 185 / 548 MB (425 / 312) |
| A3 1x / 5x | 242 / 744 MB | 13% / 21% | 210 / 587 MB | 203 / 572 MB (512 / 325) |

About 97% of background bytes are overlay-drain merge pages, which a merge
publishes with its root in one WAL transaction (see
[write overlay](TOFUDB_WRITE_PATH_OVERLAY.md)). Search acknowledgements,
retention and archive commits are frequent (8-19 k per phase) but small. On
this volume a merge-sized write costs 2.1 ms at 0.4 MB (1x) and 7.2-7.5 ms
at 1.75 MB (5x), p99 at most 9.3 ms, against 0.6 ms for a 58 KB foreground
frame (a `pwrite` probe on this pool through the userspace client, 120
shuffled writes per size, two rounds). Merge writes therefore occupy 0.6-1.6% of wall time.
Suppose every commit that arrives during a merge write, or queues just
behind one, waited for the whole write. The head-of-line cost would still be
at most 0.03 ms per commit at 1x (0.1-2.6% of the foreground WAL wait) and
0.22-0.24 ms at 5x (20-23% of the 1.0-1.2 ms mean in A1/A3, 1.5% of the
15 ms mean in A2). It reaches at most about 3% of commits, by at most one
merge write (9.3 ms p99), so it cannot produce the 49-112 ms or 0.4-2 s
p99s above.

Decision: keep merge pages in the WAL. Each way of removing them changes the
durability format for a gain capped at about 0.23 ms per commit and 9 ms of
p99 at 5x:

- (a) Merge pages in their own fsynced segment, published by a small WAL
  record. Recovery, online GC, backup, verify and history promotion would
  have to treat WAL-referenced segments as live. That needs a new store
  feature marker and a downgrade converter, and each merge adds a file
  create, fsync, rename and directory barrier on DolphinFS.
- (b) A separate maintenance WAL lane. A merge root must follow every delta
  it covers in WAL order (the dominance proofs above), so recovery would
  need cross-lane sequencing, and rotation, retirement and history
  promotion would double.
- (c) Re-derive merges and log nothing. The tree must still be durable
  before its WAL window retires, so rotation would write the pages in bulk,
  the cost the WAL-carried design replaced on network mounts.
- Priority writes or splitting large frames: frames are hash-chained and
  acknowledged as a contiguous prefix, so order cannot change, and a split
  needs a size constant (principle 5).

Next: the foreground WAL wait is the commit's own write plus the medium's
write-latency tail. Measure that tail on the target host before attributing
it to the engine; this pool showed pwrite p99 50-70 ms at 16-256 KiB and
multi-second stalls in leg A2. Reproduce the attribution with the request
profile run below, then read `walWriteBuffer`, `durability.overlayMergeCommits`
and the `wal_barrier_wait` stage per phase.

Reproduce (from a checkout whose engine source matches the binary):

```sh
python3 scripts/tofudb_load_test.py --root /mnt/your-fs \
  --engine "$TOFUDB_BIN" --scales 1,5 --duration 150 --request-profile --run-id A2
```

## Agent-loop load test

**Problem:** production users wait on the Agent process, not on the engine.
On 2026-11-12 (read-only `/metrics` deltas) the same storage operations took
2.7-11 ms mean from the API host and 230-875 ms from the resident execution
host (`record.get` 267 ms, `turn.event.record` 421 ms,
`worker_job.heartbeat` 708 ms over 2,581 s), while the engine served them in
well under 1 ms. The execution host is one GIL-bound Python process (1.08
cores over 60 s, 460 threads; 99.7% over 2 h), so its latency is client-side
queueing. The storage-only harness above drives the engine from its own
process and cannot see this.

**Decision (owner, 2026-11-12):** reverse the "full isolated server with a
mock model" rejection for the Agent process. `scripts/agent_loop_load_test.py`
starts the real resident execution host (`serverctl_pkg.execution_host`,
launched through the production `ExecutionService` owner) against one isolated
authority started through `StorageSupervisor`. A driver process plays the API
host and the browser. It submits Turns through
`conversation_turn_commands.create_turn` (create the Turn pair, dispatch to
the durable worker) and reads the attempt status once per second. The
execution host then runs the production path: durable worker claim, task
runtime, agent loop, tool execution and event persistence. A separate
fake-model process answers on the Anthropic Messages protocol, which 13,290 of
13,294 sampled production rounds used (also: the production GIL profile shows
`anthropic_outbound` frames and no OpenAI ones). Each round streams thinking,
text and one tool call to `run_command` (`head -c N`), `read_files` or
`search_files` on a scratch workspace, weighted 8,866 : 1,516 : 167 as in
production.

The workload description is `PRODUCTION_PACING`. Each value is a (median,
p90) pair measured from the `apiRounds` of production's 60 most recently
updated conversations (13,294 rounds, read-only `turn.list`):

- rounds per Turn: 62 / 264;
- time to first token: 11.5 s / 28.3 s;
- output tokens per round: 568 / 2,865, streamed at 131 tokens/s with 137
  chunks per 568 tokens;
- reasoning characters: 244 / 2,092; tool-argument characters: 443 / 2,053;
- tool result bytes: 12 KB / 120 KB, the storage harness's inspector-export
  sizes. The same sample's model-facing `toolContent` was smaller
  (1.2 KB / 6.2 KB).

1x is production's 21 running attempts, one Turn per conversation. Scales
multiply attempts; nothing else is tunable.

Per phase the report (`ROOT/results/<run-id>/report.json` and `report.md`)
holds:

- the execution host's own `tofu_storage_rpc_seconds` interval delta, read
  through the private execution control exactly as production's `/metrics`
  exports it (`tofu_execution_storage_rpc_seconds`). Per operation: count,
  rate, mean, and p50/p99 interpolated within the histogram buckets;
- Agent-process CPU cores, threads and RSS;
- a `py-spy record --gil --nonblocking` profile (`gil-<scale>x.folded`). Its
  sample count over the 100 Hz rate is the GIL busy fraction; owners are
  attributed by stack frame, with the same owner names applied to
  production's profile;
- round latency measured at the fake model, from a round's first token (and
  last byte) to the request carrying its tool result;
- Turn counters and the engine counters of the storage harness.

`--production-metrics BEFORE AFTER` compares the 1x phase with two saved
production `/metrics` snapshots.

The phase window ends before the drain. The fake model then finishes every
Turn on its next request, so each phase starts from idle conversations.

Safety is the storage harness's, reused by import: a private 0700 `ROOT`,
refusal inside a live data directory, a pinned engine, and
`TOFU_STORAGE_RPC_DEADLINE_S=20`. Every child runs with a scrubbed
environment whose `HOME`, `TMPDIR` and runtime base (`TOFU_DATA_DIR`: data,
logs, uploads) are under `ROOT`. One deliberate difference:
`TOFU_PROJECT_PATH` names the code checkout, as on production hosts. The
execution host admits only the source tree it runs from, and attaches only to a
connection document for that project and its own data directory. Token counts
need no tiktoken cache under the private `TMPDIR`: the vocabularies ship with
the checkout ([context engineering](modules/context_engineering.md)).

```sh
python3 scripts/agent_loop_load_test.py --root /mnt/your-fs \
  --py-spy "$(command -v py-spy)" --production-metrics before.prom after.prom
```

Before a long run, wait until no `scripts/(tofudb|agent_loop)_load_test.py`
process runs, from a script file so `pgrep -f` does not match its own command
line. Delete `ROOT` afterwards.

**Alternatives considered:**

- Run the durable-worker service inside the driver. Rejected: that measures
  a process that also plays the API host and the fake model, not the
  execution host's own thread population and lifespan services.
- Reuse `tests/mock_llm_server.py`. Rejected: it speaks only the OpenAI
  protocol, which production barely uses, and has no pacing.
- Stub `lib.llm.stream_chat` (the replay seam). Rejected: it removes the
  SSE parsing and request encoding that hold a quarter to a third of the
  production GIL.

Measured 2026-11-12. Setup: engine `4540cc98`, dolphinfs, WAL `normal`,
300 s per phase, fresh `ROOT`. A test gate of another conversation shared the
host. Every phase settled all of its Turns (21, 63, 105). Storage columns are
mean ms, then p50/p99 ms, as the Agent process observed them.

| | production exec host | 1x (21) | 3x (63) | 5x (105) |
|---|---|---|---|---|
| Agent CPU (cores) | 1.08 | 0.38 | 0.73 | 1.00 |
| GIL busy fraction | 0.83 | 0.25 | 0.48 | 0.66 |
| storage RPCs/s | 94.5 | 67.6 | 156.7 | 171.8 |
| turn.event.record | 421 (92/4,826) | 16 (4.2/245) | 121 (8.8/1,587) | 250 (18/4,280) |
| turn.event.record_batch | 419 (120/4,248) | 21 (8.2/243) | 104 (14/1,334) | 530 (39/4,895) |
| record.get | 267 (54/2,545) | 6.7 (2.5/77) | 60 (6.5/232) | 28 (11/243) |
| conversation.get | 253 (57/2,471) | 5.2 (0.7/85) | 33 (4.0/244) | 24 (9.2/226) |
| turn.get | 270 (52/3,177) | 4.5 (1.2/47) | 12 (4.6/96) | 31 (9.4/245) |
| worker_job.heartbeat | 708 (171/>5,000) | 7.2 (2.3/83) | 1,237 (9.7/>5,000) | 620 (48/4,661) |
| task_results.checkpoint | 354 (88/3,905) | 11 (3.0/219) | 62 (7.8/1,381) | 447 (22/4,820) |
| round: last token to next request, p50/p99 ms | - | 501/2,707 | 1,017/30,052 | 7,873/36,315 |
| engine wal_barrier_wait avg (ms) | - | 7.4 | 27 | 261 |

Production's columns are the 2,581 s `/metrics` delta (22:58-23:41 UTC+8)
and a 60 s `/proc` CPU sample. Its GIL fraction comes from the 45 s profile
at 50 Hz.

Findings:

1. The harness reproduces production's regime only at 5x attempts. At 1x the
   Agent process uses 0.38 cores and storage calls stay at 5-20 ms mean. At
   5x it reaches production's CPU (1.0 core, GIL 66% busy): reads queue to
   24-31 ms mean, writes to 250-530 ms, and the time from a round's last token
   to its next model request grows from 0.5 s to 7.9 s at p50. Completed RPCs
   flatten from 157/s (3x) to 172/s (5x). A production attempt therefore
   costs the Agent process about three harness attempts.
2. Part of the 5x write latency is engine-side: `wal_barrier_wait` averages
   261 ms, against 7.4 ms at 1x, while `wal_barrier_sync` stays at 2.3 ms.
   Production's reads (`record.get` 267 ms) do not wait on the WAL, so its
   latency is the client queue the GIL profile shows.
3. GIL owners at 1x/3x/5x are stable. Task runtime takes 41-45%, LLM stream
   parsing 25-28% (`lib/llm/stream.py` `_iter_response_bytes` alone 15-19%),
   the storage client 12%, turn lifecycle 10% and request encoding 5%. The
   top leaf after stream parsing is `lib/conversation_sync/settled_rows.py`
   `copy` (8-10%). Production with the same owner names: project_dispatch
   26%, task runtime 20%, request encoding 16%, stream parsing 12%, turn
   lifecycle 10%, storage client 5%.

Fidelity at 1x, per operation (production rate / harness rate per second):

- `record.get` 17.5 / 6.2; `conversation.get` 14.1 / 2.3; `turn.get`
  8.5 / 2.1; `turn.attempt.get` 6.6 / 1.2;
- `turn.event.record` 7.7 / 19.2; `turn.event.record_batch` 2.1 / 4.4;
  `event.append_batch` 0.8 / 17.8;
- `task_results.abort_requested` 3.7 / 4.4; `worker_job.heartbeat`
  2.2 / 1.05;
- `task_results.checkpoint` 0.9 / 2.4; `raw_archive.put` 0.38 / 0.88;
  `tool_result_artifact.put` 0.10 / 0.30.

The harness streams more per attempt: every attempt is in a model round,
whereas production attempts also wait on long tools (`poll` median 174 s) and
on children. It reads less.

Production operations the harness does not issue: `project.dispatch.get`
(15.9/s, the second largest), `project.dispatch.commit`/`list`, all
`integration.workspace.*` (get 4.9/s, `delivery_action_page`, `set_meta`;
`peek_ready` only at 0.03/s), `queue.list` (1.1/s; harness 0.07),
`swarm.agent.*`, `project_brain.work.change`, `turn.list`, `turn.revision`
and `turn.create_pair`/`queue.enqueue` (coordinator-submitted Turns).
Project-dispatched workers and coordinators (74 of 85 active production
conversations on 11-08) need a project, its dispatch grants and integration
workspaces; the harness drives chat-mode Turns only. Its conversations also
start empty, while production coordinators carry hundreds of Turns.

Verification: `tests/tofudb/test_agent_loop_load_test.py` covers the
process environments (private root, execution-host attachment, no engine
settings in the application), live-root refusal, histogram interval
accounting and bucket quantiles, parsing production `/metrics` into the
execution control's series, the fake model's round plan, its Anthropic event
stream and round-latency pairing, and GIL attribution. The run also found
that the shared `_text` helper returned short strings (up to 139 characters
short): two harness Turns failed on a near-empty final answer. It now fills
the requested size (`test_synthetic_text_has_exactly_the_requested_size`).
That also corrects slightly short small payloads in the storage harness's
earlier runs.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
