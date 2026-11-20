# Synthetic owner-resident first-open measurement

**Historical observations only.** Measured 2026-11-19 with reported Chromium
149.0.7827.55 and reported source HEAD `07bb4c6273ae744716a56e524c65dcde5c0707c1`.
That HEAD is context, not certification of the executed driver or generated assets.
This is a controlled local
component measurement, **not application/operational speed, an old/new comparison,
or a regression threshold**. No production implementation was changed.

## Historical evidence and successor receipt repair

The 18 samples below are unchanged historical data; **no Chromium samples were
replayed for this repair**. Independent audit 5b330 passed the reported medians,
causality and 88 recorded input hashes. That audit result is reported here, not a
claim that this repair recovered/rechecked those 88 manifest rows. The historical
original driver, original fixture, inlined artifact owner, bundler and generated
entry identities were not recorded. Historical JS/CSS bytes are unavailable;
their recorded hashes below do not restore those bytes. Git HEAD cannot fill any
of these gaps. **Full historical source/asset certification remains unverified.**

`deliverable_first_open_history.json` is a durable path-free, rounded aggregate
transcribed from this pinned note (`6e1666b9ec37`): all 18 component rows, all 12
request-clock rows, reported counts/sizes and six recorded digest strings. It is
not reconstructed raw browser evidence. Its 0.1-ms values recompute the common
component medians without access to the deleted scratch output. Do not synthesize
unrounded clocks or the missing 88-row manifest from it.

```sh
PYTHONPATH=$PWD .venv/bin/python3 benchmarks/deliverable_first_open.py --summarize-history
```

The successor harness records exact byte identities and snapshots for the original
fixture, original artifact owner, Python driver and bundler, plus generated entry,
emitted JS, individual CSS outputs and the combined CSS string actually supplied
to the page. Its path-free `tofu.first-open-build/v2` manifest uses project-relative
or content-addressed external input IDs. Original sources and generated outputs
remain under the caller's new output directory in `build-receipt/`; inputs.json is
the sanitized receipt, not an absolute scratch-path manifest. Generated imports use
the bundler's existing project-root alias. These are **future-run receipts**, not
retroactive certification of the 18 historical samples. A targeted minimal Vite
fixture verifies receipt generation without opening Chromium or the application.

The abfe successor fixes relative `--output` handling by normalizing the caller's
output location before constructing build paths. Builds and raw absolute-path
manifests stay in disposable staging; only the completed sanitized `build-receipt/`
directory is published by a same-filesystem atomic rename. A nonempty existing
receipt is not overwritten. Build, sanitization or publication failure cleans the
staging directory and preserves an earlier valid receipt. Atomicity covers this
receipt directory, not an entire browser run or power-loss/fsync durability.
Relative CLI tests cover repository and non-repository working directories and
check generated-entry identity plus path-free receipts. Forced failures after raw
manifest creation cover absent and previously valid evidence destinations. These
are receipt-only tests; no Chromium run or new timing observations were made.

## Reproduce the method on a new run (not these historical clocks)

```sh
PYTHONPATH=$PWD .venv/bin/python3 benchmarks/deliverable_first_open.py --output "$TMPDIR/first-open-local"
```

Use a new output directory for each new run so its build snapshots remain distinct.
The script writes local `evidence.json` (all timestamps, counts, hashes and bundled
input identities) and 18 screenshots into the caller's output directory. It bundles
existing sources into a private temporary entry using the repository's Vite test
adapter. It neither starts nor calls the application. Each sample has a new private
browser context, offline mode, blocked service workers and an abort-all route.
The English catalog is provided in memory before timing. Actual row, report opener,
artifact panel, artifact endpoint facade, Markdown parser/sanitizer/pipeline, dialog
and relevant board/card/panel styles execute in Chromium. The fixture supplies only
the API response port and environment; it does not reimplement those UI owners.

