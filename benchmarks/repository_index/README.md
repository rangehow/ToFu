# Incremental extraction and native graph candidates

The [native tag candidate](native_extraction/README.md) moves canonical
Tree-sitter cursor traversal into the exact upstream Rust grammar registry.
Its executable accepts explicit grammar and wire-budget inputs, performs no
runtime downloads, and retains per-file errors. `check_native_extraction.py`
compares exact Python Tag values; `check_native_extraction_tool.py` covers
canonical transient publication and source-admission/error semantics. The
adapter still loads the Python catalog and retains a batch of source text;
kernel correctness alone does not admit memory, cold latency or a default.
The explicit `native_extraction_library.py` adapter uses one coarse C call per
batch to investigate process costs. Its concurrent/buffer checks share the same
real-tool oracle; grammar lifetime, hard cancellation and default admission
remain separate requirements.
The explicit v2 `NativeCatalogBatchScanner` moves language lookup into Rust and
uses a generated admission-key set. Both derive from one pinned upstream source;
the catalog checker verifies exact lookup/admission behavior and a fresh scan
with Python parser imports forbidden. This does not select a product default.
`native_extraction_stream.py` combines Git discovery, reads and native extraction
in one sealed, joined child and consumes one file frame at a time. The
[native README](native_extraction/README.md) owns its explicit budgets, framing,
text compatibility and cancellation scope; `test_native_extraction_stream.py`
checks those boundaries before real-tool measurements.

**Problem:** the Rustworkx candidate differs from canonical PageRank scores at
the declared precision, so it cannot authorize a backend switch. **Decision:**
`native_csr_kernel.rs` implements the canonical weighted CSR power iteration,
including uniform initialization, personalized dangling redistribution and the
node-scaled stopping criterion. `native_csr.py` loads only an explicitly owned,
digest-verified artifact; no production path or dependency default changes.
**Alternatives considered:** relaxing score precision risks changing selected
files; direct SciPy removes graph objects but retains its larger import/runtime
footprint. Both alternatives remain separately measured.

Compile with `rustc --edition=2021 --crate-type cdylib -C opt-level=3
benchmarks/repository_index/native_csr_kernel.rs -o /tmp/libtofu_repo_csr.so`.
Run `check_native_csr.py --library /tmp/libtofu_repo_csr.so` for 300 independent
weighted/dangling/personalized graphs, concurrent calls, invalid buffers and
failed-call output withholding. `native_repo_rank.py` supplies the repository
adapter, checked against the 300-case `check_rank.py` oracle. Run
`sparse_rank_measure.py --backend native --library /tmp/libtofu_repo_csr.so
--samples 2 --pairs 1 --output /tmp/native-rank-pilot.json` before the default
100-sample/three-pair comparison. Baseline ranking is frozen at the recorded Git
revision; both arms use actual public scan/rank/render. Every memory worker runs
the full cohort. Cold first-call diagnostics include lazy kernel loading, but
they are not repeated independent cold-operation admission. HTTP, durable task
publication, live model inference and production packaging remain outside scope.

The full Rust comparison retains 1,800 wall/CPU observations and 36 independent
latency/memory processes. P50 ratios are 0.812–0.827 (small), 0.866–0.923 (dense)
and 0.894–0.938 (multilingual); peak RSS ratios are 0.486–0.506. Every observed
latency/CPU/peak/retention guard passes, with exact generated output traces.
`system_repository_native_csr_20261008.json`
does not supply production packaging or durable task-publication admission.
The separately measured direct SciPy candidate fails two small-case retention
pairs; keep its `system_repository_sparse_ranking_20261008.json`.

The extraction candidate reads every selected source and hashes its complete
normalized text. It never trusts mtime/size as a freshness witness. Admission and
reference mode are part of the key; deletions and failed reads discard affected
entries. Fresh result containers and Tag values protect cached derivations from
caller mutation. The owner/root-scoped LRU charges retained objects conservatively
and clears explicitly. Empty non-Python results are not cached because they may
represent a missing grammar. Dynamic grammar/extractor changes still require a
production epoch/invalidation design before adoption.

