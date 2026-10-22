Rust source and locked build authority now live in
[`packages/tofu-repo-kernel/rust/extraction/`](../../../packages/tofu-repo-kernel/rust/extraction/).
The installed stream binary is `tofu-repository-stream`; this directory retains
the experiment protocol and historical measurement interpretation.

# Native repository tag extraction candidate

Problem: the Python grammar adapter imports generated configuration classes
before ordinary repository parsing, and every tree cursor step crosses the
Python/native boundary. Decision: the exact locked upstream Rust registry loads
the existing trusted grammar libraries, and Rust performs the canonical tag
traversal. Alternatives: modifying installed Python packages creates an
unmaintained import override; replacing the traversal with upstream symbol
queries changes the established Tag semantics.

`src/lib.rs` owns the experimental serde request/response types. A request has
format `tofu.repository-extraction/v1` and ordered `files`, each containing
`rel_path`, `language` and UTF-8 `source`. The response retains occurrence order
and returns `rel_path`, `tags` and a nullable error for each file. Tag fields
mirror the canonical dataclass in `lib/repo_map/outline.py`; the Python
differential checks enforce exact values and ordering. Unknown request fields,
invalid JSON/Unicode and an unsupported format refuse the whole request.
Missing/broken grammars withhold only that file's tags. The CLI prepares the
complete response before writing stdout; callers must reject incomplete frames.

The CLI takes an explicit trusted grammar directory, request byte limit and
response byte limit. It reads one JSON request from stdin and writes one JSON
response. It never discovers a user cache or downloads a grammar. Loaded grammar
libraries retire with the owned process; no in-process unload contract is
claimed. The parent owns deadline/cancellation and waits for terminal exit.
Bounds apply to wire bytes; tree/Tag/source residency still requires measurement.

Protocol `tofu.repository-extraction/v2` additionally allows an omitted/null
`language`, resolved from the pinned POSIX filename catalog in Rust. An explicit
language remains authoritative. v1 continues to refuse missing/null languages.
The response echoes the selected protocol. An unknown filename stays a file-local
`invalid_language` error. This wire version is separate from the C ABI version.

Problem: both source admission and filename-to-language lookup eagerly import
the Python grammar package. Decision: `generate_catalog.py` reads the exact
reviewed grep-ast 0.9.0 source without importing it. It generates the 204-entry
Rust mapping, a lightweight Python extension-key set for canonical admission,
and 2,244 filename expectations computed with CPython `posixpath`. The upstream
source SHA256 is checked before generation; its Apache-2.0 license is retained
in `LICENSE.grep-ast.txt`. `src/catalog.rs` owns lexical lookup, including full
basenames, case sensitivity and dotfile rules. The scanner still borrows the
canonical skip list and Python AST path. Basename recognition does not expand
the canonical extension-based source-admission policy.

`NativeCatalogBatchScanner` explicitly selects v2. It never imports Python
parser packages for catalog/admission; grammar availability belongs to its
trusted native artifacts. Alternatives rejected: a hand-maintained extension
table drifts, and import shims leave dependency internals as another authority.
This is an experimental POSIX catalog, with the library's existing artifact,
residency and cancellation limits. It introduces no production selection.

Regenerate with `python3 packages/tofu-repo-kernel/rust/extraction/generate_catalog.py
--upstream /path/to/grep_ast/parsers.py --python-output
lib/repo_map/_native_catalog.py`; add `--check` to refuse drift. Updating
the pin requires reviewing both the upstream table and resolver. The generator
also verifies/copies the adjacent distribution's license. Run
`tests/repo_map/test_native_extraction_catalog.py` with explicit library/grammar
artifacts: exact tags, admission parity, v1 refusal, actual tool publication and
a fresh process denying all Python parser-package imports are required.

The same crate also builds a `cdylib`. `src/ffi.rs` exports
`tofu_repository_extract_json_v1`: explicit UTF-8 grammar-path and JSON byte
regions, caller-owned output capacity, and an output-length pointer. Status 0
means success, 2 refusal, 3 contained Rust panic. Refusal leaves the output buffer
untouched and resets length to zero. `src/wire.rs` is the single bounded JSON
preparation owner for both interfaces. Read the export's unsafe memory contract
before using another caller. Rust panic containment cannot catch grammar faults,
allocator aborts or blocked native I/O.

