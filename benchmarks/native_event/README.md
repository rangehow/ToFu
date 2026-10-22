# Native event retention experiment

The candidate stores Python event references and byte counts in separate Rust
deques. It compares actual TaskRuntime append/poll paths, including producer
clock stamping, JSON byte accounting and publication guards. Generated candidate
source changes only retention and the private replay view. Canonical cursor
interpretation remains shared. This is an experimental boundary, not a callable
application backend: legacy task adoption, public list mutations, native package distribution, live transport and durable recovery
admission remain required before promotion. Eviction/clear finalizer reentrancy
and GC cycle teardown have executable checks in `check.py`.

Build with Python 3.12 or newer, Rust and the selected Python environment:

```bash
export CARGO_TARGET_DIR=/tmp/tofu-native-event-target
export PYO3_PYTHON="$VIRTUAL_ENV/bin/python"
cargo build --release --manifest-path benchmarks/native_event/Cargo.toml
export TOFU_NATIVE_EVENT_LIBRARY="$CARGO_TARGET_DIR/release/libtofu_event_prototype.so"
export TOFU_BENCHMARK_PYTHON="$PYO3_PYTHON"
"$PYO3_PYTHON" benchmarks/native_event/check.py
"$PYO3_PYTHON" benchmarks/native_event/runtime.py
"$PYO3_PYTHON" benchmarks/native_event/runtime_faults.py
export TOFU_NATIVE_EVENT_OUTPUT=/tmp/tofu-native-event-runtime.json
"$PYO3_PYTHON" benchmarks/native_event/runtime_measure.py
```

The runner defaults to 1,000 unprofiled individual operations, 20 warmup
operations, three alternating fresh-process pairs and separate memory processes.
`TOFU_BENCHMARK_SAMPLES` can change the sample count (minimum 100). Each sample
reports the wall/CPU duration of one actual append or poll operation. Counts are 64, 4,096 and 65,536;
cases are append/eviction, four-event tail poll and full-window poll. Raw samples,
source digests and runtime identity accompany the result; no storage or network
is started. Check all metrics against the system admission contract and retain
rejected measurements. Do not infer whole-system speedup from this experiment.

The default build uses the active CPython API; `--features stable-abi` enables
PyO3's stable ABI for comparison. Native library and Python compatibility are
part of the experiment identity, never a reason to replace host libraries.

`check_public.py --output /tmp/native-public-events.json` uses actual
`AgentExecution`, Headless auth/event/SSE routes and Quart's in-process ASGI
client. Counts 1 and 64 force retained-window eviction; old/base/end/future
cursors must preserve exact projected pages and SSE event traces, including
terminal synthesis. Unauthenticated reads are rejected and terminal observers
are released. A running execution's pending async subscription is explicitly
cancelled/joined; the task remains running, a subsequent event is replayed on
reconnect and no waiter remains. Both list and native variants run those checks.

The native library and production/experiment sources have retained digests.
This covers public projection, not real TCP/backpressure, durable recovery,
native packaging or public tail performance. `AgentExecution` uses the
canonical iterable reader, which may copy the whole native window; the private
experimental fast reader in `TaskRuntime.poll` is not automatically used there.

The next native build supplies an owned snapshot `__iter__`: canonical public
replay already holds the task lock, and a full snapshot no longer dispatches
one Python-to-Rust `__getitem__` call per event. The iterator's membership is
fixed at creation and retains original event identities across append/clear;
it is not a mutable-list iterator compatibility claim. Checks cover snapshot
ownership, reference-cycle teardown and the existing runtime/public boundaries.
**Problem:** public iterable fallback incurs per-element language crossings.
**Decision:** build one owned native snapshot and return its Python iterator.
**Alternatives:** a live custom iterator keeps per-item crossings and races;
bypassing canonical cursor/lock interpretation duplicates replay authority.

`iterator_measure.py --reference /path/old.so --candidate /path/new.so --output
/tmp/native-iterator.json` measures this snapshot kernel at 4,096 and 65,536
events, three alternating pairs of 100 wall/CPU observations after 20 warmups,
with separate RSS workers and retained library hashes. The old artifact must
remain unchanged. `--count 4096 --samples 2 --pairs 1` is smoke only. Whole
public paging/SSE timing, native packaging, legacy mutable-list adoption and
durable recovery remain gaps; this cannot waive the earlier replay regressions.