Boundary: **click on an already populated row with modules resident → first
viewport-intersecting nonempty content geometry at rAF**. First opens, not warm
reopens. The opener promise resolves locally: application boot, list fetching,
lazy chunk transfer/evaluation, HTTP/transport/auth/storage/server costs are excluded.
The file arm is specifically a Markdown document artifact, not a binary attachment,
PDF/deck page preview or iframe. There is no source-turn redirect, download, script
consent, review toggle, file access, shared cache or cross-session state.

Two synthetic fixtures contain 16 / 512 varied sections (headings/lists, tables,
quotes and inline code), 1,399 / 42,768 UTF-8 bytes. Each report and document arm gets
identical source. Assertions conserve 6/171 headings, 5/171 tables and 5/170 quotes,
the final sentinel, exactly one parse, equal rendered-text hashes across arms and
repeats. The historical samples reported 124 / 3,932 descendant elements for
the small / large fixtures; these numeric counts are observations, not asserted
invariants. The runner checks only equal document-arm descendant counts across
orders and repeats within each size; it neither pins those numeric values nor
asserts report-arm descendant counts. Three fresh contexts per arm and size.

Response ordering is fixture-controlled, not a backend latency model:

* Hold metadata until the loading shell has a frame; only metadata may be requested.
* Release metadata; versions and raw body must both become pending.
* `versions-first`: settle versions, verify no body, then release raw.
* `body-first`: release raw, observe content and a subsequent frame opportunity
  while versions remain pending, then release versions.

All 18 samples: **zero browser network requests; reports zero API calls; documents
one metadata + one versions + one raw API-port call**. API-port counts are logical
facade calls, not measured HTTP exchanges. No review or supporting-fact calls.
The response holds include Playwright/observer scheduling; they are explicitly
reported below and are not service latency. Screenshot acquisition is after timing.

`domReady` is a MutationObserver observation of complete content. `visibleFrame`
is an rAF callback after measuring a nonzero viewport-intersecting body rectangle.
`nextFrameOpportunity` is a subsequent rAF, not proof of compositor pixels. A
screenshot confirms eventual appearance, not the exact first pixel timestamp.
No CPU, layout-vs-paint split, screenshot-clock alignment, production telemetry or
persistent runtime instrumentation is claimed. No thresholds or policy knobs added.

## Historical final run: milliseconds, median of three

Each column is independently aggregated; do not sum medians. Markdown time includes
source preparation, parse/sanitization, HTML policy and the existing exact cache.
DOM gap includes insertion/dialog work and observer scheduling; frame gap includes
style/layout and frame scheduling, not separately attributed renderer CPU.

| Bytes | Arm/order | Markdown | Markdown end → DOM observed | DOM → visible frame | Click → visible frame | Click → next frame opportunity |
|---:|---|---:|---:|---:|---:|---:|
| 1,399 | report/local | 16.8 | 6.0 | 0.6 | 24.7 | 31.1 |
| 1,399 | document/versions-first | 17.0 | 0.2 | 4.0 | 43.3 | 47.4 |
| 1,399 | document/body-first | 16.6 | 0.2 | 3.8 | 39.6 | 47.5 |
| 42,768 | report/local | 43.2 | 28.8 | 4.4 | 76.9 | 78.8 |
| 42,768 | document/versions-first | 44.9 | 3.8 | 22.9 | 92.8 | 98.4 |
| 42,768 | document/body-first | 43.4 | 4.0 | 22.5 | 89.4 | 94.9 |

| Size/order | Click → metadata request | Controlled metadata hold | Metadata release → raw request | Controlled raw hold | Raw release → Markdown | Controlled versions hold |
|---|---:|---:|---:|---:|---:|---:|
| small/versions-first | 3.7 | 12.9 | 0.9 | 4.1 | 0.3 | 2.5 |
| small/body-first | 3.8 | 12.3 | 0.8 | 1.8 | 0.2 | 32.3 |
| large/versions-first | 3.7 | 13.0 | 0.7 | 4.0 | 0.4 | 2.3 |
| large/body-first | 3.6 | 12.9 | 1.0 | 1.7 | 0.3 | 80.3 |

### All final component samples (ms)

Preserve the adverse small-report sample: Markdown 110.4 ms, first visible 117.7 ms.
Its cause was not isolated; no outlier was removed. This shared-host observation
prevents any universal small-is-faster or report-always-faster conclusion.

