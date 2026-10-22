# Repository index experiment guidance

This directory owns offline extraction-cache and native-ranking candidates.
It does not export a production index or change repository tool behavior.
Canonical scan/rank/render under `lib/repo_map/` remain the reference owners.

`packages/tofu-repo-kernel/rust/extraction/` owns the Rust extraction sources
and locked grammar registry; `native_extraction/` retains experiment documentation. `check_native_extraction.py` compares exact installed Python Tag values;
`native_extraction_scan.py` and `check_native_extraction_tool.py` verify the real
transient tool boundary. The adapter retains one source batch and still loads
the Python language catalog; neither cold-import elimination nor production
lifecycle/default admission follows from these checks. Build artifacts stay
outside the checkout. Verify `tests/repo_map/test_native_extraction_candidate.py`
with explicit binary/grammar environment paths before measuring.
`native_extraction_library.py` and `check_native_extraction_library.py` exercise
the coarse C boundary with caller-owned output. Grammar libraries stay resident
until process exit; no hard cancellation or default admission is implied. Set
the additional `TOFU_NATIVE_EXTRACTION_LIBRARY` artifact for its candidate tests.
`NativeCatalogBatchScanner` opts into the pinned native v2 filename catalog.
Its generated extension set comes from the same reviewed upstream source; never
edit that output or generated Rust mapping. Verify catalog/admission parity and
the no-parser-import subprocess in `test_native_extraction_catalog.py` before
collecting fresh cold clocks. POSIX coverage is not cross-platform admission.
Library binding is lazy and scanner-owned: one lock publishes a complete CDLL
and callable together, failed/interrupted binding stays retryable. Python AST
(.py), extensionless and explicit skip-list paths bypass the generated catalog;
scans with no native batch never load the extraction image.
Verify `test_native_extraction_lazy.py` before measuring optional-load costs;
loader interruption checks do not qualify interruption of active native parsing.

`native_extraction_stream.py` reexports the application-owned
`lib/repo_map/_native_stream.py` real-Git scan in one joined child
using the packaged `rust/extraction/src/stream_main.rs`. File reads and native parsing stay
inside its killable boundary; Python AST/reference processing stays cooperative.
Verify stream parity, protocol refusal and interrupted cleanup before collecting
actual-tool clocks. Read its native README for byte-budget and admission limits.
`native_extraction_owned.py` reexports `lib/repo_map/_native_owned.py` and retains
only the benchmark task adapter. The canonical scope lends one lazy sealed image across an explicitly
authorized execution/root. Its close cancels and joins active scans before image
release; the real transient task adapter binds the canonical abort event and
tool-environment resource. Verify `test_native_extraction_owned.py`, including
owner cancellation and registration rollback. This is an explicit candidate,
not default admission, a resident child pool or installed grammar authority.
`lib/repo_map/native_runtime.py` and trusted SDK configuration own explicit
application selection; verify the native application scanner/runtime tests.

`native_csr_kernel.rs` is a compile wrapper for the standalone package's Rust
source; `native_csr.py` exports its digest-verified bridge and `native_repo_rank.py`
exports production-owned graph semantics. Install the standalone wheel first,
then compile outside the checkout,
then run `check_native_csr.py --library PATH` and the repository parity oracle.
Invalid shapes, concurrent calls and withheld partial outputs are required.
`sparse_rank_measure.py --backend native --library PATH --output /tmp/result.json`
compares frozen NetworkX generation against actual scan/rank/render. Run a
two-sample/one-pair pilot before the full comparison. The same driver compares
the direct SciPy candidate by default. Neither installs a production backend;
their scoped comparisons do not substitute for production admission.

`native_publication_measure.py` compares installed-wheel production selection
against package absence through real transient task registry/handler/publication.
Both arms use current cursor extraction. Independent child environments select
availability; worker receipts must match the requested arm. Run a two-sample,
one-pair pilot before full 100-sample/three-pair coverage. Verify installed Python
bytes, compiled source/library receipt and exact output/cleanup traces; do not
mix the optional package into the reference child environment.

`index.py` reads complete admitted source and retains only derived data plus a
content witness, scoped to an explicit owner/root and byte budget. `check.py`
verifies edits, filtering, read faults, result isolation and eviction.
`native_rank.py` is a Rustworkx candidate; its strict precision check currently
fails and never authorizes promotion. Read README.md before measurement.
Dependency installations and generated results stay outside the checkout.

`real_measure.py` owns frozen real-source paired measurements. `direct_tags.py`
and `check_direct_tags.py` compare production cursor extraction without grep
display metadata to the immutable Git reference. The owning tools document and
`tests/repo_map/` define valid deep-nesting and file-local failure behavior.

`check_session.py` verifies actual execution settlement of the experimental
scope. `budget.py`/`check_budget.py` propose sizing from the shared launch probe;
they do not provide an application registry or global quota enforcement.