`public_measure.py --library /path/new.so --output /tmp/public-replay.json`
compares the actual `AgentExecution.event_page()` on list/native windows of
64, 4,096 and 65,536 events with stale, four-event tail and future cursors.
Identical synthetic dictionaries seed both representations; append is outside
this measurement. Every projected page matches a list-backed canonical oracle
after the measured wall/CPU interval. Three alternating pairs each contain 100
observations after 20 warmups, with independent memory workers and retained
library/source hashes. `--count 64 --samples 2 --pairs 1` is smoke only.
**Problem:** a snapshot kernel gain may disappear in canonical public replay.
**Decision:** measure that unchanged public projection before adoption.
**Alternative:** timing the private optimized reader would bypass the actual
consumer. HTTP transport, pagination, producer cost, concurrent readers and
durable recovery remain separate gates; this benchmark does not replace them.

`public_tail.py` binds the existing experimental native reader to one execution's
otherwise canonical `event_page`, `events` and `events_async` in private function namespaces. The binding
requires the exact native window and task lock; no global production method is
patched. Run it with the library environment to verify cursor/status parity,
caller mutations and membership changes. `public_tail_measure.py` reuses the
public measurement fixture/driver with this one reader substitution; all new
adapter sources join its hash identity. Its invocation matches `public_measure.py`.
**Problem:** generic iterable replay snapshots the whole window before reading a
small suffix. **Decision:** reuse canonical cursor interpretation with the native
tail view. **Alternative:** a new replay algorithm would duplicate authority.
This remains an experiment pending measured gains and public/durable integration.

`check_public_tail.py --output /tmp/public-tail-routes.json` reuses the actual
Headless auth/page/SSE oracle and pending-observer cancellation/reconnect checks
with a private execution constructor. It tests all three bound read ports without
patching application/module globals. This proves in-process ASGI semantics,
not real TCP backpressure, fault recovery, packaging or performance admission.
The unadapted public-read comparison
retains all 5,400 samples: whole-window iterable snapshots heavily regress tail
and empty reads. Keep that result when evaluating the suffix adapter.

The first full suffix-port comparison
retains another 5,400 samples/108 independent workers: tail reads recover from
whole-window copying, but empty/future P50 still regresses 14-21% and measured
guards block adoption. The experimental view now sends an exact nonnegative
open-ended integer slice straight to native `tail`, avoiding two redundant native
length queries. General/custom/huge/negative slices retain canonical normalization.
Semantic and full measurement gates must run again for this changed adapter.
**Alternative:** using producer base hints alone would lose the existing first
event sequence authority; changing that contract is unnecessary for this fix.

The full direct-suffix result
still fails admission: future/empty P50 regresses12-18% and most RSS guards fail.
The native-only public adapter imports the generated experimental TaskRuntime;
that extra experiment residency belongs to these numbers. A minimal packaged
reader remains unmeasured, and this result cannot reject all native designs.

`native_reader.py` now owns the shared view and canonical-helper reader without
importing/generating the experimental TaskRuntime. The producer retains its
existing reexports; public bindings import the lightweight owner directly.
**Problem:** public-read-only memory trials paid unrelated producer import/code
generation residency. **Decision:** separate that adapter dependency without
changing replay behavior. **Alternative:** subtracting estimated bytes from RSS
would fabricate evidence; the new dependency graph needs independent measurement.

The full lightweight reader comparison
retains5,400 exact page observations/108 independent workers. All RSS guards now
pass (~.99-1.02 ratios), but empty/future P50 still regresses13-23% and several
CPU/latency guards fail. The dependency correction is retained; native retention
is still unadmitted. Live transport, durability and producer integration remain
required; passing memory alone does not replace those gates.

**Metadata crossing decision — Problem:** the suffix adapter repeatedly crosses
PyO3 for length/first-event/empty-tail reads, and small future-cursor controls
regress despite larger-window gains. **Decision:** the experimental `bounds()`
returns coherent length and an owned first-event reference in one native call.
`bounds_reader.py` creates an ephemeral metadata view while holding the task
lock; canonical helpers still interpret all cursor and terminal behavior. Empty
open-ended suffixes avoid another native call. **Alternative rejected:** caching
metadata across calls would retain evicted event references or become stale after
producer mutation. `check_bounds.py` verifies 1,000 metadata operations, ownership
and GC, 96 exact public pages, actual Headless auth/SSE/terminal and observer
cleanup. Retention/runtime/fault neighbors pass; `bounds_measure.py` retains
independent paired samples and source/library witnesses. Its two-sample 64-event
pilot validates the driver only (future ratio 1.061); full controls remain
required. No producer/TCP/durable/default performance admission is inferred.