The experiment now accepts an explicit nonnegative `extractor_epoch` on scans;
an epoch change clears old derivations before extraction. Foreign owners cannot
invalidate entries. A per-index lock serializes scans, eviction and clearing,
so concurrent calls cannot corrupt accounting and `close()` waits for active
scans before clearing. Its experimental close operation is a reusable reset,
not permanent owner shutdown. Tests cover changed extraction on unchanged source,
invalid/foreign epoch requests, 32 concurrent scans and clearing during a blocked
scan. Production still needs an authoritative producer for epoch changes and an
application-owned terminal lifecycle; this argument alone does not supply either.

Snapshot construction uses a standard-library attribute getter derived from
the canonical dataclass fields. It preserves all field values and conservative
object charging while avoiding a Python generator for every tag's fields.
Returned Tag objects remain detached from frozen cached values, including when
a Python caller mutates a frozen dataclass through its instance dictionary.

**Problem:** the full quota/session-owned tuple-cache comparison exceeds the
original cold RSS gate despite strong warm gains. **Decision:** the next
experimental representation stores tags and identifiers in one immutable JSON
byte payload per file, then restores fresh objects on a hit. ASCII escaping
preserves surrogate values; builtin JSON preserves arbitrary-size integers.
Charge includes the actual payload/entry/digest/key sizes plus conservative LRU
metadata. **Alternatives considered:** a smaller fixed cache would trade away
machine-scaled reuse; global string interning would escape owner reclamation;
fixed-width codecs risk numeric changes. The prior tuple result remains rejected
evidence, and this representation needs its own full scoped measurement.
`check_packed.py` covers all captured fields, Unicode/surrogate values, large
integers, detached restores, canonical path admission and charging. Its synthetic
charged-byte ratio is not process RSS or end-to-end performance evidence.
The full tuple predecessor's 1,800 observations/36 independent processes,
guard regressions and original artifact identity are retained in
`system_candidate_rejections_20261008.json`.
The packed successor completes the same full scoped comparison: 1,800 raw
observations and 36 independent latency/memory processes, exact output traces
and all measured original guards passing. Cold P50 ratios are 1.022–1.037,
RSS 1.004–1.009; warm/edited P50 ratios are 0.118–0.123. Retained
`system_repository_packed_20261008.json`
does not supply real task registry/epoch coordination, larger-project/agent
frequency, global retained-memory or whole-application recovery admission.

`owned_scope.py` adds an experimental terminal owner around the reusable index.
Its required epoch callback is the only version authority; callers cannot pass
a competing epoch. Foreign owners cannot read, clear or invoke that callback.
Closing joins in-flight scans, rejects future scans and permits repeated close.
Cleanup faults remain visible and can be retried without reopening admission;
context-manager cleanup preserves an existing caller exception. `check_scope.py`
exercises blocked/concurrent close, callback ownership, version changes,
exception exit and injected cleanup failures. This is not an application task
resource store; production lifetime registration and sizing still need admission.

**Problem:** reentrant close from an epoch callback can report completion and
then let that same scan repopulate the supposedly closed cache. **Decision:**
the scope rejects close and recursive scan on its active scan thread; other
closers wait for quiescence. **Alternatives considered:** waiting on the same
thread deadlocks; returning early violates terminal cleanup. Fault tests also
verify retry after cleanup failure and preservation of the primary caller error.

`check_session.py` binds the fixture cache as a component of one actual
`ExecutionSession` tool environment. Success/failure/cancel/timeout settlement
releases it exactly once, and acquire-to-bind failure rolls it back. It does not
add a duplicate environment resource or install a registry in durable task state.
Production must compose with the environment's existing release, not replace it.

`check_environment.py` exercises that composition with real custom-tool
environment mint/dispose and client result waiters. Four terminal outcomes
unblock this environment's waiter before joining a blocked cache scan, while a
neighbor with the same call ID remains available. Quota stays reserved until
the scan exits. One execution resource releases both components; registration
rollback and single/dual cleanup failures preserve primary errors and permit
cache cleanup retry. This remains an offline composition experiment, not route
integration, a new task registry or throughput evidence.
**Problem:** joining the cache first can delay cancellation of client handoffs.
**Decision:** stop environment waiters before joining cache scans and attempt
both cleanup steps even if one fails. **Alternatives considered:** two bindings
conflict with the existing environment resource; sequential cleanup without
failure isolation can leak the second component.

