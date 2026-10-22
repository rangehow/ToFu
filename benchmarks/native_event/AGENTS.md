# Native event experiment guidance

This directory owns an offline Rust/PyO3 retention candidate and isolated
TaskRuntime comparisons. It does not export a production runtime or change
application event, storage, or publication authority.

Build artifacts and measurements live outside the checkout. `src/lib.rs` is
retained Rust source; Cargo produces the shared library. `runtime.py` constructs
a process-local candidate from the current TaskRuntime source, with asserted
replacement boundaries. Cursor interpretation reuses the canonical replay helper.

Read `README.md` for commands. Verify `check.py`, `runtime.py`, and
`runtime_faults.py` before measurement. Freeze all compared sources while the
runner is live; failed or missing gates never authorize production promotion.

`check_public.py` exercises actual AgentExecution and Headless event/SSE
projection via an in-process ASGI client, including observer cancellation.
It does not prove real transport, durable recovery or public tail performance.

The experimental `__iter__` returns an owned membership snapshot, not a live
mutable-list iterator. `check.py` verifies snapshot references/cycles;
`iterator_measure.py` compares untouched old/new library artifacts in separate
processes. A kernel gain cannot waive public/durable/legacy adoption gates.

`public_measure.py` measures actual `AgentExecution.event_page()` with identical
seeded list/native event windows and an exact per-page oracle. Its independent
memory workers include fixture/oracle residency. It does not time producers,
HTTP routes/SSE or durable recovery. Run the two-sample smoke before full pairs
and keep `check_public.py` as the neighboring route/lifecycle parity check.

`public_tail.py` binds a private per-execution reader; its check covers the
projection/cursor boundary and membership changes. `public_tail_measure.py`
derives its worker/driver from `public_measure.py` with asserted seams. Run the
same smoke/full ladder and preserve the unadapted public-reader measurements.
`check_public_tail.py` reuses actual Headless route and observer tests with a
private constructor binding all three public read ports; real TCP and durable
recovery remain separate gates.

`native_reader.py` owns the lightweight view/canonical-helper adapter shared by
the private producer and public ports. Public binding must not import/generated
TaskRuntime merely to read a window. Verify runtime/fault/public semantic checks
and the paired public measurement when changing this shared adapter.

`bounds_reader.py` uses one native `bounds()` tuple per locked read; metadata
views must not survive across producer mutation or retain evicted first events.
Verify `check_bounds.py`, retention/runtime/fault neighbors and then
`bounds_measure.py` small/full cohorts with a separately built library. Freeze
Rust, adapters, driver and native artifact together while measurement is live.

`chunked_window.py` / `chunked_runtime.py` are a Python retention experiment.
The first consumer converts once to canonical lists; selection happens under
the existing lock and publication callbacks retain their original placement.
`check_chunked.py` covers newest-valid-event byte exceptions, immediate eviction,
snapshots/GC, concurrent conversion and callbacks waiting for another reader.
`chunked_measure.py` records cold conversion separately from steady append/poll;
its tail witness must not change the layout of write-only cohorts. It requires
no native library and never authorizes application/durable/public promotion.

`layout_crossover_measure.py` probes intermediate capacities with the unchanged
write-only worker before proposing a layout threshold. Keep all failing/tiny
capacities in the report; a crossover is not mixed-workload admission.

`demand_runtime.py` uses the observed 1024 crossover without restricting resource
capacity: small capacities use original methods; large histories migrate only
when retained population and imminent eviction justify it. Readers permanently
select original lists. Verify `check_demand.py` and then `demand_measure.py`,
including growth/mixed cohorts; never nest the non-reentrant event lock around
conversion or replay helpers, and preserve callbacks outside that lock.

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