The full coherent-bounds comparison verifies 5,400 exact page observations and
108 distinct latency/memory workers. Future-cursor P50 regresses 12–23% across
64/4,096/65,536 events, and other measured guards fail; there is no stable
primary public-page gain. `system_event_bounds_reader_20261008.json`
retains every ratio, raw/source/library witness and unchanged public baseline
identity. This adapter is rejected for promotion despite semantic/fault parity.
# Chunked Python retention experiment

`check_demand.py` verifies the measured-crossover policy: capacities below 1024
reuse the original class directly; larger windows stay lists until their actual
population reaches 1024 and eviction is imminent. Any consumer, including an
empty-window read, permanently selects original list retention for that task.
Migration happens inside the canonical producer lock; consumer marking releases
the non-reentrant lock before calling the conversion/replay helpers. Publication
callbacks retain their original lock placement. No application default changes.

`demand_measure.py --samples 1000 --pairs 3 --output /tmp/event-demand.json`
compares actual append, empty-history growth, alternating append/tail read, tail
and full replay at 64/1024/4096/65536 capacities. Population/setup is outside
timing; first-operation migration is recorded separately, while every later
migration remains in its operation sample. Independent memory workers repeat
the same count. Final trace, membership, bytes and layout witnesses must agree.
This does not certify independent cold-start/public/durable/whole-event scope.

`layout_crossover_measure.py --samples 1000 --pairs 3 --output /tmp/event-layout.json`
measures actual write-only runtime operations at 64, 128, 256, 512, 1024, 2048
and 4096 capacities. It uses the same isolated latency/memory workers, cold
operation, warmups, retention/trace oracle and source identity checks as the
chunked comparison. This determines a layout crossover; it does not select a
production threshold or certify mixed producer/consumer and first-reader costs.

`check_chunked.py` checks count/byte-target retention, the canonical newest-event
exception, snapshot ownership/GC, and actual TaskRuntime sequence, publication,
oversize and concurrent producer behavior. The private runtime uses chunks while
there are no readers, then atomically converts to original lists at the first
read. Producer layout selection stays inside the original event lock; durable
callbacks remain outside it. A concurrent consumer/producer test and a callback
that waits for another reader exercise those boundaries. No production default
or storage authority is changed.

Run `python benchmarks/native_event/chunked_measure.py --samples 1000 --pairs 3
--output /tmp/chunked-event.json`. Independent latency/memory workers compare
actual append, tail and full poll methods at 64, 4096 and 65536 event capacities.
Each arm has a separately recorded first operation, 20 warmups and raw wall/CPU
samples. The one-time reader conversion belongs to the cold operation, not the
steady-state samples. The final four-event witness avoids triggering conversion
in write-only cohorts. All source hashes, output witnesses, retained event counts
and JSON-byte totals must match. Resident RSS includes setup/cold conversion and
untimed final witnesses; this does not certify real TCP, durable recovery,
legacy external mutation, long-run retained growth or the whole events domain.

`durable_runtime.py` owns a private native authority, the actual
`StorageEventBatcher`, and private bindings of canonical event-log functions.
The shared native harness verifies the compiled source/library identity; no
production client getter, batcher or configured authority is changed.
`check_durable_runtime.py` verifies seed commits, cold replay, reopen, four
producers, duplicate receipts, batch plus fallback failures, withheld attempt
frames and successful recovery. The synthetic attempt label exercises the
producer withholding branch; it does not prove native live-attempt fencing.

Problem: retention-only gains do not establish durable event-path performance.
Decision: `durable_measure.py` pairs actual append acknowledgement, complete
bounded replay, native engine reconnect/read, four-producer waves of 32
committed events, and failure/recovery. Alternatives rejected: an in-memory
sink omits commits; a fake cold replay omits recovery; changing the selected
application backend would contaminate the evidence and violate ownership.

Run the owning native check with `TOFU_EVENT_NATIVE_ENGINE`; then run a pilot
with `--engine /path/to/tofu-db --count 1024 --samples 2 --pairs 1
--output-prefix /tmp/event-durable-pilot`. Default formal cohorts are 4096
seed events and 100 samples in three independent pairs. Separate memory
workers run the full cohort; peak RSS includes the complete durability oracle,
and retained bytes use canonical live replay wire accounting. Every persisted
sequence is reconciled, each producer's order is checked, and exact logical
producer/index multisets are compared where concurrent schedules differ.
CLI standard reports keep missing domains incomplete. HTTP, native attempt
fences, and full application publication remain separate verification owners.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