Columns: repeat, size, arm/order, Markdown, Markdown-end→DOM, DOM→frame,
click→frame, click→next-frame.

```text
0 small report/local             16.8  6.7  0.6  24.7  29.7
0 small document/versions-first   17.2  0.2  4.0  43.6  47.4
0 small document/body-first       16.5  0.2  3.8  39.7  47.3
0 large report/local             48.7 30.1  5.0  84.4  86.8
0 large document/versions-first   48.0  4.0 23.2  96.9 103.6
0 large document/body-first       41.9  4.0 22.6  88.0  94.1
1 small report/local            110.4  6.0  0.6 117.7 118.8
1 small document/versions-first   16.7  0.2  4.0  42.5  47.3
1 small document/body-first       17.8  0.2  3.7  39.6  47.5
1 large report/local             43.1 28.8  4.4  76.9  78.8
1 large document/versions-first   44.9  3.8 22.9  92.5  98.4
1 large document/body-first       43.4  3.9 22.4  89.4  94.9
2 small report/local             16.2  5.4  0.5  22.8  31.1
2 small document/versions-first   17.0  0.1  4.2  43.3  47.4
2 small document/body-first       16.6  0.2  3.8  39.5  47.7
2 large report/local             43.2 28.7  4.4  76.9  78.8
2 large document/versions-first   44.3  3.8 22.3  92.8  98.3
2 large document/body-first       44.6  4.0 22.5  90.1  96.1
```

### All document request clocks (ms relative to click)

Each pair is start/release; holds are release minus start. Last columns are
metadata-decoded / loading-shell-frame. Versions and raw overlap after metadata.
This is interval/causal evidence, not disjoint renderer stages: the versions hold
contains time spent obtaining the body's visible-frame observation. Do not add
versions/raw holds, subtract overlapping holds from total time, or reinterpret the
order checks as stage CPU timings. Rounded endpoints may differ by 0.1 ms from a
hold originally calculated before rounding.

| Repeat,size,order | Metadata | Versions | Raw | Metadata decoded | Shell frame |
|---|---|---|---|---:|---:|
| 0 small versions-first | 3.7/16.6 | 17.1/19.9 | 17.4/22.0 | 16.7 | 14.0 |
| 0 small body-first | 3.8/15.8 | 16.6/48.8 | 16.9/18.8 | 15.9 | 14.0 |
| 0 large versions-first | 3.7/16.7 | 17.2/19.5 | 17.4/21.3 | 16.8 | 14.0 |
| 0 large body-first | 3.6/16.5 | 17.0/97.1 | 17.3/19.3 | 16.7 | 14.2 |
| 1 small versions-first | 3.8/16.2 | 17.0/19.5 | 17.2/21.3 | 16.3 | 14.2 |
| 1 small body-first | 3.7/16.1 | 16.6/48.9 | 16.9/17.7 | 16.2 | 14.1 |
| 1 large versions-first | 3.9/15.7 | 16.3/18.6 | 16.5/20.5 | 15.8 | 13.5 |
| 1 large body-first | 3.6/16.7 | 17.4/97.7 | 17.7/19.4 | 17.1 | 14.4 |
| 2 small versions-first | 3.7/17.3 | 17.9/20.0 | 18.2/21.7 | 17.3 | 14.2 |
| 2 small body-first | 3.9/16.2 | 16.7/49.0 | 16.9/18.7 | 16.3 | 14.4 |
| 2 large versions-first | 3.6/17.2 | 17.8/20.0 | 17.9/22.1 | 17.3 | 14.1 |
| 2 large body-first | 3.6/16.3 | 17.1/99.1 | 17.4/18.6 | 16.6 | 14.1 |

## Pinpointed dependency edges, not a production optimization

* Row → opener promise: `frontend/src/features/lead-deliverables/rows.ts:46`;
  production lazy import is `frontend/src/features/coordinator-task-deliverables.ts:68`.
  Acquisition of that chunk is **not measured** by this resident-module fixture.