Problem: a separate extraction child adds a process boundary to every scan.
Decision: `native_extraction_library.py` explicitly tests one coarse library call
per batch, with the same source admission, schema and traversal. Alternative:
creating a new resident worker pool before measuring this boundary adds another
lifecycle owner. The library experiment adds no pool or application selector.
All loaded grammar paths remain resident until process exit; only fixed trusted
artifact paths are appropriate. The Python catalog and batched source residency
remain. No hard deadline, installed-artifact or memory admission is claimed.

Build into an external target directory:

```sh
env -u TSLP_LANGUAGES -u TSLP_SKIP_GRAMMAR_PATCHES -u PROJECT_ROOT \
  TSLP_OFFLINE=1 CARGO_TARGET_DIR=/tmp/tofu-native-extraction-target \
  cargo build --release --locked --manifest-path packages/tofu-repo-kernel/rust/extraction/Cargo.toml
```

Leaving `TSLP_LANGUAGES` unset selects no built-in grammars. An empty string is
an invalid grammar selection in upstream 1.20.0. Keep upstream scanner safety
patch checks enabled. `TSLP_OFFLINE` prevents grammar-source downloads; Cargo
dependency acquisition follows its ordinary locked build behavior.

Run `check_native_extraction.py --binary BINARY --grammar-dir DIRECTORY
--output /tmp/native-extraction-oracle.json`, then
`check_native_extraction_tool.py` with the same arguments. The first check uses
the exact installed Python grammar directory and refuses absent fixture
grammars. It covers seven languages, deep/malformed syntax, Unicode, parser
reuse, concurrent children, corrupted grammars, bounded wire failures and a
blocked grammar open killed and joined by the parent. The second uses actual
transient task admission, repository handlers, ranking/render and publication.
Tests are `tests/repo_map/test_native_extraction_candidate.py`, with
`TOFU_NATIVE_EXTRACTION_BINARY` and `TOFU_NATIVE_EXTRACTION_GRAMMARS` explicit.
Also set `TOFU_NATIVE_EXTRACTION_LIBRARY` to check the library with the same
source corpus and actual tool/publication checks. The library check joins 64
concurrent calls on a shared CDLL and verifies caller-buffer contents after
malformed, over-budget and invalid-Unicode refusals. These checks do not run the
CLI's blocked-grammar cancellation test inside the application process.

`native_extraction_scan.py` borrows canonical admission/read/reference helpers,
retains one source batch, and launches one extraction child per scan. Its strict
mode refuses grammar failures during measurement; `strict=False` verifies the
canonical per-file degradation behavior. It still initializes the ordinary
Python language catalog. These checks do not claim cold-import removal,
constant-memory streaming, sealed installed execution, repeated-interrupt
cleanup, performance admission or an application default. Python AST handling,
filename/language selection and public ownership remain with their current
owners until their own verified migration.