`quota.py` owns experimental byte/slot reservations. `scoped_measure.py`
measures their full execution-bound cold/warm/edited pipeline.
`check_environment.py` checks real environment/client-waiter composition before
cache quiescence; neither script installs an application cache default.

`check_packed.py` verifies immutable byte-cache field fidelity and charging.
The tuple predecessor's measured failures remain retained evidence; packed
storage requires a fresh full scoped comparison before any promotion.

`tool_port.py` privately binds the scoped scanner into actual `generate_repo_map`
and the repository tool handler. `check_tool_port.py` verifies text/metadata and
one settlement, root/owner denial, edits/epochs and terminal close. `tool_measure.py`
reuses execution/quota accounting with those ports; memory uses the full cohort.
Its recording finalizer is not durable publication or a production registry.

`task_port.py` uses canonical manager task ownership; the older running component
port retains its explicit fixture-only `user_id` alias. `task_indexes.py` owns
one execution reservation across authorized roots, active budget leases and
terminal joining. Verify `check_task_indexes.py` and `check_task_index_faults.py`.
Neither installs task state or supplies a real epoch/registry authority.

`task_measure.py` measures actual TaskRuntime task identity and its own execution
session through the task port/group. Cold includes creation/terminal cleanup;
memory runs the full cohort. Run its small worker and CLI smoke before full pairs.
The default corpus is one root; multi-root behavior tests are not performance proof.

`check_task_index_pressure.py` verifies concurrent multi-root scans under tiny
retained-byte quota and original-error precedence on accounting failure.

`extraction_epoch.py` observes loaded Python extraction functions, explicit
rules, local distribution versions and tag schema. `check_extraction_epoch.py`
verifies unchanged-source invalidation and stable code fingerprints. This is
experimental; opaque grammar state/reload fences and full query cost need proof.

`loaded_epoch_task_measure.py` includes real loaded-witness queries in canonical
task/cache comparisons; its per-group witness replaces only the driver's fixed
epoch fixture. `check_loaded_epoch_task.py` checks actual task/tool equality,
rule invalidation, failed-query error settlement and terminal reservation release.
No production registry or durable/reload authority is installed by these scripts.

`bootstrap_epoch_task_measure.py` admits one canonical fresh scan per root
without a loaded witness or byte-cache admission; every later scan observes
facts before admission/reuse. Each fresh scope starts packing
on its second scan. Caller-provided/preseeded scopes are refused. Verify
`check_bootstrap_epoch_task.py` for missed/rebuilt/hit extraction, multi-root
invalidations and failed-witness denial/error settlement before paired runs.

`public_tool_port.py` borrows the real public handler/API scanner parameters.
`check_public_tool_port.py` verifies ownership, root errors and one settlement.
Only recording publication remains privately bound; do not label it durable
application integration. `public_tool_measure.py` owns independent paired runs.

`check_codec_admission.py` pins changed fields before first witnessed caching
and direct capture/restore; codec snapshot mismatch must fail before retaining
or restoring partial fields. Re-run packed/bootstrap/pressure semantic boundaries
and measure new guard overhead independently of older source identities.

`check_multi_root_public.py` verifies actual public ports with one task, three
roots, eight callers, tiny quota and terminal join of a first uncached scan.
It supplies authority/lifecycle semantics, not multi-root performance admission.

`multi_root_measure.py` owns independent three-root cold/warm/edit pairs. Verify
the small CLI cohort, source witnesses, 12 distinct workers per pair and exact
metadata traces before full 100-sample/three-pair measurement. An operation is
three actual public tool calls; do not report its latency as one root scan.

`publication_port.py` owns a transient chat experiment with real registry handler
lookup and canonical event publication. Check `check_publication_port.py` before
`publication_measure.py` paired runs; do not relabel transient events as durable
application publication. Cleanup must preserve tasks owned by other consumers.

`cold_profile.py --case cold` owns instrumented preparation/invoke/finish and
nested extraction/foreground-GC diagnosis. Verify exact traces, nested phase
bounds and source witnesses in a small cohort before full independent pairs.
Do not use diagnostic ratios to waive uninstrumented cold-tail failures.

`published_extraction.py`/`published_extraction_measure.py` compare uncached
ancestor/cursor implementations through real transient publication. Verify
`check_published_extraction.py`, then exact traces and retained ancestor/package
witnesses in small/full cohorts. Neither arm admits an execution cache.

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

`native_domain_measure.py` owns current installed-kernel versus NetworkX system
reports for the same five fixtures. `domain_fixtures.py` keeps inputs shared
without importing the historical Git oracle. `native_domain_worker.py` uses
actual transient manager/handler/publication and bounded audit completion;
no timed worker may enter reference-count fallback. `native_domain_check.py`
executes all fixture, edited-source, publication, foreign-root and cleanup checks
in both package environments before measuring. Verify observation refusal and
cleanup precedence in `tests/repo_map/test_native_domain_measure.py`.
Run `--samples 2 --pairs 1 --cold-pairs 1` before the full defaults. Each case
uses three independent timing pairs, three separate full-cohort memory pairs,
and 100 independent cold pairs. Workers must begin before SciPy/native package
loading and report distinct boot/PID/start identities, exact outputs and joined
cleanup. These are transient repository-tool boundaries; other domains, HTTP,
durable chat and whole-agent/default admission remain separate requirements.
CPU receipts include worker threads plus reaped child CPU, retaining both raw
components. Git/rg executable paths and content digests must match in every
worker; prior parent-only CPU cohorts cannot certify this expanded boundary.