* Report → existing item body → dialog: `frontend/src/features/lead-deliverables/open.ts:88`
  and `:136`; no report-detail call. One Markdown parse per fresh first open.
* Document → awaited live metadata → raw: `frontend/src/runtime/sections/artifacts.js:309`
  then `:322`, via `:363` and `:371`. This is the exact serial edge observed.
  Metadata determines type and supports current access checks; it is not redundant
  work, and this measurement does not authorize speculative prefetch/removing it.
* Metadata → header → versions is fire-and-forget (`artifacts.js:449`, `:461`),
  not a raw-body prerequisite. Every body-first case displayed content while the
  versions response was still held. No versions-blocks-body bottleneck reproduced.
* Review mount (`artifacts.js:313`) starts closed and fetches only on explicit toggle
  (`frontend/src/rich-response/artifact-review.ts:54`, `:67`). No review/facts read here.
* Format `file` skips raw (`artifacts.js:322`); PPTX preview has additional pages→page
  dependencies in `frontend/src/rich-response/artifact-delivery.ts`. Neither binary
  nor deck timing is covered by these Markdown fixtures.

Rendering is the largest measured non-fixture-wait component for the large body;
report DOM/dialog publication and document layout/frame gaps differ. The measurement
does not isolate those gaps to a particular DOM call or prove a safe production fix.
The actionable handback is **metadata-settlement → content request**; versions are
not on the critical body chain. Leave production unchanged pending a separately
justified change. Existing warm behavior is not reclassified as duplicate work.

Source history inspected before measuring: opener `7a04a68805`, 2026-11-18
00:59:43 +08:00; artifact owner `4009a6b52f`, 2026-11-18 12:11:16 +08:00;
Markdown bridge `91b4c7e205`, 2026-10-22 17:49:43 +08:00.
Final bundle SHA-256 `165403f4aafa54772c0706c43495b3d3533a4caf824064b3e5f9b259e0dc42b7`;
CSS `20b13f9b22b2674efa1cbe89507e4e80a32bfcd798efb282a5cf975a01ba1001`.
Small source `cbb51b397e9d6afe68f4f3213e3bda9f5852d7ce254e7f38748a4b3e0517f25e`;
large source `f221a970b134eb98529b797324d538dc6c368ebcd93284bc60abda9174884bfc`.
Rendered-text hashes respectively
`b3651f20b77218b57851ebf2b862dea9165cd3a29505ca468758464ebb9c886b` and
`2e1efff20fb2ce685ce3de9102393a60c5c90120889340873a30c2318be63c1e`.

Earlier exploratory runs were not production baselines: first attempt failed before
sampling because the harness searched the bundle root rather than manifest CSS
outputs; corrected using the emitted manifest. Two subsequent 18-sample runs passed
before locale/card-style setup was completed. Their click→frame report medians were
22.9/77.2 ms and 22.7/75.3 ms (small/large); do not pool them with the final setup.
This was harness misuse of the documented bundler asset path, not a product defect.

## Acceptance evidence

1. **Historical aggregate reproducibility met; full historical certification remains
   unverified.** All 18 samples, two sizes, both document response orders, reported
   counts and request clocks are retained without old worktree paths. Targeted tests
   compare every aggregate row to the pinned note, independently recompute medians,
   preserve the adverse 117.7-ms sample and check request overlap separately from
   timings. Source/asset gaps listed above cannot be inferred from HEAD. No new
   speed, full-app/chunk/HTTP/backend or physical-pixel claim.
2. **Met for the receipt-only repair:** standalone measurement/aggregate files and
   one targeted receipt test only; no runtime implementation, real file/user data,
   shared docs, access/auth scope, redirects, deployment, network or telemetry changes.
   The earlier **3 Python + 12 Vitest passes are historical**, not rerun evidence
   for this successor. The earlier receipt repair ran **3 targeted tests**. The abfe
   successor runs **7 targeted tests**: the unchanged aggregate/interval and absolute
   build checks, two relative CLI contexts, and two forced-failure cases. No Chromium
   replay or broad suite was run. Historical fixture and aggregate bytes are unchanged.