`budget.py` proposes cache bytes from a supplied shared launch probe: total
reconstructible cache stays below 1/64 of physical/cgroup capacity and 1/16 of
observed headroom, then divides by the explicit live-scope limit. No fixed
high-end ceiling is added. Missing telemetry falls back lean; zero headroom
disables cache; a positive operator override stays explicit. `check_budget.py`
records the real shared probe and scale-up/down/override checks. This proposal
does not enforce global reservations and has not been adopted as a default.
**Alternatives considered:** a fixed small cache wastes larger hosts; an
unbounded per-task cache multiplies resident growth across active executions.

`quota.py` is a separately constructed reservation pool for that plan, with
explicit owner/execution identity. It reserves before scope construction and
returns quota only after owned scope cleanup succeeds. No module-global pool
or durable-task field is added. Duplicate identities, foreign releases and
stale handles after identity reuse cannot change another reservation. Stopping
admission does not pretend active leases have been reclaimed.
`check_quota.py` checks 16 concurrent contenders for four slots, constructor
rollback, blocked close, cleanup failure retention/retry and actual execution
settlement/bind rollback. Production registration, authoritative epoch update
coordination and full scoped-path performance remain required before adoption.

`scoped_measure.py --output /tmp/tofu-scoped-index.json` measures the actual
execution-session, quota-reservation and terminal-scope composition on the same
frozen real-source corpus. One parent shared launch probe determines the plan
passed to every worker; no worker reprobes to choose its own budget. Both arms
own an execution session and tool-environment binding. Cold operations create
and settle that execution inside timing; warm/edited operations reuse it and
verify final release. Exhausted/disabled quota uses the canonical scanner.
Three alternating pairs with 100 observations per arm cover cold, warm and
edited input; independent memory workers retain source/probe/budget identities,
raw timing and exact output digests. Charged cache peak and fallback counts are
reported; imports, fixture copy and oracle work remain in process high-water RSS.
This is the full experimental ownership path, not deployed task integration or
concurrent multi-execution throughput admission. `--samples 2 --pairs 1` is smoke
only. Scan failures retain their primary error while failed cleanup is reported.

With the selected Python environment and NetworkX, SciPy and the grammar pack:

```bash
python benchmarks/repository_index/check.py
export TOFU_BENCHMARK_PYTHON="$VIRTUAL_ENV/bin/python"
export TOFU_REPOSITORY_INDEX_OUTPUT=/tmp/tofu-repository-index.json
python benchmarks/repository_index/measure.py
```

The real scan/PageRank/render comparison uses 32 synthetic files, three alternating
fresh-process pairs, 100 observations after 20 warmups, separate RSS workers and
a digest of every rendered result. Workloads include Python repeated scans,
single-file edits, Python/JavaScript/Rust scans and repeatedly empty caches.
This does not prove performance on enterprise-sized repositories or agent runs.
Recorded cache gains do not waive the observed cold-scan CPU regression.

`real_measure.py --output /tmp/tofu-real-repository-index.json` repeats the
comparison on a frozen copy of tracked Tofu sources under `lib/repo_map`,
`lib/knowledge` and `frontend/src/runtime/sections`. Every file has a retained
content digest. Each subprocess owns its own copy; the edited case appends a
new definition to that copy of `outline.py`. It checks exact scan containers,
PageRank scores and rendered output before measurement, and compares the digest
of every timed output across variants. The default is three alternating pairs
with 100 observations after 20 warmups for empty-cache, repeated and edited
scans. Separate memory workers report process high-water RSS including imports,
fixture copies and oracle checks; this is not application retained memory.
The first-scan diagnostic follows oracle checks and is an empty-cache diagnostic,
not fresh-interpreter startup. `--samples 2 --pairs 1` is a bounded smoke check
and never satisfies the performance admission contract. Use the compatible
Python wrapper through `TOFU_BENCHMARK_PYTHON` when subprocesses require it.

`direct_tags.py` selects the production extraction callable for comparison. It uses the same installed
language resolver and tree-sitter parser, traverses with a cursor, retains only
accepted tags and sorts stably by starting line. It omits `TreeContext`'s grep
display scopes, headers and per-line lists of every syntax node. It preserves
tag names, kinds, signatures, ancestor depth and line/preorder ordering.
`check_direct_tags.py` checks real sources plus deterministic malformed fixtures
in eight languages and parser-failure/unknown-language fallback. The candidate
also handles a 1,200-block nested JavaScript fixture for which the reference
returns no tags after recursive traversal failure. The owning tools document
records the decision to keep valid definitions; production tests cover that
behavior, file-local failures and concurrent project isolation.

