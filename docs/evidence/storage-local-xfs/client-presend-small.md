# Common client pre-send localization

The original 32-event observation below did **not** reproduce the slow tail and
did not justify a runtime patch. Its sanitized archive is unchanged. The
[current nonempty-source follow-up](#nonempty-source-discriminator-and-one-call-local-improvement)
adds a separate, deterministic duplicate-scan proof and one call-local change;
it does not reinterpret historical timings as causal evidence.

## Original empty-source observation (no runtime patch at that stage)

## Scope and source

The prior probe recorded checkout SHA `4b58a83448eccb9308d6f915be99fae98cd5df9d`
and eight source blob identities in `source.txt`. All eight match that commit
and this repair checkout (see archived `source-check.json`). Eight matching
blobs do **not** independently establish whole-tree execution provenance,
checkout cleanliness, untracked inputs, or the original environment.
No runtime code changed. No native runtime or long-experiment path was modified;
no production authority, DolphinFS workload, restart, or new scale leg was run.
The only new workload was a private local-XFS SQLite authority with synthetic
normal-size events. The shared checkout was read, never written.

Against retained `a2fbe5f74d8fdff5eb9cf149b93bb4436505ac98`, the reviewed
`turn_lifecycle.py`, `protocol.py`, `session_pool.py`, `credential_redaction.py`,
and `storage_json.py` blobs are unchanged. `client.py` has the existing
`088bd86ae2` extraction of `_request_frame` and separate `check_command_frame`
preflight; the normal event path does not call that preflight. The archive
commit is not an ancestor of current HEAD; comparisons are explicit blob diffs,
not an assumption that its benchmark additions are current runtime code.

Both prior archive manifests passed. Replaying the original join summary
recovered 4,000 dispatch/dequeue/response joins, 3,963 commit joins, and 37
unresolved commit paths. In the same slowest-39 commit-joined cohort,
`before_send.total_ms / count = 103189.914171 / 39 = 2645.895235153846 ms`.
This is a **mean**, not a maximum. The selected-39 pre-send maximum is
3496.133129 ms; the full 3,963-event commit-subset pre-send maximum is
3583.036834 ms. No new cause follows. This package-only repair recomputed these
values from retained `join-replayed.json`, not by rerunning the joined workload.

## Exact path and seam

At the reviewed source:

- `lib/turn_lifecycle.py:2509` / `:2549`: `record_task_event` enters
  `_task_projection_critical_section`; `:2420` owns the task-local RLock.
  `:2430` also defers commit notices until after the lock is released.
- `:2660`: `_record_task_event_locked`; `:2710` reads the committed cached
  projection. A cold baseline performs the distinct attempt and Turn queries
  at `:2720` and `:2724`; they are not two reads of the same object.
- `:2751` and `:2851`: projection and revision patch preparation.
  `:2870` evaluates `_CommandEncoding.frozen` before entering `client.command`.
- `lib/storage/client.py:583`: `command` performs credential redaction, then
  `:258` `_call` -> `:365` `_attempt` -> `:346` `_request_frame`.
  `lib/storage/protocol.py:75` materializes the fragment-bearing wire body.
- `lib/storage/client.py:447`: `_pooled_exchange` acquires a session, sets its
  timeout, and reaches `send_frame` at `:452`.

The retained collector starts immediately before the public
`record_task_event` call and stops immediately before the original target
`client.send_frame` function. It includes lock acquisition, cold-state reads,
projection/patch construction, value freezing, redaction, request construction,
and pool acquisition, plus scheduling during those operations. Earlier query
RPCs, if present, fall inside this composite interval. They must be separated
rather than interpreted as preparation CPU.

The target frame's final `encode_frame` and `sendall` are **after** that stop
(`lib/storage/protocol.py:184`). Its wire, server dispatch, writer queue,
commit, response, and post-ACK notices are not pre-send work. However value
encoding during `frozen` is **before** the stop; excluding all serialization
would also be wrong. `_CommandEncoding` memoizes container encodings by identity
(`lib/turn_lifecycle.py:1988`); `FrozenPayload.wire_body` splices fragments
(`lib/storage/protocol.py:68`) rather than encoding those containers again.
Redaction has a different security responsibility; changed values can discard
the frozen representation. No safe redundant operation is established here.

## Small matched observation

Archived `probe.py` uses the existing disposable-authority and running-task test
helpers. Four arms, control/trace/trace/control, each have two tasks and four
phase events per task; cumulative text grows by 432 characters per event.
There is no artificial contention, provider, model, restart, or scaled fan-out.
The initial event has cold projection state; subsequent events use the real
committed cache. Tracing wraps the actual task RLock's acquisition, preparation,
freeze, redaction, request construction, pool acquisition and target send entry.
It records no frame payload or credential. Credential sources and source-identity
inputs are synthetic empty values: this does **not** exercise real vault stat,
credential rebuild, nonempty secret scans, or their contention.

All 32 events returned `carried`. Every arm recovered exact cumulative content,
FIFO sequences 0..3, exact schema-v1 phase rows, and equal projection revision
and work digests: `956462f243d4391c95b967dfa7f6fb115d0888d374745403f0a2c8fc9ebb7af5`.
Integrity returned `{ok: true, result: ok}`. Sixteen events were traced.

| Mean per traced event | Cold (4) | Warm (12) |
|---|---:|---:|
| Entry -> target send entry, wall ms | 0.617264 | 0.181044 |
| Calling-thread CPU within that seam, ms | 0.385568 | 0.179945 |
| Wall minus calling-thread CPU, ms | 0.231697 | 0.001099 |
| Actual projection-lock acquisition, wall ms | 0.000888 | 0.000709 |
| Projection, wall ms | 0.059408 | 0.056498 |
| Patch construction, wall ms | 0.023353 | 0.025148 |
| Freeze/value encoding, wall ms | 0.038468 | 0.030348 |
| Redaction including redactor lookup, wall ms | 0.007430 | 0.007616 |
| Pool acquisition (all calls before stop), wall ms | 0.022408 | 0.008369 |
| Earlier completed RPCs | 2 | 0 |
| Target event sends | 1 | 1 |

Cold RPC order was `turn.attempt.get`, `turn.get`, `turn.event.record`; warm
order was only `turn.event.record`. Each event had one projection, patch,
freeze, redaction, and actual projection-lock acquisition. Nested spans are
**not** an additive partition (e.g. redactor lookup is inside redaction).
Wall minus caller CPU includes earlier RPC waits and scheduling; it is not a
measurement of a particular blocking owner. CPU samples lie inside the wall
seam; instrumentation remains part of these very small times.

Whole-arm wall ms were 26.236395 / 7.411985 / 18.928480 / 8.211447; caller CPU ms
were 18.894090 / 2.410060 / 2.847460 / 2.338220. They are retained control
observations, not a trace-overhead estimate, optimization baseline, or backend
comparison: warmup/order and shared-host scheduling differ. The slowest-39
2,645.90 ms pre-send mean was **not reproduced**. The earlier CPU trace's slowest-40 population is not the
join archive's slowest-39, so its off-CPU numbers cannot be subtracted here.

Three unchanged neighboring specifications passed in 3.68 s: carried row plus
projection atomicity, conflicting-row whole-frame rollback, and suffix-only
text replay, in `tests/turn_lifecycle/test_turn_event_carried_task_event.py`.
No patch means no new cancellation/error behavior was introduced; this probe
is not a new cancellation or process-crash durability certification.

Probe-development receipts: the first readback assertion omitted the existing
stored-event `v:1` field; diagnostic readback showed it and the assertion was
corrected. A preliminary CPU subtraction used non-nested clock sample endpoints
and produced tiny negative residuals; the final collector samples CPU inside
the wall seam. Neither preliminary observation is used for the table.

## Decision and next discriminating evidence

**No structural duplicate proven; no speculative product edit.** One target
send is not evidence that every preparation traversal is optimal, but removing
freeze, redaction, liveness queries, or locking would require a behavioral proof
that these observations do not supply.

Next observation, still small and isolated, should join the **same event IDs**
and target request IDs to disjoint pre-send stages: actual projection-lock
acquisition; cold-state queries; projection; patch; freeze; redactor
source-identity/cache-hit/rebuild-lock work; request construction; pool checkout.
Retain wall and caller CPU at each boundary. Record lock acquisition and owner
hold/release separately; for residual off-CPU gaps collect only the owned
caller's TID scheduler/blocking evidence (e.g. schedstat deltas plus stacks),
so explicit lock wait is distinguished from runnable scheduling/GIL delay.
A signed wall-minus-CPU residual alone is not a lock attribution. Include a
small synthetic nonempty credential case and cache invalidation without reading
live secrets. Stop before original target `send_frame`; keep earlier query
transport separate. No change is justified until a repeated operation on the
same immutable input is demonstrated unnecessary while preserving failure,
identity, FIFO, cancellation and durable-before-visible boundaries.

## Sanitized deterministic audit package

[Sanitized synthetic receipts and original executable probe](client-presend-small-original.tar.gz)
(SHA-256 `4b0d535cbb2510240390b360444dc5d8afefc7b4540502dcbb1e8cf6d9f0a08e`)
replace the earlier unsanitized archive in place; the historical filename is
retained, but the package is explicitly **sanitized**, not raw. `SANITIZED.txt`
and the `sanitized` field in `receipt.json` describe the transformation. The receipt's absolute
module path becomes `lib/__init__.py`; its machine PID is removed. Arrays,
event/request identities, ordering, all numeric measurements and integrity
results are unchanged. `sanitize.py` pins the old raw receipt hash and asserts
value equality of `scope`, `arms`, `rows`, and `integrity` before writing.
The raw receipt is not republished. The original probe source remains unchanged;
running it would create a new raw receipt. No probe or runtime test was run in
this repair; the three-test receipt above belongs to the original stage.

The tar uses sorted member names, uid/gid/mtime 0, empty owner/group names,
0644 modes, no extra headers, and gzip filename empty/mtime 0. Two independent
pack invocations produced identical bytes in this Python/zlib environment.
`MANIFEST.sha256` was regenerated and covers every member except itself.

After extraction, verify the manifest and run only the arithmetic tools:
`.venv/bin/python3 summarize.py <extracted>` and
`.venv/bin/python3 check_arithmetic.py <extracted>`.
The separately implemented accumulator check imports neither the probe nor
`summarize.py`; it checks all small-table values to 1e-9, 4 cold/12 warm rows,
32 arm events and equal work digests, plus the distinct join mean/max values.
The shipped summarizer reproduces the unchanged `summary.json` exactly as JSON.

A **pattern-only** scan of the explicit publishable data/source member allowlist
returned zero matches for absolute machine paths, prior workspace/user
identifiers, credential assignments/token signatures, and unexpected JSON
payload/content/message fields. `pattern-scan.json` records patterns and counts
only, never matched values. This is not semantic privacy certification or a
comparison against live credentials. No raw receipt, database, log, or original
joined event archive is included. This repair changes only this report and its
archive; runtime behavior and the no-duplicate/no-patch conclusion are unchanged.

Independent read-only package audit `af2874c1` passed: type-sensitive recursive
comparison against checkpoint `13ed41d0e3a7` preserved all measurement subtrees,
all table fields were independently recomputed, 11/11 manifest entries verified,
and two memory-only repacks reproduced the published bytes. Joined maxima and
cohort membership remain retained summary facts, not independently regenerated
from raw joined events during this repair. Full own-change inventory was checked
(including staged, unstaged and untracked paths): only the report and archive
above. A notice naming this conversation's old worktree was checked against the
prior checkpoint; its report bytes matched the old package, not a competing edit.


## Nonempty-source discriminator and one call-local improvement

### Current path and reproducible fixture

The follow-up started at checkout `7db0ad86ed4efb5f46c550eded90e89dab1da760`.
The exact original redactor Git blob is
`3bc42a9a35b9474fd29a0068122b36c88613c301`; the changed blob is
`95c5abd13682d30bccd922d3b37a6f3e41ede128`. Unchanged path-owner blobs:

| Owner | Git blob |
|---|---|
| `lib/tasks_pkg/manager/_events.py` | `601eb7f670cc66e0f3d76d9d5359a1032f8f8ae9` |
| `lib/turn_lifecycle.py` | `6e7e06e8112d854683b5eb7ac6ea1d1e9042e529` |
| `lib/storage/client.py` | `49c8f4ba13edab5137c42f5ae2fe7e9278787e06` |
| `lib/storage/protocol.py` | `7efb4f47bb05838a9bc8de1883fe462dba93a655` |

Executable observation:
`tests/turn_lifecycle/test_client_presend_sources.py`. Run from the checkout:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q -s --tb=short \
  tests/turn_lifecycle/test_client_presend_sources.py
```

Two disposable SQLite arms each use one task and six small phase events:
cold cache, hit, vault replacement, config replacement, model-routing secret
replacement, hit. There is no concurrent producer or artificial contention.
The fixture generates nonempty disposable values and a disposable encryption
key in memory, uses its own encrypted vault/key paths and config directory,
and writes model-routing secrets through the real Sidecar-backed repository.
Model-routing reads/decryption and the real repository's atomic stamp
invalidation are not replaced. Config replacement also changes its inode.
Temporary storage is local XFS (confirmed by `stat -f` on task scratch).
No real credential source, production connection, production authority or
live log was read. Only the selected disposable SQLite process is reopened.

The production `_advance_phase_snapshot` copies the phase detail string by
reference into the task's phase snapshot. `record_task_event` then carries
that snapshot in `event_payload.phase` and the original immutable detail in
`task_event.event`. This is ordinary producer behavior, not an artificially
aliased container graph. Each detail contains the raw and both URL-encoded
forms of all three synthetic sources. The task's ordinary content grows by
400 characters per event. Both arms have the same independent expected
redacted rows and cumulative-content oracle; generated input values are not
published. The previous arm retains the original recursive `_walk` logic in
`_uncached_walk`; it changes no source/cache/transport behavior.

### Deterministic result and exact removed work

Before changing runtime code, the real six-event test with the one-scan
assertion failed on its first event: `assert 2 == 1`. After the change, the
paired test passed both arms with these exact per-event counts:

| State | Detail scans, previous / current | Source identity / stats | Rebuild / rebuild lock | Earlier projection / credential RPCs | Target send entries |
|---|---:|---:|---:|---:|---:|
| cold | 2 / 1 | 1 / 3 | 1 / 1 | 2 / 2 | 1 |
| hit | 2 / 1 | 1 / 3 | 0 / 0 | 0 / 0 | 1 |
| vault replacement | 2 / 1 | 1 / 3 | 1 / 1 | 0 / 2 | 1 |
| config replacement | 2 / 1 | 1 / 3 | 1 / 1 | 0 / 2 | 1 |
| model secret replacement | 2 / 1 | 1 / 3 | 1 / 1 | 0 / 2 | 1 |
| hit | 2 / 1 | 1 / 3 | 0 / 0 | 0 / 0 | 1 |

Thus **six redundant scans of the same immutable detail are removed**:
12 scans become 6 across six frames. Each frame still has one projection-lock
acquisition, projection, patch, freeze, redactor lookup and redaction entry.
The three stats are distinct vault/config/owner-stamp checks, not duplicates.
A cache hit acquires no rebuild lock; a changed identity acquires it once.
Its under-lock cache double-check remains necessary and unchanged.

Earlier cold projection queries are `turn.attempt.get` and `turn.get`.
Credential rebuild queries are `model_routing.get` and
`model_routing.secret.get`, including on warm projection state. All are
completed before the target `turn.event.record` send entry. Per-event receipts
join the fixed event sequence to the actual target request ID and RPC order.
Observation rows are thread-local: only the thread that activates a row can
see it. This applies uniformly to spans, RPC order, string-scan counts, lock
releases and the target-send stop, including CPU sampling. Unrelated supervisor
health probes still execute normally but cannot see or modify that row.
Every span retains wall and calling-thread CPU. Lock acquisition is observed
around the real lock; owner release is recorded separately: rebuild lock is
released before target send, while the projection lock is released after it.
Projection hold clocks are clipped to the pre-send boundary, not represented
as the full owner hold through ACK.

The first follow-up collector used a process-shared active row. Independent
review found an intermittent `system.health` in its exact event RPC order,
and a deterministic unrelated-thread span counterexample. That collector's
numeric clock observations are withdrawn here: they cannot certify exclusively
calling-thread CPU or event work. The runtime duplicate-scan finding was
independently reproduced and is unchanged. The repaired collector's controlled
concurrent-health regression first failed with extra request/pool/RPC spans;
with thread-local rows it requires only the event caller's span, no foreign
RPC/stop fields, and a successful real health response. Supervisor liveness
checks are not disabled or filtered by operation name.

Current clocks remain instrumented host observations, not a latency gain or
slow-tail reproduction. Different random synthetic values, process warmup and
nested spans preclude causal speed ratios. Credential RPCs are inside
rebuild/redaction, request/pool inside RPC; do not sum them.
No wall-minus-CPU residual is attributed to a lock, GIL or filesystem, and no
scheduler/stack collection is needed to establish the deterministic duplicate.

The stop is immediately before original target `send_frame`: final frame
encoding, socket send, target writer work, commit and ACK are excluded.
The unchanged freeze happens before that stop. Discarding a frozen payload
when redaction changes a value can cause later encoding; that is not the
pre-send duplicate admitted here and was not optimized.

### Change, guarantees, verification and stop condition

The only runtime improvement is in `KnownCredentialRedactor.redact_value` /
`_walk`: reuse the result for an identical exact built-in string within one
walk. Its local identity table holds the original input to prevent object-ID
reuse and disappears when that call returns. Nothing is stored on the
redactor or in a process/request/source cache. Strings are immutable, and all
reuses use the same already-selected redactor snapshot. Containers still
traverse normally, subclasses keep their original calls, and a later value
walk always starts with a fresh table. No credential read, source identity,
cache freshness, lock, redaction rule, encoded fragment, receipt, retry,
FIFO, write fencing or durability policy changed. There is no new setting,
threshold, log or production instrumentation.

The paired test independently requires all nine credential forms to become
the expected labels in the actual target frame, unchanged caller input,
`carried` returns, exact FIFO 0..5 schema-v1 event rows and exact cumulative
projection after a clean authority stop/start, plus integrity success. This
is clean-reopen persistence, not a new power-loss/SIGKILL certification.
Added focused unit cases cover matched/no-match alias reuse, unchanged-object
identity, caller/container mutation, string-subclass behavior, equal but
distinct strings, the same input under different redactor snapshots, and a
failed walk followed by retry with no retained memo.
Existing source-rotation, owner/tenant/request authority, source-error/retry,
streaming/alignment, no-value-logging, conflicting-row whole-frame rollback,
suffix replay and carried-event specifications remain in the neighboring
files and were rerun:

```sh
PYTHONPATH=$PWD make test TESTS="tests/credentials_vault/test_known_credential_redaction.py tests/credentials_vault/test_model_routing_credential_redaction.py tests/turn_lifecycle/test_turn_event_carried_task_event.py tests/turn_lifecycle/test_client_presend_sources.py"
```

After the thread-isolation repair, the complete collector file passed 3/3
(including both six-event arms and the controlled concurrent-health case).
The four-file neighboring verification passed again, zero reused and zero
incomplete, tree `53bc8cd5574b`. The health regression failed red before the
repair with foreign `request`, `pool` and `rpc` spans; after repair it confirms
that the real health operation returns ready while the event receipt contains
only `event_caller`, with no foreign RPC or stop timestamp.

Earlier result: neighboring verification passed, four files executed, zero reused,
zero incomplete (tree `a54771beee3e`, after merging the shared branch and adding
all six focused unit cases). A final fixture-only change generated the
subclass test's synthetic key dynamically too; that file passed again at tree
`175d590b7d4c`. The smaller paired observation separately passed both arms. Independent static review `32d4aa89` found no blocker, confirmed the
frozen previous walk and boundary/source isolation, and emphasized that the
memo adds O(unique exact strings) transient entries: this is a redundant-work
proof, not a demonstrated net application-latency gain.

**Stop here:** one structural improvement is demonstrated. No remaining
source-identity/cache/rebuild-lock duplicate was found, and the original slow
tail is still not reproduced. Do not remove freshness checks, skip rebuilds,
relax projection locking, chase the off-CPU residual, or run larger profiling
without a new same-event structural contradiction. This result is separate
from `db-fair-operation-logical-results` and native GC; it makes no DolphinFS,
native-engine, production-load, scale or external-job performance inference.
