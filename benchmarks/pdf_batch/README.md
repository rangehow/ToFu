# PDF scheduling experiment

Both variants call the unchanged rich PDF core. The parent holds canonical
admission per compressed document and dispatches windows no larger than the
launch-derived unfinished capacity. The candidate groups within each window,
with chunk size limited by the actual worker count. Each result or parser error
is preserved; unexpected worker failure joins work before releasing admission.
No application scheduler/default or persistent data is changed.

Run `python benchmarks/pdf_batch/experiment.py --check --output /tmp/pdf-check.json`
for actual PDF/malformed-result equality, closed admission, lease balance and
joined workers. Run `python benchmarks/pdf_batch/measure.py --output /tmp/pdf-batch.json`
for single-document negative control and16-document batch comparisons: three
alternating independent process pairs, respectively1000/100 samples after20
warmups, plus another process tree sampled every5ms for memory. Worker count is
requested4 and clipped by canonical capacity; this is a controlled experiment.

CPU uses a separate interval of repeated extraction, without per-operation CPU
inspection, trace hashing or RSS sampling. Counter granularity is amortized over
that interval. Memory includes fixtures and untimed output hashing and may miss
spikes shorter than5ms. Cold has only three observations. Earlier observations
were diagnostic; inspect the revised distributions before considering promotion.

`faults.py` proves concurrent close callers all wait for owned worker exit, and
an actual killed worker fails without retry, releases canonical leases only after
joined shutdown, and leaves no worker. The original closed-flag-only owner fails
concurrent disposal; an event/lock now distinguishes rejected admission from
completed disposal. These are experimental pool checks, not application-level
cancel/progress/repository integration.

`heterogeneous.py --check --output /tmp/pdf-heterogeneous-check.json` compares
eight real PDFs with alternating 1/2/4/6/8-page work, differing text density and
drawn tables, plus one malformed document. Chunk sizes 1 and 4 must preserve
exact rich results, input order and per-document error carriers; admission
returns to its initial count and every worker is joined. Fixture page counts,
byte sizes and content hashes accompany the report.

`heterogeneous.py --measure --output /tmp/pdf-heterogeneous.json` performs
three alternating fresh-process pairs of 100 observations after 20 warmups,
with separate CPU intervals and memory process trees. The parent freezes one
byte-identical input snapshot for every latency/memory worker; worker admission
verifies each file hash. Every pair is checkpointed. Supplied `--samples 2
--pairs 1` is smoke evidence only. The shared measurement worker accepts an
explicit document collection; its original fixture/default cases are unchanged.
Heterogeneous PDFs do not cover mixed web/PDF application scheduling, durable
progress, application cancellation/recovery or cold-start admission.

The explicit `TOFU_BENCHMARK_PYTHON` launcher is also selected through
`multiprocessing.set_executable` for spawned workers. **Problem:** launching
the parent through a compatibility loader while spawning bare Python produced
per-document native-import failures in every worker. **Decision:** retain the
same explicit runtime for the full process tree and keep the failed run as
evidence. **Alternatives considered:** treating these errors as equivalent
outputs or omitting larger PDFs would conceal a broken comparison; changing
production parsing/storage would bypass the experiment's unchanged-owner rule.
The heterogeneous comparison completed 600 latency observations/12 independent
latency/memory processes with exact output/error traces. Static grouping has no
primary gain and two P99 guard violations; it remains unpromoted. Per-pair
metrics and original artifact identity are retained in
`system_candidate_rejections_20261008.json`.

`size_order.py` is a separate scheduling candidate: each existing bounded
admission window submits larger byte payloads first, one document per future,
then returns the original order. It never opens PDFs to invent cheap estimates,
changes rich parsing, expands worker capacity or retries a crashed worker.
`size_order_check.py --output /tmp/pdf-size-check.json` checks nine varied real
PDFs including malformed input; `size_order_faults.py` checks actual concurrent
close and SIGKILL. A shutdown failure retains admission leases on the owner and
preserves the primary exception; unknown quiescence cannot admit more payloads.
`size_order_measure.py --measure --samples 100 --pairs 3 --output /tmp/pdf-size.json`
reuses frozen-byte inputs and process-tree accounting. Both arms use singleton
submissions; the candidate changes order only. `--samples 2 --pairs 1` is smoke.
**Problem:** static grouping had no heterogeneous primary gain and failed tails.
**Decision:** compare bounded byte-size priority without parser changes.
**Alternatives:** parsing page counts adds another parser pass; unbounded
whole-corpus sorting changes admission; fixed grouped tasks amplify stragglers.
Byte length is an imperfect cost hint, so real gains remain unproven. Full
application mixed web/PDF, deadlines/cancellation, durable progress/recovery and
cold/resource-scaled deployment remain required before production promotion.