Use `real_measure.py --candidate direct --case cold --output /tmp/tofu-direct-tags.json`
for three paired, 100-observation complete scan/rank/render comparisons on the
same corpus. Both variants fully extract every operation; this isolates the
traversal change without an extraction cache. Each candidate subprocess checks
exact ordered extraction against the unpatched reference on its snapshot before
installing its process-local experiment seam. Paired rendered-output digests
still must match. This does not replace the cache experiment or prove gains in
actual agent execution.

`reference_tags.py` compiles only the original extraction function from retained
Git ancestor `e9fee0085`, with a recorded function digest. The current Tag model
and scanner owners are shared, while the traversal oracle stays immutable after
promotion. Missing Git evidence fails explicitly; it never falls back to the
live candidate. `check_direct_tags.py` uses this oracle.

Measured extraction replacements are plain functions (`patch(..., new=...)`),
with no Mock call recording. An earlier `side_effect` replacement retained
complete source arguments and inflated the separate memory worker to about
3.47 times the reference; those observations are invalid for admission.
Corrected runs retain fresh latency and memory process IDs, derive P95/P99 with
the system contract's `floor((n - 1) * percentile)` index and checkpoint each
completed pair. Candidate parse/setup/traversal failures stay file-local,
including a parser returning an invalid result.

