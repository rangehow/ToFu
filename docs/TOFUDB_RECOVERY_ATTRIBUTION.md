# Recovery attribution and qualification

Owner: `packages/tofu-db/src/recovery_profile.rs` and `engine.rs`.
Durable state and generation publication remain owned by
[TofuDB storage](STORAGE_TOFUDB.md) and
[rotation handover](TOFUDB_ROTATION_WINDOW.md).

## One open, one completed population

**Problem:** the existing `engine_open` startup timer cannot distinguish WAL
scanning, payload lookup/replay and completion of a sealed predecessor. A slow
restart can therefore be incorrectly attributed to a neighboring source edit.
Copying a FUSE authority to local XFS also changes the measured operation even
when all database bytes match. Request profiling additionally changes embedded
command scheduling and cannot serve as the normal-path startup observer.

**Decision:** build the non-default `recovery-profile` Cargo feature, then set
`TOFU_DB_RECOVERY_PROFILE=1` to enable one instance-owned report per `Engine::open`.
Default builds use a zero-sized inline no-op with no environment read, clock,
state or Drop. They share the same recovery algorithm and observer call sites. The ordinary structured engine logger emits target
`recovery_profile`; its `message` contains a JSON `tofudb.recovery-profile/v1`
object. In diagnostic builds, the flag is read once per open. With the flag off, opens
read no probe clocks,
allocate no probe collection and emit no recovery report. There is no worker,
process-global accumulator, source payload, path, tenant or operation label.

A serving process can open both the primary authority and a native search
projection. Each is a separate `Engine::open` report. Never assume one report
per process or merge their work counters. Bind source reports to `startup_timing` / `authority_open` and deferred search
reports to `background_timing` / `search_projection_open`. Startup
`search_projection_prepare` constructs only the handle. Ambiguous/interleaved
spans or reports outside a known owner do not establish primary attribution.

The fixed phase vocabulary partitions elapsed time, rather than nesting spans:
control and lease setup; selected WAL scan; block catalog; recovery ID selection;
packed lookup; history catalog; payload replay; witness publication; runtime
initialization; sealed-predecessor preparation, encoding and publication; finish.
All phases appear, with `entered` distinguishing skipped work from a measured
zero. Their `elapsedNanos` sum exactly to the report's elapsed interval. These
are elapsed phases, not CPU time or proof that all work was I/O.

The work object records scan record/payload-byte counts, sealed record count,
bounded selected recovery IDs, payload segment count and admitted replay count.
A value not yet observed is null. The input scan can include an unconfirmed tail
that later fails reference admission; it must not be relabeled as committed work.
The replay count is reported only after that loop completes. Existing limits,
reference checks and unconfirmed-tail decisions remain authoritative.

`completed` becomes true only at successful return. Error or panic unwinding
closes the current phase and retains an incomplete report; it never converts an
error into success. Failed-open elapsed time includes cleanup until this owner
is dropped. Report formatting/emission is outside the recorded interval, but
still costs the enclosing startup. Instrumented clocks are diagnostics and are
excluded from default-path performance qualification.

The enabled-feature observer occupies 272 stack bytes on the measured Linux
build; the default-build no-op occupies zero bytes. Its arrays
have fixed size; output allocation happens only for an enabled report. No new
resident cache, thread, file or durable namespace is introduced.

**Alternatives:** subtracting process-lifetime request totals loses exact open
identity; timing copied fixtures on another volume changes the workload; logging
every block adds unbounded output proportional to recovery; treating unfinished
stages as zero hides the location of failure. The single existing open owner
provides the needed interval without any new execution path.

## Replay buffer ownership

Problem: recovery retained every scanned WAL frame while cloning each admitted
frame into the committed transaction list. The temporary overlap added one
entire backlog of requested allocations and copying before open could return.

Decision: after the record's existing payload/reference validation succeeds,
transfer its encoded buffer into the committed transaction. Keep sequence,
hash and file-local offset metadata in the scan result for the unchanged
durable-witness and tail-truncation checks. The preliminary complete WAL scan,
per-open source validation, cancellation boundaries and checkpoint bytes do not
change. No format, cache, worker, quota or weaker acknowledgement is introduced.

Alternatives rejected: skipping validation or trusting a previous open changes
integrity guarantees; a backlog cap refuses accepted work; a second cache keeps
the duplicate ownership. This change removes one redundant copy, not all
backlog-dependent memory: committed encoded bytes, decoded state and staged
carried content retain their existing lifetimes.