The full size-order comparison verifies 600 latency observations in 12 independent
latency/RSS worker processes and exact per-pair parsed/error traces. P50 stays
about 0.2–0.3% slower, CPU remains near neutral, and RSS falls 3.5–3.9%. The
third-pair P99 gain does not establish a consistent primary latency improvement.
The candidate is unpromoted; `system_pdf_size_order_20261008.json`
records all pair ratios, input/source/raw identities and concurrent-host/full-app
coverage limits. Parser/process budgets and document order/errors remain unchanged.

**Observed-cost decision — Problem:** byte-size ordering has no primary latency
gain on the frozen rich-PDF cohort. **Decision:** `measured_cost.py` records
worker parser-call nanoseconds and prioritizes known expensive inputs inside
each canonical window. Unknown inputs use byte hints in a separate priority
category; nanoseconds and bytes are never treated as the same unit. SHA256 keys
retain no payload/result; the owner-scoped LRU charges conservative key/value/
entry bytes against one eighth of the launch-derived text envelope, and clears
after close. Repeated files are still parsed every time. **Alternatives:**
unbounded/global hints lose ownership; result reuse would add a new invalidation
authority; size alone failed the measured objective. None is adopted.
Verify `measured_cost_check.py` (real output/errors/order, tiny-budget eviction),
`measured_cost_faults.py` (actual close/SIGKILL), then the small and full
`measured_cost_measure.py` paired cohorts. No production scheduling/default
change or scaled/full-app/durable/recovery admission is established here.

Formal observed-cost pairs verify 600 exact-output latency observations and 12
independent latency/memory workers. P50/CPU remain near neutral and peak RSS
falls about 3–4%; inconsistent P99 gains do not meet primary-gain admission.
`system_pdf_observed_cost_20261008.json`
retains all ratios and source/raw/fixture identities. The candidate remains
unpromoted; mixed application scheduling, scaled resources, cold, durable
progress and recovery/cancellation coverage are still required.

**Page-job decision — Problem:** document-only jobs leave a long document
serial while another process may idle. **Decision:** `page_jobs.py` reuses the
canonical `_extract_classic_page`, the complete parent heading object and
original page indices in bounded chunks on the same worker/admission budget.
Documents reopen/close per chunk; no page/document cache is retained. Parent
assembly, images, limits and ordered progress stay canonical. Short documents
and explicit small text ceilings use serial extraction. **Alternatives rejected:**
nested pools oversubscribe the shared budget; global document caches retain
input and introduce ownership/close authority. One owner denies new admission
before waiting for active parent parses and worker jobs. Worker transport loss
is re-raised after canonical fallback, and processes join before lease release;
failed quiescence retains lease debt and the primary error. The pool initializer
primes classic imports before heading deserialization without protocol banners.
Verify `page_jobs_check.py`, real `page_jobs_faults.py` and baseline fault
neighbors before `page_jobs_measure.py` paired runs. The current two-sample
pilot has exact rich output/error traces and four independent workers, but
P50 is about 3.5% slower and sampled RSS about 13% higher. This is not formal
admission or evidence for larger single-document/resource-scaled workloads.
No application scheduling/default changes or full-ingest promotion are made.

`single_document_measure.py` separately tests one parent-frozen 32-page
text/table PDF with the same full-core defaults and process/admission budgets.
Its small pilot verifies exact output traces and four independent latency/RSS
processes: P50 is about 27% lower, CPU near neutral, but tree RSS about 86%
higher. This is a workload-specific pilot, not adoption; mixed-batch failures
remain failures and full/cold/scaled/application evidence remains required.
`page_jobs_reentrant_check.py` verifies explicit owner validation and progress
observer self-close refusal without deadlock: accepted parsing can complete,
new/empty work stays refused, and a later external close joins/clears admission.
No model/user input can supply a page callback or change these policies.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