Rustworkx is optional and belongs only to the isolated experiment environment.
`python benchmarks/repository_index/check_rank.py` compares 300 generated weighted
graphs/focus choices with the canonical defaults at a declared stringent precision.
That check currently stops on a score difference; matching graph semantics does
not imply identical convergence or rendered file selection. See the
[official PageRank API](https://www.rustworkx.org/apiref/rustworkx.pagerank.html).
No graph backend or application dependency is changed by this experiment.

`tool_port.py` privately supplies the scope's scanner to actual repository API
and tool-handler functions. Each call carries the task's explicit owner; a
different root is rejected before scanning. Module globals and production task
state stay untouched. `check_tool_port.py` verifies actual text/metadata/settlement,
filters/budgets, read-only source, edits/epochs, foreign owners/roots and close.
`tool_measure.py --output /tmp/repository-tool.json` reuses full execution/quota
cold/warm/edited accounting with this actual handler/API path. Memory workers now
run all samples; worker-local template compilation occurs before measured calls.
`--case warm --samples 2 --pairs 1` is smoke only. The recording finalizer checks
one settlement but excludes durable result publication. Real registry/epoch
authority, parent environment binding and agent invocation frequency remain gaps.
**Problem:** duplicated benchmark scan/rank/render bypassed the actual consumer.
**Decision:** use the canonical API and handler with private extraction/finalizer
ports. **Alternatives:** globally patching the handler risks concurrent calls;
copying its ranking/result construction would create another behavior owner.

`task_port.py` replaces only the component port's ownership seam with canonical
`manager.task_user_id()` on actual TaskRuntime dictionaries. It validates the
structured principal and never creates a `user_id` alias. The earlier component
measurement used that fixture alias, so it cannot prove real task authorization.
`task_indexes.py` reserves one execution quota for explicitly authorized roots.
Each active scan leases the remaining capacity after idle retained data and
other active leases; unused authorized roots do not divide the budget. Same-root
calls serialize, different roots may overlap, and a zero lease uses canonical
uncached extraction. Root ports borrow read capability; only the group closes.
Terminal release stops admission/wakes queued calls, joins admitted scans,
attempts every root close and releases quota only after successful cleanup.
`check_task_indexes.py` uses actual task/principal and tool/API paths, two roots,
concurrent scanning and four terminal outcomes. `check_task_index_faults.py`
verifies binding rollback, cleanup-error precedence/retry and constructor reentry.
**Problem:** a one-root fixture and noncanonical owner field miss real execution
integration. **Decision:** one execution group with canonical identity and lazy
root leases. **Alternatives:** separate environment bindings duplicate lifecycle;
fixed shares across all authorized roots starve an active root while others idle.
These ports are still experimental: registry installation, real epochs/durable
finalization, inactive-root pressure policy, bounded cancellation and full
canonical-task performance remain unverified. Payload bounds are not total RSS.

The full component tool comparison
retains1,800 observations/36 independent latency/memory workers over179 frozen
real files, including full memory cohorts. Warm/edited P50 improves about88%;
cold rises1-3.5% and RSS stays within1.4%. Every measured guard passes. Its
fixture-only ownership and recording finalizer gaps remain explicit; the new
canonical-task/multi-root ports need their own full measurement and adoption.

`task_measure.py --output /tmp/canonical-repository-task.json` derives paired
accounting from the scoped worker with actual TaskRuntime dictionaries, canonical
principal validation and the task's own execution session. The kernel creates
that session; another session with its ID is rejected and must not be invented.
Cold includes kernel/task construction, group binding and terminal cleanup;
warm/edited calls reuse the same task. Terminalization enters `kernel.finish`,
which settles the one resource owner before checked quota/task teardown. Both
arms use the actual tool/API; memory runs the full latency cohort. The source
identity includes task/principal owners and the new adapters. The two-sample
warm CLI is smoke only; all three full cold/warm/edited pairs remain required.
**Problem:** old component timing bypassed canonical task identity/lifetime.
**Decision:** measure the actual producer and terminal owner on both arms.
**Alternative:** a second synthetic execution session duplicates authority and
the registry correctly refuses it. This default measures one root; concurrent
multi-root behavior, real epochs/registration and durable publication need
their own performance/integration evidence before production promotion.

`extraction_epoch.py` proposes a local witness for loaded extractor functions,
admission/reference/filter rules, active extension catalog, installed grammar
package versions and tag-field schema. It maps observed changes to monotonic
epochs and fails closed on schema changes that require rebuilding the cache
codec/owner. Missing distributions are recorded as missing, not invented.
`check_extraction_epoch.py` verifies code/rule/package changes invalidate cached
derivations on unchanged source, unchanged identities retain their epoch and
incompatible schema does not silently reuse old byte payloads.
**Problem:** a caller-supplied constant epoch misses changed extraction policy.
**Decision:** observe explicit loaded functions/rules and local package facts.
**Alternatives:** source mtime does not prove loaded code; default marshal-v3/v4
reference flags change with transient refcounts, causing false invalidation.
Marshal-v2 passes pre/post-execution stability checks. Function object IDs are
process-local and are never durable witnesses. Mutable closures, opaque/native
grammar state and concurrent reload still need authoritative producer/admission
coordination. Epoch-only timing is diagnostic; full cache integration, package
lookup cost and production registry remain unverified.

The canonical task comparison
verifies 1,800 observations/36 independent workers through actual task/session
creation, principal validation, tool/API output and kernel terminal cleanup.
Every measured guard passes; warm/edit P50 improves86-88%, cold stays within2.8%
and RSS within1.6%. This run still uses a constant epoch and one root, and its
recording finalizer excludes durable publication. The newer witness's query
cost and real registry/multi-root/agent frequency/recovery remain admission gaps.

`loaded_epoch_task_measure.py` measures the canonical task/tool comparison with
one `LoadedExtractionEpoch` per execution group, queried on every scan. It
includes loaded code/config/local-package checking in timed operations, rather
than assuming a constant epoch. Worker reports retain observed installed package
versions outside timing. Verify `check_loaded_epoch_task.py`, then run a small
`--samples 2 --pairs 1` CLI cohort before the full 100-sample/three-pair run.
Rule changes invalidate unchanged source; an epoch read fault preserves the
scanner error and the canonical tool error settlement, with terminal quota release.
Recording finalization, single-root fixtures, opaque grammar/reload authority and
actual registry remain gaps; including epoch cost does not authorize adoption.

**Fresh bootstrap decision — Problem:** eager loaded-witness queries regress
cold calls even when an execution never reuses its cache. **Decision:**
`bootstrap_epoch_task_measure.py` gives each fresh scope one canonical scan
without packing unused cache entries or querying loaded facts. Every later
scan of that scope observes actual loaded code/config/packages and uses epoch 1
or higher before cache admission/reuse. Roots share the monotonic witness, while
each scope independently starts witnessed caching on its second scan. Query
failure blocks reuse and preserves canonical error settlement; another root
shares the same monotonic witness. **Alternatives:** constant epochs miss rule
changes; reusing provisional entries before observation grants unproved freshness.
Both are rejected. Verify `check_bootstrap_epoch_task.py`, then a two-sample CLI
smoke and full pairs. This remains experimental; it installs no production
registry and does not close durable, grammar/reload or whole-agent coverage.

The full eager loaded-epoch task comparison verifies 1,800 timed observations
in 36 independent latency/RSS workers and exact tool/metadata traces. Warm/edit
P50 remains strongly lower, but cold P50 fails the 1.05 guard in two pairs and
cold P99 reaches 1.2305. The candidate is unpromoted;
`system_repository_loaded_epoch_20261008.json`
records raw/source/package facts and all pairs. Repeat-admission is a separate
experiment; it cannot erase this cold regression.

`public_tool_port.py` uses the actual project handler
`repository_scanner` keyword and public `generate_repo_map(scanner=...)`
composition. Extractor/API clones and rewritten handler imports are absent. Only
the recording publication function is privately bound in the handler namespace;
this still excludes durable publication and a production registry.
Verify `check_public_tool_port.py` for actual task identity, cold/warm/edit/rule
equality, root/owner denial, malformed principal rejection and terminal release.
`public_tool_measure.py` repeats canonical task comparisons with real loaded
epochs and lazy per-root admission; run its small CLI cohort before full pairs.

The full public-port comparison verifies 1,800 observations in 36 independent
latency/RSS workers. All measured cold/warm/edit P50/P95/P99/CPU/peakRSS guards
pass; warm/edit P50 falls about 87%. `system_repository_public_port_20261008.json`
records source/raw/package identities and per-pair ratios. Recording publication,
registry/default, larger/multi-root and whole-agent/whole30case coverage remain
gaps; no production cache or whole-domain admission is inferred.

**Codec admission decision — Problem:** a tag class can gain a field before a
fresh loaded witness first observes it, while an already-imported byte codec
still captures the older field list. **Decision:** admission, capture and restore
compare actual dataclass field order against the codec snapshot and fail closed
on mismatch. The first uncached scan still returns full canonical fields; no
partial cache payload is admitted. **Alternative rejected:** trusting only epoch
changes misses this initial mismatch and can silently replace provenance with
a field default. Verify `check_codec_admission.py`, packed/bootstrap/pressure
neighbors; old performance evidence retains its pre-guard source identities and
is not automatically evidence for this changed code. Reload fences remain a gap.

`check_multi_root_public.py` exercises three authorized roots in one real task
through the actual public handler/API ports: 48 calls on 8 threads match canonical
results/metadata under a 2 KiB retained-byte fixture quota and one execution lease.
A blocked first uncached scan keeps terminal release pending; close rejects new
root admission before joining the active scan and returning the reservation.
Recording publication remains excluded, and this semantic/concurrency check
does not establish multi-root latency/CPU/RSS or full-agent admission.

`multi_root_measure.py` measures three private copies of the frozen corpus in
one execution through actual public ports. Each operation calls all three roots;
100 observations therefore mean 300 tool calls per variant/case/pair. Cold
operations include task admission and terminal release; warm reuse and rotating
root edits share one execution reservation. Independent latency and full-cohort
RSS workers retain raw wall/CPU vectors, source witnesses and exact metadata
traces after 20 warmups. A small cohort verifies the driver only; recording
publication and full-agent admission remain excluded.

`publication_port.py` uses actual transient chat creation, built-in registry
lookup, the unchanged public handler and its canonical finalizer/event append.
No handler namespace is cloned. The adapter owns and removes only its created
tasks; `check_publication_port.py` verifies cold/warm/edit metadata equality,
one foreign-root error event and preservation of an unrelated runtime task.
`publication_measure.py` compares that path with independent full-cohort memory
workers; event buffers are retained for their actual task lifetime. This includes
transient registration/publication, not durable storage, gateway admission or
whole-agent/default cache installation. Production publication source witnesses
are checked before/after completed runs and added to the final report.

The formal post-codec-guard comparison retains 1,800 exact-output observations
in 36 independent workers. Warm/edit P50 gains persist, but cold pair 3 P99
ratio 1.0544 exceeds the unchanged 1.05 guard. `system_repository_codec_guard_20261008.json`
retains this failure; the changed configuration is not admitted by averaging
it with successful pairs. The canonical transient publication pilot verifies
three cases and 12 independent workers, with unique tool identities per port.
Adjacent event delivery tests use an explicit anchored clock and verify its
timestamp/provenance instead of expecting legacy unstamped task events. The
event domain and clock contracts pass 123 tests (two optional tests skipped);
this still does not establish durable application performance.

`cold_profile.py --case cold` diagnoses actual transient task preparation,
public invoke/publication and terminal cleanup, with extraction as a nested
invoke subphase. It preserves the initial extraction oracle and 20 warmups.
Foreground-only GC callbacks report their phase intervals; metadata wrappers
use a static type to avoid adding class cycles per task. Raw phase/CPU vectors
and independent full-cohort memory workers share the same instrumentation.
`--samples 2 --pairs 1` validates the driver; defaults are 100 and three pairs.
The instrumented comparison is diagnostic, so existing cold guard failures
remain failures. Nested phase durations must not be summed as disjoint stages;
background GC, transport, durable storage and full-agent work remain excluded.

The three-pair cold-phase diagnosis verifies 600 exact-output observations in
12 distinct latency/memory workers. `system_repository_cold_phase_profile_20261008.json`
retains prepare/extraction/invoke/finish and foreground-GC summaries plus raw
/source witnesses. Extraction dominates these observed calls; preparation and
cleanup are small. This instrumented run does not explain the earlier slow
cluster or establish its host/GC cause. Original uninstrumented cold-tail
failures remain unwaived, and the cache is not installed by this diagnosis.

`published_extraction.py` isolates the pre-cursor extraction function from its
retained Git ancestor in a private scanner namespace. It supplies the real
public scanner parameter through the actual registry handler/finalizer; the
production module global and imports are unchanged. `published_extraction_measure.py`
compares that implementation with the current cursor using uncached lifecycle
branches in both arms, so this is extraction evidence rather than cache evidence.
`check_published_extraction.py` verifies Python/JS/TS/Rust, repeated/edit exact
metadata, eight canonical publications and terminal removal. The small driver
cohort verifies all three traces in 12 independent workers, with P50 ratios
about 0.496/0.502/0.509; full latency/CPU/RSS guards remain required. The parent
ancestor revision/function digest and loaded extractor versions are retained.
Durable storage, gateway, full-agent, larger/scaled and five-workload repository
coverage are still gaps; no whole-domain admission follows from this pilot.

`domain_measure.py` exercises the five repository workloads through canonical
transient task admission, registry lookup, public handler, scanner, rank/render
and tool-result publication. Both arms are uncached; the retained pre-cursor
function differs. The deterministic Python/multilingual/reference-graph fixtures
contain 96 files (192 for ranking); incremental edits include the actual write,
and cold scans include a new task/settlement per operation. First operation is
recorded separately, not promoted into repeated process-cold evidence.

Run a CLI pilot with `--samples 2 --pairs 1 --output-prefix /tmp/repository-pilot`,
then the default 100-sample/three-pair comparison. Executed parity/error/cleanup
checks are mandatory. Per-operation wall/CPU vectors and exact output traces
are retained. Separate workers run the same full cohort and measure OS peak
RSS plus positive post-cleanup RSS growth from a warmed/settled baseline. This
retention definition includes native/allocator residency; it is not a precise
cache charge or long-run leak proof. Reports use the shared admission format;
the other five domains remain missing and the whole-system goal stays incomplete.
HTTP, durable chat and model execution are outside this transient tool boundary.

Current installed-kernel system admission uses `native_domain_measure.py
--native-package /path/to/isolated/install --output-dir /tmp/unique-run`.
First run `--samples 2 --pairs 1 --cold-pairs 1`; the full defaults collect the
five declared cases with separate timing/memory processes and 100 fresh cold
pairs per case. The reference environment must provide working NetworkX/SciPy
and exclude the native package. Both arms refuse reference-count fallback.
Clocks include actual transient tool publication and completion of its queued
audit writes. They exclude common interpreter/module startup; cold operations
include first backend loading. Native artifact/source identity, raw clocks,
backend availability, exact results and owned cleanup are retained. No cache,
GC timing change, HTTP or durable-chat claim is introduced.
CPU includes the worker's threads and reaped children; raw clocks retain those
components separately. Reports pin Git/rg executable availability, paths and
binary digests. Historical parent-only CPU vectors retain their narrower scope.

Memory workers journal raw clocks and joined-child receipts to owned unlinked
files, then reconstruct them after residency counters are frozen. Reports pin
the journal count/bytes/digest and this ordering; earlier list-retaining cohorts
keep their original observer scope. See the memory-observer decision in
`docs/modules/tools_execution.md`. This does not subtract estimated observer
bytes, remove the inherited wait4 limitation, or requalify historical failures.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