`engine_recovery_memory_tests.rs` uses the existing `allocator-diagnostics`
feature in a fresh child process. It recovers 256 exact 128 KiB transactions,
verifies every payload and its logged envelope, and rejects an extra full
backlog of peak requested bytes. Seeding and concurrent suite workers cannot
contaminate that child's counters; no allocator counter is reset. The bounded
parent owns kill/reap. These are requested Rust allocation bytes, not RSS or
an application latency promise. Ordinary generation, corruption, missing-source,
unconfirmed-tail and cancellation tests retain the recovery safety boundary.

The 33,568,000-byte encoded fixture measured 100,779,716 peak requested bytes
before transfer and 67,211,716 afterwards: exactly one encoded backlog removed
(33.31% of that peak). Retained requested bytes after open stayed 67,185,092;
all 256 transactions and logged envelopes matched. The default-feature prefix
test also checks both missing-reference outcomes after valid records: an
unconfirmed suffix alone is truncated and the prefix survives checkpoint/reopen;
a durable bad reference refuses open with CONTROL and WAL evidence unchanged.

## Shared VFS range reads

**Problem:** Unix `RealFile::read_exact_at` uses a seek followed by an exact read.
A fixed terminal image exposed thousands of such pairs. Removing a syscall is a
mechanism observation, not an end-to-end performance result.

**Decision:** retain the existing VFS implementation. The positioned-read
alternative removed 6,498 seek/read pairs from one exact recovery image, but
recovery and pipeline observations did not establish a general, qualified gain.
Keep the prototype, adverse observations, source/artifact identities and all
corrected comparison scopes in the external run evidence. Do not promote it
from syscall counts alone or weaken fault-injection and error behavior.

**Alternatives:** adding a read cache adds ownership/invalidation work without
first establishing the bottleneck; bypassing VFS loses its fault/encryption
boundary. A runtime optional observer still carries state and branches in an
ordinary build. The non-default diagnostic feature keeps attribution available
while compiling that observer state and environment/clock work out by default.

Verify `recovery_profile` and `tests/startup_integrity.rs` in both default and
`recovery-profile` feature builds. The former must ignore a probe environment
flag and preserve exact recovery; the latter checks exact sequential accounting,
actual replay counts, and incomplete failed-open reports without changing the
CONTROL/WAL evidence. Full native fault/recovery suites cover both builds.

## Repeated carried content

Problem: the per-open carried-payload lookup checked history before consulting
its existing verified bit. Historical hits also returned without setting that
bit. Repeated references therefore reopened routing pages and source files,
even after their exact content had already been verified during the same
exclusive recovery. A cold-cache deterministic regression records 512 reads
for 128 repeated references to one published historical payload.

Decision: consult the existing content-addressed proof first, and record
successful history and conservative-fallback verification in that same map.
Each supplied payload is still hashed; a fresh lookup must verify its source
again. Missing or damaged copies retain WAL repair, other I/O errors propagate,
and unselected identifiers retain conservative resolution. No
cross-open cache, new storage format, background worker or retention is added.
The preliminary ID selection uses the existing strict non-copying envelope
decoder; actual replay continues validating all carried bytes and references.

Alternatives: a second persistent proof introduces generation/integrity
ownership; skipping first-use source verification weakens deduplication and
corruption handling; suppressing I/O errors changes recovery semantics. Reusing
the already-owned proof removes repeated work without those changes.

`repeated_carried_history_recovery_verifies_each_content_once` checks actual
VFS reads, per-pass proof lifetime, nonrepairable errors and crash readback.
`repeated_carried_recovery_paired_measurement` is an explicit ignored test:
set `TOFUDB_RESTART_BENCH_PARENT` to the measured volume and run it with
`--release --lib -- --ignored --nocapture`. Its eight ABBA legs use the same
default-feature binary and disposable authority, compare the conservative
reader with per-open reuse, and verify bytes, sequence and segment count after
each arm. This is a repeated-content mechanism experiment, not a complete
production startup or filesystem cold-cache measurement.

On the original DolphinFS/FUSE volume, the eight-leg default release run
measured reference `[108200, 105751, 105680, 121137]` microseconds versus
reuse `[1653, 1688, 1696, 1668]` for 128 references (medians 106,975.5 and
1,678 microseconds). All exact byte, sequence and segment-count checks passed.
The deterministic regression changes repeated source reads from 512 to zero
after first verification; a fresh pass still reaches an injected PermissionDenied.
These measurements do not attribute the previously observed 111.267-second
production authority open or establish its remaining duration.