The upstream registry is the same
[Rust grammar API](https://docs.rs/tree-sitter-language-pack/1.20.0/tree_sitter_language_pack/)
used by the installed Python package; Cargo.lock pins the candidate dependency.

The first frozen real-tool pilot
retains 78 fresh workers, three 100-sample warm pairs and ten cold pairs for
multilingual extraction, ranking and incremental edits. Outputs and cleanup
match. Incremental warm P50 ratios are 1.096–1.119; two multilingual pairs also
regress. Ranking CPU improves about 4%–5%, while its median latency rises about
1%–2%. The added per-scan process/JSON boundary and unchanged Python catalog
remain in this candidate. This is a rejected performance direction in its
current form, not a default candidate; separate memory and full cold admission
were not run after the pilot exposed these regressions.

The coarse library pilot
also retains 78 fresh workers with the same warm/cold scope. All nine warm pairs
reduce worker-plus-child CPU (ratios 0.742–0.928). Ranking warm medians improve
9%–16%, and incremental medians improve 7%–9%. Six warm guard exceedances remain:
one multilingual pair's P50/P95/P99, one ranking P99, and one incremental pair's
P95/P99. No causal attribution for those latency spikes is established. Per-worker
library/grammar hashes and exact outputs are retained, but no separate memory,
full cold, hard cancellation or installed-artifact admission follows. The Python
catalog remains in the cold path, so the unchanged candidate is not promoted.

The native catalog pilot
retains 168 passing related tests and 78 fresh workers. All 39 candidate workers
finish without Python parser modules. Ten-pair cold medians are 71.25→45.72 ms
for multilingual extraction, 78.82→50.29 ms for ranking, and 71.16→45.84 ms for
incremental edits. All nine warm CPU ratios improve (0.811–0.904). Five warm
latency guard exceedances remain: multilingual P99, one ranking pair's P50/P95/
P99, and incremental P99. These observations do not establish the spikes' cause.
The incomplete cold population, unmeasured separate memory, eager library bind
on Python-only scans and unresolved native cancellation prevent default admission.

The current library adapter binds on the first native request. One scanner lock
publishes the library and configured callable together; interrupted or failed
binding remains retryable. Python-only scans short-circuit before importing the
generated extension set and never load the extraction library. Problem: eager
binding taxed scans with no native work. Decision: defer binding and catalog
loading until needed. Alternative: a global eagerly initialized library would
retain the same optional startup cost and another lifetime owner. Verify
`test_native_extraction_lazy.py`: concurrent first use, failed/interrupted retry
and a fresh Python-only scan prohibiting both optional loads. This preserves the
existing process-resident library lifetime after first use; it does not provide
hard cancellation of an active parse or native fault containment.
The optional-load comparison
retains 172 passing related tests and 46 fresh workers comparing the frozen eager
candidate with the lazy candidate on Python-only scans. All 23 lazy workers have
neither the extraction image mapped nor the generated catalog imported after
the operation. Twenty-pair cold medians are 44.25→43.94 ms; three warm CPU pairs
improve 3%–6%. Procfs mapping presence is not a physical-memory measurement, and
this scoped comparison does not waive the earlier mixed-language tail failures.

## Owned discovery/read/extraction stream

Problem: separate discovery and extraction launches pay two process boundaries;
an in-process parser cannot be killed safely, and parent file reads can block
cancellation. Decision: `tofu-repository-stream-candidate` performs real-Git
discovery, source reads and parsing in one owned child. The Python adapter
`native_extraction_stream.py` keeps canonical selection, Python AST, references,
and RepoMap assembly. Alternatives considered: a resident pool adds an idle
lifetime and task isolation owner; in-process extraction leaves blocked native
work outside the kill/join boundary. Neither is installed by this experiment.

The stream uses u64 little-endian byte lengths. Its first frame contains the
codec byte and NUL-delimited names. The parent returns one `tofu.repository-stream/v2`
selection request with discovery indexes then input EOF. The child rejects
duplicate/foreign indexes, emits one ordered source/tag frame with the same
index and path-free tags per selected file, then a zero-length
terminal frame. The parent requires terminal framing, output EOF and successful
joined exit before exposing any result. Malformed, incomplete, trailing or
over-budget frames refuse the whole call. A successful empty Git tree remains
authoritative; ordinary Git discovery failure negotiates directly owned rg,
then a Rust walk if rg fails. Walk names retain original POSIX bytes.

Source text matches UTF-8 replacement decoding and universal newline conversion.
Admitted Python files retain complete text for the canonical AST; other files
return native tags and only the reference character prefix. Oversized sources
retain canonical reference-prefix behavior. Ordinary metadata/open/read failure
remains file-local; source growth beyond the explicit read ceiling refuses the
whole call, never publishing a truncated success. Strict grammar failure refuses
the scan; non-strict mode preserves canonical file-local degradation.

Every scan seals and verifies its executable digest through the installed
`tofu_repo_kernel` owner, projects the allowed Git environment, starts without a
parent `cwd`/`preexec_fn`, and kills then joins before closing owned resources.
One deadline covers child discovery, blocked file/grammar I/O and transport;
cancellation is checked during I/O and between bounded parent computations.
Repeated cleanup interrupts preserve the original failure and still join.
Stderr is drained without retaining an unbounded diagnostic buffer. There is no
resident worker, parser pool or retained executable descriptor between scans.
After both output pipes close, completion waits on the owned PID with its
remaining deadline and cancellation polling. Selecting an empty descriptor set
would impose the entire polling interval on an already exiting child; the
closed-pipe tests cover successful exit, timeout and cancellation explicitly.

`StreamLimits` requires explicit path/request/file-frame/source-read budgets.
These limits do not certify parser, tag or accumulated-result RSS, and the test
values are not a launch-capacity policy. Artifact admission and parent AST/
reference CPU remain cooperative; application task-cancellation wiring is still
pending. Grammar paths remain trusted mutable inputs, not sealed artifacts.
Real-Git coverage, local Linux descriptor execution and the pinned grammar
catalog do not admit cross-platform discovery, product fallback or defaults.
Run `test_native_extraction_stream.py` with `TOFU_NATIVE_STREAM_BINARY`, the exact
grammar directory and the installed native package; it shares the existing
331-source and actual transient tool/publication oracles.

The stream pilot
retains 196 passing related tests and 130 distinct joined workers across all five
repository workloads. Every three-pair warm median improves (ratios 0.569–0.635),
but twelve CPU guards and one cold-scan warm P99 guard exceed 1.05. The mixed
language ten-pair cold medians improve from roughly 70–78 ms to 41–44 ms; Python
cold CPU increases. These incomplete cold cohorts and unmeasured separate memory
do not admit defaults. An initial diagnostic exposed empty-selector 50 ms exit
waits; the fixed source, original failed clocks and 30-scan before/after evidence
are retained. A separate parent CPU profile identifies frequent framing reads
and selector registration plus per-scan image sealing as further costs; its
instrumented timings are not application performance evidence.

The parent transport now attempts nonblocking reads before registering readiness
and retains at most one 64 KiB read-ahead block. An offset consumes coalesced
frames without repeatedly copying their unread suffix; incomplete frames still
assemble under the existing byte ceiling. Buffered bytes beyond the terminal
frame refuse completion, just like bytes still in the pipe. Problem: each
small header/payload separately registered, selected and read. Decision: amortize
those operations over ready bytes. Alternative: unbounded whole-result reads
would weaken streaming and delay cancellation. Fragmented reads, coalesced rows,
trailing bytes and the existing cancellation/retirement corpus verify this
transport change. The additional bounded buffer is not a parser/result RSS claim.
The buffered pilot
retains 199 passing related tests and 130 exact-output, joined workers using the
unchanged native artifacts. Warm median ratios are 0.519–0.599. Python warm CPU
ratios are 1.183–1.209; mixed/incremental/task-recreated scans remain 1.087–1.110,
while ranking is 0.984–1.003. Twelve CPU guards and two warm P99 guards still
fail. Separate parent profiles confirm fewer framing/readiness operations, but
are not uninstrumented end-to-end performance evidence. Ten-pair cold results,
no separate memory cohort and unresolved application/grammar ownership continue
to preclude defaults; per-scan executable verification remains a measured cost.

## Execution-owned executable image

Problem: copying, hashing and sealing the same executable on every scan adds
parent CPU, while a global cached image has no task release boundary. Decision:
`native_extraction_owned.py::OwnedStreamScope` explicitly authorizes one owner
and root and lazily owns one sealed image across its scans. Each scan still owns
an independent child; there is no resident parser, worker pool or source cache.
Alternative: trusting a mutable pathname after one hash would allow replacement
between validation and execution. Existing one-shot scanners retain per-call
sealing; only the explicit execution scope borrows an owned image.

Each accepted call registers one lease before artifact or subprocess work.
Closing refuses further admission, sets cancellation, joins accepted scans,
then closes the image. The original image remains usable if its source pathname
changes; the next scope verifies its own bytes and refuses a digest mismatch.
Concurrent first scans publish one image; failed/interrupted admission is
retryable. A close from its own active scan refuses reentry. Interrupted joins
and image closure finish retirement before propagating interruption; an ordinary
close failure retains the owner for retry. Context cleanup preserves the primary
error. Artifact admission and bounded parent CPU remain cooperative.

`start_owned_workload` binds this scope to the real transient task's existing
`tool_environment` release callback and `abort_event`. It preserves owner/root
denial, canonical handlers and error publication. Tests invoke authoritative
`cancel_task`: a foreign owner cannot signal the task; cancellation during a
blocked source read joins the child and yields no successful late tool result.
Task completion/failed registration release the image through the existing
resource boundary. This verifies explicit task integration; product selection,
durable conversation publication, sealed grammar ownership and capacity-derived
budgets remain separate requirements. One retained image consumes its measured
artifact size per active execution; a borrowed scan also retains at most one
64 KiB transport block, apart from its admitted frames and accumulated result.

The `system_repository_extraction_owned_20261015.json`
retains 219 passing related tests and 160 independent tool workers. All warm
latency guards pass. The four cases that reuse tasks reduce CPU in every pair
(ratios 0.665–0.875); the task-recreated case still exceeds CPU guards in all
three pairs (1.080–1.118). All 824 measured scopes close their images. Each
admitted image is 1,769,104 bytes and its accounted retention is zero after close.

Thirty further independent memory workers execute the same 100-operation
cohorts. The existing exit observer verifies all 3,660 direct-child identities,
single execs, successful waits and joined cleanup before selecting post-exec
RSS estimates. The declared peak sum is parent VmHWM plus maximum direct-child
post-exec peak plus sealed image bytes; independent/shared peaks may overlap,
and this is not peak PSS. All retained-growth guards pass. One Python pair's peak
estimate fails at 1.051417; the other peak ratios are within the 1.05 guard.
The initial memory qualification correctly refused two unjournaled metadata Git
children; the observer worker now reads frozen source receipts and validates
file hashes without spawning those helpers. Original refusal evidence remains.
Ten cold pairs, short-task CPU and the peak-memory failure still prevent default
admission. Product/whole-agent wiring and immutable installed grammars remain
separate from the verified explicit transient task owner.


## Explicit runtime image loans

Problem: recreating a task also repeats immutable executable sealing, while
code identity does not belong to a project or user. Decision: an explicitly
constructed `RuntimeStreamImage` may retain one lazy image and lend it to
`OwnedStreamScope(runtime_image=...)` during active scans. The runtime contains
no root, user, repository cache or resident parser process. Alternative rejected:
an unowned process-global cache has no deterministic shutdown boundary.

Task authority and canonical abort events remain independent. Task close drains
only its calls; runtime close stops admission and cancels/drains all borrowers
before image disposal. Idle task scopes do not prevent runtime shutdown. Reentry
from an active borrower refuses; interrupted retirement preserves the original
failure, and failed disposal remains retryable. Borrowers must request the same
artifact path/digest. Mutable path replacement cannot change sealed bytes; a new
runtime validates its own first-use image. Runtime construction is cheap and
sealing remains inside the first actual scan, including first-use measurement.

The application owner must close this object after draining tasks, including
exceptional exits. A borrowed task reports zero locally owned image bytes; the
runtime reports the actual retained artifact size until shutdown. Memory
measurements include that size once in the peak sum, even across task recreation.
`native_source_admission.py` shares the generated extension policy without
importing other extraction transports or Python parser packages. Runtime loans
remain explicit experiments; installed grammar integrity, launch-probed budgets,
plain-directory fallback, whole-agent integration and default admission are open.

The retained Python stream, byte limits, source admission and image lifetimes
now live under `lib/repo_map/`; benchmark modules explicitly reexport them.
`native_extraction_owned.start_owned_workload` remains a measurement adapter.
Trusted application/SDK composition uses `lib/repo_map/native_runtime.py` and
`tofu_agent.NativeRepositoryConfiguration`; it cannot be selected by model JSON.
See the tools execution and developer-runtime contracts for lifecycle and limits.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