`native_discovery_measure.py` compares Git subprocess discovery with explicit
installed Rust discovery through the same transient repository tool and native
ranking. It owns real Git fixtures and refuses partial/fallback observations.
Use a two-sample/one-pair/one-cold-pair pilot, then independent timing, memory
and fresh-cold workers. Its scoped patch is experimental, never an application
backend switch; cooperative filesystem deadlines remain an admission gap.

Discovery experiments accept `--transport process` for the packaged one-call
Rust worker. Strict observations include its waited CPU and require process
transport receipts. Separate memory workers use `child_memory.py` wait4 receipts
for every joined child, including very short-lived peaks. Report parent RSS,
child maximum and sealed-image bytes separately and their conservative sum;
shared pages may be counted twice. Timing/cold workers install no wait hooks.
Verify wait/poll/communicate exit paths in `tests/repo_map/test_child_memory.py`.
Cold parent-only RSS and the sum are not physical-PSS or whole-system admission.

`record_journal.py` owns unlinked memory-cohort evidence files. Clock records
materialize after parent residency counters; child records materialize after
the worker returns. Counts and content witnesses remain mandatory. Capture
receipts are available only after `observe_children()` exits successfully.
Verify record reclamation, short writes, concurrent appends, failure precedence
and the real memory boundary in `tests/repo_map/test_record_journal.py`.
Journaling does not reset process high-water marks: both parent RUSAGE_SELF
and child wait4 can retain pre-exec peaks in the launcher chain.

For process transport, optional `--memory-observer exit` uses the external
Linux observer only for separate memory workers. `exit_memory.py` requires
matching boot/PID/start identity, every original waited child and exit status,
and complete joined snapshots. Raw worker and observer files remain immutable;
qualified rows retain their hashes and legacy peaks. Parent VmHWM freezes
before record reconstruction. No traced clock, exit PSS or conservative peak
sum grants timing, physical-PSS or default admission. Verify `test_exit_memory.py`
and `test_record_journal.py`, then the small CLI pilot before full memory pairs.

For a cumulative candidate with application-source changes, `--reference-root`
selects a frozen reference checkout. Its real worker and checker execute there;
candidate workers execute in the current checkout. Package sources must remain
identical, and every row must match its own arm's source receipt. Report both
roots and source sets; this mode compares the complete declared source change,
not discovery alone. The fixed reference must not be the moving shared tree.


`native_extraction_owned.py` owns explicit task scopes and optional runtime image
loans. Verify `test_native_extraction_owned.py` and `test_native_extraction_runtime.py`
for cancellation, root/owner isolation, borrower drain and shutdown retries.
Charge borrowed code once to the runtime until its explicit close; keep lazy
sealing inside measured first use. `native_source_admission.py` shares generated
catalog admission without importing another transport or Python parser package.

`native_extraction_runtime_worker.py` retains the task/runtime-owned measurement
entry point. Both arms use identical installed ranking; pin explicit affinity,
stream artifact and grammar directory. It verifies source/grammar identity and
joined scopes; `--profile` creates a separate diagnostic cohort that must never
qualify clocks. Host counters are outside the operation clocks. Smoke warm, cold
and memory pairs before collection; wait4 peak sums remain unqualified until
checked by the independent exit observer. No worker output alone promotes defaults.

The domain worker reports actual profile/trace/tracemalloc state at both cohort
boundaries, including CPython monitoring and thread-default hooks. Never hardcode clean instrumentation flags: collectors reject
instrumented rows even when output equality and process cleanup pass.

The runtime worker receives `--source-commit` from its collector and validates
source bytes itself. Never spawn metadata Git inside the observed worker: it
would be an unjournaled child in the independent memory exit observer.

`native_extraction_runtime_measure.py` runs current correctness/cleanup tests,
then all five paired warm, independent exit-observed memory and fresh cold
cohorts. The default is 3 × 100 warm/memory plus 100 cold processes per arm
and case; `--pilot` deliberately remains incomplete at the unchanged system
gate. Raw rows/observers are immutable; metadata Git runs in the collector,
never the observed worker. Owned worker sessions are killed/joined on failure.

`native_extraction_cold_phases.py` records nested first-use import, image and
scan intervals without warming native imports. Its explicit phase wrappers mark
rows instrumented even without a Python profiler hook; normal admission must
refuse them. These parent/thread CPU and wall intervals overlap and exclude child
CPU inside individual phases. Use the untouched worker for clean comparisons.