## Recovery history lookup covers the complete WAL reference set

Problem: distinct WAL references each walked the same immutable history routing
pages, both to recover content and to decide which loose copies need syncing.
When those pages exceed the ordinary payload cache, the walk repeatedly evicts
its own preceding pages. Recovery then performs history-sized I/O per WAL
reference and can exhaust the supervised startup deadline. A 572,675,112-byte
incident WAL contained 226,130 distinct references; the former 65,536-ID lookup
cutoff left most references on that repeated path even after batching. Those
counts are a read-only framing observation, not an integrity certificate.

Decision: select every distinct reference in the chosen WAL and resolve packed
and history locations in one verified pass. Each relevant history routing page
is read and validated once; only candidate locations and exact absence survive
until replay finishes. Candidate order, current source membership and payload
content verification remain authoritative. An unreadable page records its error
at the original position for each affected ID: an earlier valid copy may answer,
but reaching that error preserves repair, alternate-copy fallback and I/O failure
instead of consuming a transient fault. Cancellation discards preparation; every
new open verifies its own sources. Sync staging consumes the same evidence.

Temporary metadata follows distinct selected references and routing levels;
it retains neither history page bytes nor another copy of carried payloads.
It is released with this open. `activeWalRotation.recoveryLookupPolicy` replaces
the internal build contract's recovery-location cutoff. Its former 65,536-entry
value remains solely the unchanged persisted-hint retention limit, now named
`payloadSegmentReadCache.maximumPersistedHints`. All build-time consumers move
together; disk and wire formats, WAL admission and startup deadlines do not change.

Alternatives rejected: raising the startup timeout or the ID cutoff only moves
the failure to a larger backlog; enlarging the ordinary cache moves it to a
larger history; trusting cache files or skipping payload validation weakens
recovery integrity. The regression exceeds the former cutoff with zero payload
cache. Neighboring tests retain failed-source, valid-copy and cancellation checks.

The original 128-distinct-reference fixture measured 118 history-page reads
before batching and one after it, with the payload cache disabled. Loose-sync
staging adds no page read. The expanded 65,537-reference regression also reads
one page, including restoration and sync staging. This is work-count evidence, not a production
restart-time or filesystem durability guarantee; the packed-catalog scan and
actual payload validation remain separate costs.

## Reopen time against WAL size

**Problem:** after a graceful stop the R3 engine reopens in about 0.4 s (no
replay), but a crashed or latched authority took 625 s (2026-11-13 02:07,
`engine_open` 625,139 ms) and a killed-stop probe 690 s. Without a phase split
it was unknown whether that cost follows the active WAL's bytes, the premise of
keeping the WAL small, or something else.

**Decision:** `packages/tofu-db/src/engine_restart_open_tests.rs` measures it with
three ignored tests, each run in its own process on a disposable authority:

- `dirty_open_build_base` writes indexed history (16 M locations) and a payload
  catalog at production scale (4,092 segments of 57,000 blocks).
- `dirty_open_build_window` commits an unretired window to a requested active
  WAL size, optionally behind a CONTROL-owned sealed predecessor, and ends without
  engine teardown. That leaves what a SIGKILL leaves.
- `dirty_open_phase_measurement` drops the page cache and times `Engine::open` in
  a `recovery-profile` build. It checks the acknowledged durable sequence and reads
  back every carried window block and a sample of catalog and history references,
  byte for byte.

The window follows production's WAL composition: about 360 distinct references
per MiB and a 27% inline share. Runs go on isolated VMs, one per WAL size,
launched together; never on the production host.

Result on R3 engine source (`f3102951`), VM local disk, cold cache, 3 runs per
point (median seconds):

| active WAL (selected IDs) | `engine_open` | packed lookup | payload replay | WAL scan + ID decode |
|---|---|---|---|---|
| 68 MB (23,809) | 105.3 | 92.8 | 11.0 | 0.10 |
| 269 MB (94,465) | 128.1 | 86.5 | 40.4 | 0.33 |
| 714 MB (250,369) | 223.9 | 110.3 | 111.1 | 0.97 |
| 1,075 MB (376,833) | 286.8 | 118.9 | 164.8 | 1.49 |
| 561 MB sealed + 655 MB active (426,753) | 294.4 | 104.9 | 184.0 | 1.48 (+3.0 sealed completion) |

The open is CPU-bound on one thread (97-98%). Its cost follows the catalog and
the number of distinct references, not WAL bytes:

- The packed lookup reads every catalogued index (11 GB) and builds the union
  filter, routes and membership witnesses over every catalogued ID. It costs the
  same for any crashed open whose WAL names a payload.
- Payload replay re-reads a segment's index for each reference the bounded index
  cache misses.
- Reading and decoding even a 1.2 GB window takes under 2 s.

A smaller WAL therefore shortens only payload replay. Raw runs and method:
`.eval-runs/tofudb-reopen-20261113/` (`METHOD.md`, `results/README.md`).

Alternatives considered: timing production restarts gives no phase split (the
profile is a diagnostic build), and copying the production authority to local
disk changes the measured operation (see the fixed-image boundary below);
production stays read-only.

## Recovery builds the catalog location table

**Problem:** a crashed open whose WAL names any payload ran one catalog-wide
pass that built the packed filter, its per-segment membership witnesses and
prefix routes over every catalogued ID (87-119 s of a 105-294 s open above).
Replay then re-read a whole segment index (2.7 MB at production shape) for each
WAL reference that the bounded index cache missed. Replay needs none of the
filter, routes or witnesses, and the online GC driver rebuilds them after
readiness anyway. Loose compaction also moves history manifests, routing roots
and routing pages into payload segments. A lookup of only the WAL's own IDs
would therefore leave each history-metadata read to a catalog-wide candidate
scan.

**Decision:** recovery reads every catalogued index once, verified
(`with_verified_index`, the first-table builder's path), into the
[catalog location table](TOFUDB_BLOCK_READ_PATH.md)
(`PayloadSegmentStore::build_location_table_selecting`). In the same visit it
records the exact index position of each block the WAL names, through a
galloping merge of the sorted, verified index with the sorted selection. Where
several segments hold a block, the latest in catalog order answers.

- The table is published for the installed publication (source `recovery`).
  Every recovery read (history manifest, routing root and pages, references
  outside the selection) resolves by position, and a fully indexed table proves
  absence. The online GC driver then finds the first table already published.
- A WAL reference reads its recorded position: one entry read and one payload
  read, checked by CRC32C and content hash. Errors keep the kinds the
  whole-index read reported (a missing segment file is invalid data), so replay
  repairs from a carried copy, propagates other I/O errors, truncates or fails
  closed as before.
- The packed filter, routes and witnesses are built after readiness by the
  online GC driver, as after a clean open. Until then dedupe probes restage,
  which is idempotent by content ID.

A segment whose index fails verification is left out of the table:

- The lookup is incomplete and the table never proves absence. The segment
  stays catalogued, so blocks only it holds answer missing or invalid, never
  absent.
- Recovery logs each such segment by name (`recovery`
  `step=catalog_index_unverified`) and counts it (`system.metrics`
  `durability.recoveryUnindexedSegments`); `verify` reports it
  (`payload.failed_segment_ids`).
- Deliberate change (engine reviewer ruling, 2026-11-13): recovery serves any
  verified copy of a block, as reads already do once the first location table
  is published. Before, a damaged index earlier in catalog order could fail the
  open, or truncate a record, whose block had a verified copy in another
  segment.

The table holds one 8-byte entry per catalogued block. That is 1.87 GB at
production's 233 M blocks, which the GC driver held within about 21 s of
readiness anyway; now the table is resident during replay.

**Result:** the same harness, method and VM shape as the curve above. Each
VM builds one crashed fixture and alternates cold opens of the R3 engine and
the candidate on copies of identical bytes, 3 each. Median seconds:

| active WAL | R3 `engine_open` | candidate | speedup | catalog pass, R3 → candidate | payload replay, R3 → candidate | peak RSS (MiB), R3 → candidate |
|---|---|---|---|---|---|---|
| 68 MB | 106.6 | 22.2 | 4.8x | 94.6 → 19.8 | 10.7 → 0.8 | 1,775 → 1,939 |
| 269 MB | 141.0 | 28.9 | 4.9x | 99.7 → 24.6 | 39.9 → 2.6 | 2,116 → 2,276 |
| 714 MB | 210.1 | 34.7 | 6.1x | 102.6 → 24.3 | 104.7 → 8.2 | 2,876 → 3,020 |
| 1,075 MB | 278.0 | 42.9 | 6.5x | 115.0 → 28.0 | 159.9 → 11.9 | 3,533 → 3,627 |
| 561 MB sealed + 655 MB active | 308.2 | 48.9 | 6.3x | 119.2 → 29.5 | 181.9 → 13.0 | 3,715 → 3,883 |

- Every open reached the acknowledged durable sequence, and the window's blocks
  read back byte for byte.
- The final revision repeated the 68 MB and 1.2 GB points on fresh VMs: 94.0 →
  19.0 s and 302.1 → 48.2 s.
- The catalog pass itself is 4-5x cheaper (no filter, witnesses, routes or
  per-ID probes); payload replay is 13-14x faster.
- Open-thread CPU falls from 97-98% to 84-89% of the open, so the remaining
  pass waits on reads more than before.
- Raw runs: `.eval-runs/tofudb-reopen-20261113/raw/lever1-*`.

**Alternatives considered:**

- Resolving the WAL's IDs through `CatalogLocationTable::candidates` after the
  build. Entries keep a 36-bit ID prefix, so at 233 M blocks about 0.34% of
  lookups would find another block's prefix, and a false candidate in a later
  segment would shadow the true location. The merge uses the verified indexes'
  full IDs.
- Locating only the WAL's IDs without a table: misses the history metadata
  that loose compaction moved into segments.
- Raising the index cache for recovery: a memory-sized knob that only moves
  the re-reads.
- Parallel record decode: WAL read and decode take under 2 s even at 1.2 GB.
- Per-segment parallel index reads: deferred until the after-curve shows how
  much of the open is I/O wait. The pass is sequential, like the first-table
  builder.

**Verification:** `engine::recovery_lookup_tests` covers the per-ID
differential over seeded stores, the damaged index (no partial entries, both
catalog orders, confirmed and unconfirmed records), catalogued history
metadata and the published first table. Also
`engine::recovery_witness_tests::a_dirty_reopen_reads_each_catalogued_index_at_most_once_and_builds_no_packed_filter`
and `block::tests::carried_recovery_lookup_repairs_invalid_files_but_propagates_io_errors`.

## Fixed-image comparison boundary

A recovery fixture is copied only after SIGKILL has been joined. Its exact file
hashes, source filesystem/device, original binary and acknowledged readback
population are retained. The copied authority is immutable evidence; each arm
opens a separate clone. Copying and hashing precede the clock and warm file data,
so these observations are not cold-cache results. Readiness, exact readback,
offline integrity and teardown are separate measurements.

A valid matched comparison fixes the fixture hashes, target filesystem/device,
durability, features, probe selection and readback population. Original-volume
FUSE runs and local-XFS controls are distinct cohorts. All failed/adverse arms
remain retained. A successfully opened database without exact readback is not a
qualified result. Standalone raw SQLite reopen is not an equivalent substitute
for a native crash-recovery fixture with different admitted work.

Production source fingerprints exclude integration-test files. Freeze the
integration-test/fixture inputs separately when certifying the candidate; retain
both witnesses and refuse changes during native commands. Pipeline and adjacent
raw-SQLite workloads remain required before inferring broader performance.

## Build and startup boundaries

A source digest identifies inputs; it is not an executable identity. Pin the
executable and embedded library hashes, enabled features, compiler, release
profile, code-generation concurrency and build context. Rebuild the unchanged
reference in the candidate's build context before source-level attribution.
Equal source digests with differing executable code sections are an explicit
comparison variable. Preserve earlier cross-artifact measurements, but do not
silently relabel them as an isolated source-change experiment. Non-allocatable
symbol/build-ID differences alone do not prove different executing code.

The benchmark adapter's whole `_launch_serve` interval includes Python client
imports, schema validation and client setup after reading the daemon's readiness
pipe. Keep three distinct observations: launch-to-complete-readiness-line;
readiness-line-to-client-ready; and the complete launch-to-client-ready interval.
The existing native `startup_timing` `engine_open` interval covers a narrower
boundary. Authority open and its engine-open child overlap; never sum them.
Daemon readiness also includes projection, resources and listener setup.
Neither a flat engine-open clock nor a faster full client launch establishes
that every startup component is unchanged. CPU snapshots are a separate measure.

A fixed-image report retains every raw observation and its exact readback before
ratios. A few repeated opens describe those observations, not a population P99.
Failed performance SLOs remain failed: the pipeline CLI's exit 1 can accompany
correct and latency-eligible measurements. Validate the report's correctness,
eligibility and expected CLI status separately; an SLO failure must neither be
hidden nor mislabeled as an infrastructure failure that is retried away.
