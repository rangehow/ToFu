# Immutable history and incremental wire experiment

This prototype freezes exact builtin JSON at explicit append/replace/compact
boundaries and caches canonical projection of closed message groups. Global
validation excludes malformed shapes, duplicate IDs, orphan results and role
merges from reuse. Pending tool batches are recompiled until their results settle.
Exact immutable record content, owner and the actual gateway-policy flag determine
reuse. Caller mutation cannot rewrite a committed snapshot; a foreign owner is
denied. No parent-history chain or process-global prompt cache is retained.

Run from the repository with its Python environment:

```bash
python benchmarks/context_compiler/check.py
export TOFU_BENCHMARK_PYTHON="$VIRTUAL_ENV/bin/python"
export TOFU_CONTEXT_COMPILER_OUTPUT=/tmp/tofu-context-compiler.json
python benchmarks/context_compiler/measure.py
```

The five workloads cover append-only tool history, early result edits, an explicit
summary/suffix replacement, gateway-policy switching and multimodal JSON blocks.
They are sanitizer/serialization experiments, not complete context-domain
admission: provider_projection switches the gateway sanitizer, not Responses or
Anthropic translation; compaction does not exercise its durable authority; media
bytes are fixtures, not owner-scoped acquisition/token accounting. Model budgets,
injection permissions, real producer integration and diagnostic receipt parity
remain required before production adoption.

Each workload has three alternating independent process pairs and 100 measured
operations after 20 warmups. RSS comes from separate workers; raw timings and
final payload identities are retained. First-use numbers are three diagnostic
observations, not a statistically complete cold-start gate. The reference consumes
raw producer dictionaries; the candidate's revision encoding cost is included.

The cache limits retained source/output payload bytes to the explicitly supplied
budget and evicts old groups. Container overhead is included in measured RSS but
is not charged to that payload budget. Production sizing must include that
overhead and derive defaults from the launch probe. The prototype is deliberately
not installed into the application: observed first-use regressions and the
unverified authority/diagnostic boundaries prevent promotion.

`cold.py --check` verifies first-build factory parity, malformed fallback,
snapshot isolation and foreign-owner rejection. `cold.py --output /tmp/tofu-context-first-build.json`
measures fresh compiler/history construction on every operation, including
record validation/encoding, sanitization, serialization, cache filling and
teardown. It compares the full reference, repeated immutable append and a bulk
tuple construction using the same `Record.capture` admission. The bulk factory
is benchmark-only and does not change the application's history writer.

Defaults are three alternating fresh-process cohorts with 100 observations
after 20 warmups for 257-message text/media histories and a 2,049-message text
history. Memory comes from separate processes and includes fixture/oracle work.
Raw observations, fixture/output digests, source digests and latency/memory PIDs
are retained after each completed cohort. Imports and fixture creation are
outside timing: this is empty-cache first use, not interpreter startup.
`--samples 2 --pairs 1 --case basic` is only a smoke check. Provider translation,
source permissions, token budgets and production revision ownership remain gaps.

`check_provider.py --output /tmp/tofu-context-provider-projection.json` checks
actual request construction and Responses/Anthropic conversion, including
native continuity and checked conversion diagnostics. It uses synthetic opaque
fixtures and no provider I/O or model registration. Current compiler output
loses Responses encrypted reasoning and Anthropic signed thinking blocks; on a
compatible Responses route it also erases the mandatory-continuity rejection.
These are projection regressions, independently of the first-build cost. The
sanitizer-only prototype remains unpromoted; a provider-aware immutable IR must
retain native carriers and exact failure/diagnostic behavior before adoption.

`History.materialize_provider_input(owner_id)` now exposes detached raw source
revisions to the actual body/protocol owners. It retains every captured field;
sanitized debug/wire snapshots remain a different stage and are not provider
input. The native probe checks initial, appended, edited opaque carriers and
summary/suffix replacement on all three protocol/profile fixtures, including
exact conversion failures/loss receipts, caller mutation and foreign-owner
denial. Twelve source-IR projections match the canonical raw-history path.
This repairs the experimental input boundary, not the sanitizer-only compiler's
native projection or performance. No production integration or speed claim is
made; producer ownership, counting, durable compaction and cold costs remain.

**Problem:** debug-wire sanitization removes native carriers before request
preparation can enforce continuity. **Decision:** materialize immutable raw
source revisions for the canonical request owners, with explicit owner checks.
**Alternatives considered:** restoring private fields by output position is
unsafe after message merges/drops; sending them as generic wire fields bypasses
protocol admission. Provider-specific incremental caching remains separate work.

`provider_measure.py --output /tmp/tofu-native-source-ir.json` measures actual
body construction plus checked Responses/Anthropic projection. Raw producer
history is compared with immutable raw-source capture/materialization, retaining
native carriers and exact rejection/loss diagnostics. Three profiles each cover
first capture, growing settled-pair append and native-carrier edits. First use
includes full capture per operation; repeated cases include changed-record
capture and tuple revision costs, with initial history outside timing. No
sanitized compiler output is fed to the provider owner.

Defaults are three alternating independent pairs of 100 observations after
20 warmups. Separate RSS workers execute the same full growing-history cohort;
raw samples, exact output traces, fixture/source identities and worker PIDs are
retained. `--samples 2 --pairs 1 --profile openai --case first` is only a smoke
check. Supplied token counts exclude token admission; durable history producers,
compaction, multimodal acquisition, interpreter startup and provider I/O remain
outside scope. This experiment cannot promote an application history writer.

`native_copy.py --output /tmp/native-copy-check.json` tests a different path:
reuse the canonical builtin-container copier at only the native-sidecar restore
site in actual `build_body`. An isolated function namespace leaves public-field,
extra/intent copying and every protocol owner unchanged; no global monkeypatch
or product default is applied. Checks preserve native payloads, mandatory
continuity rejection/loss receipts, cycles/aliases, caller isolation, successful
custom-hook memo semantics and injected hook failures.

`native_copy_measure.py --output /tmp/native-copy-measure.json` compares raw
producer histories in both arms, without adding immutable capture/materialization.
Every assistant turn carries synthetic opaque native state. Three profiles use
three alternating independent pairs of 100 operations after 20 warmups, with
separate memory workers, exact trace checks, source/fixture identities and pair
checkpoints. `--samples 2 --pairs 1` is smoke only. Supplied token counts exclude
token admission; ordinary/native-sparse histories, producer integration, cold
interpreter and live cryptographic/provider validation remain outside coverage.
**Problem:** native source IR added duplicate work ahead of an existing detached
request boundary. **Decision:** investigate the already-owned native copy site,
with unchanged canonical conversion and failure behavior. **Alternatives:**
skipping detachment exposes caller mutation; serializing native fields risks
custom-hook/numeric changes; optimizing another added snapshot cannot remove
the extra boundary. This experiment needs measured gains before promotion.
The source-IR projection matrix's 5,400 observations/108 independent processes
preserve native results but show latency/CPU regressions. Its original artifact
identity and per-pair metrics remain in
`system_candidate_rejections_20261008.json`.

The native-copy comparison also includes sparse state (only the last assistant)
and no native state, on all three profiles. `--density heavy|sparse|none` selects
one control; the default measures all nine profile/density combinations. The
compatible profile rejects only fixtures containing mandatory encrypted state;
the no-state control must remain representable. Both variants retain identical
raw inputs and actual conversion receipts. Nine boundary smoke checks match;
formal gain/non-regression checks on every density are required before promotion.

`conversion_copy.py` isolates the next copy boundary: checked protocol conversion
detaches the entire already-built body before validation/translation. Private
checked-wrapper namespaces replace only that detachment with the canonical
builtin-wire copier; body construction and converter validation/loss receipts
remain unchanged. Checks cover nine protocol/density fixtures, detached output,
cycles/aliases, custom hooks, injected copy failure and fixed-code rejection.
`conversion_copy_measure.py --output /tmp/conversion-copy.json` reuses the actual
raw-history worker and paired latency/CPU/separate-memory accounting for all nine
cases. `--profile compatible --density none --samples 2 --pairs 1` is smoke only.
**Problem:** specializing one native sidecar gave only modest measured gains.
**Decision:** measure whole-body checked-conversion copying as a separate site.
**Alternatives:** skipping detachment would expose live input to converter
mutation; combining both optimizations would obscure the source of any gain.
No product module/global or protocol failure semantics are replaced.

The initial full checked-copy comparison
retains 5,400 observations/108 independent workers. Most P50 cases improve5-9%,
but adverse CPU/latency pairs and the primary threshold block promotion. This
version timed recursive diagnostic `dataclasses.asdict` output copying too.
The next accounting revision exposes canonical dataclass fields shallowly in
both arms; the actual converter still detaches input and the worker serializes
and compares every output trace after timing. It removes benchmark-only cloning,
not a production ownership boundary. Keep the initial result and measure the
revised source identity independently before any adoption conclusion.

The shallow-receipt comparison
preserves all5,400 traces in108 independent workers. Most P50 improves6-9% and
RSS remains neutral, but two no-native controls regress P99 by28%/59% and the
primary10% gate is not consistently met. Both full results remain unpromoted;
unavailable GC/host provenance does not justify deleting adverse observations.

**Final token admission — Problem:** earlier copy comparisons supply an input
count and exclude the actual final adapted-body safety guard. **Decision:**
`admission_project.py` retains canonical request construction, then runs real
`enforce_body_input_limit` before checked protocol conversion. The final guard
recounts with the actual local tokenizer and clamps against the declared window;
its admitted integer is included in the exact receipt trace. Preliminary build
counts remain a fixture hint and cannot bypass final admission. **Alternative
rejected:** accepting the hint as content identity would miss in-place changes.
The check verifies nine protocol/density cases, detached results, actual tiktoken
and stale-count mutation refusal. `admission_measure.py` uses independent
latency/full-cohort RSS workers and records tokenizer versions. Run the small
cohort before full pairs. This adds final admission cost, not durable producers,
initial prompt accounting, media acquisition, compaction or full-context proof.

The final-admission comparison verifies 5,400 exact count/payload/receipt
observations in 108 distinct latency/memory workers. P50 gains are mostly
1–5%; no case consistently reaches 10% P50/CPU gain, and tail/control guards
also fail. `system_context_final_admission_20261008.json`
retains every ratio, raw/source/fixture witness and actual tokenizer versions.
This copy configuration remains unpromoted; final local counting does not
close the other producer, compaction, media, cold or full-context boundaries.
# Native settled-history producer comparison

`check_native_producer.py --engine /absolute/path/to/tofu-db` commits 257 settled
Turns through the real semantic writer into an owned native authority. An
owner-bound conversation repository supplies a complete view to the actual
conversation message producer, canonical body builder, local final token
admission and checked protocol adapters. Nine protocol/native-density cases
verify native opaque continuity survives persistence, exact payload/rejection
receipts, source/caller immutability, foreign-owner denial and close/reopen.

Run:

```sh
TOFU_CONTEXT_NATIVE_ENGINE=/absolute/path/to/tofu-db python \
  benchmarks/context_compiler/native_producer_measure.py --samples 100 --pairs 3 \
  --output /tmp/context-native-producer.json
```

Each arm rereads and transforms the
same committed static history on every operation. Population is outside timing;
only checked-conversion copying differs. The tested binary must have its shared
library alongside it and its compiled source identity must match this checkout.
Latency/memory workers and both arms must match native build/library, committed
Turn count, tokenizer version and output witnesses. Peak RSS includes imports,
population, preflight and reopen. Reduced runs are pilots. Static native reads
do not certify append/edit workloads, source acquisition/composition, actual
compaction settlement, media resolution, provider I/O or complete context scope.

`mutations.py` binds the canonical Turn append/edit facade and atomic compaction
store adapter to an explicitly owned native client. Public projections do not
expose provider-private sidecars; edits follow the real public Turn boundary.
`check_mutations.py` verifies committed additions, edited text, stale edit
rejection, compaction CAS denial without structural mutation, opening-anchor
and retained-order preservation, reopen, exact token/protocol receipts and an
actual generated 64×64 inline PNG. Synthetic signatures/encrypted carriers
prove structural continuity, not live provider validity.

Problem: static producer measurements omit source revision costs. Decision:
`mutations_measure.py` includes real append/edit/atomic-fold operations before
owner-bound reads, message construction, final token admission and checked
protocol conversion. Rejected alternatives: copying an edited list omits the
authority; replacing history bypasses narrow atomic commands; timing supplied
token counts omits final admission. Summary text is supplied deterministically;
model summary generation and external media/provider acquisition are excluded.

Run the native check first, then a two-sample/one-pair CLI pilot using
`--engine /path/to/tofu-db --output-prefix /tmp/context-mutations-pilot`; default
formal cohorts use 100 samples and three independent pairs per declared case.
Provider projection is one batch of OpenAI/compatible/Anthropic native histories;
other cases use one history. Memory workers warm/dispose a separate authority
before their RSS baseline, run the full matched cohort, close/join all owned
authorities and collect before measuring positive retained RSS growth. This
is one disposal interval, not long-run leak proof. Record raw wall/CPU vectors,
source/binary/tokenizer identities, final counts/history sizes and exact traces.
Missing domains keep the whole-system goal incomplete; no defaults change.

`tool_argument_measure.py --output /tmp/tofu-tool-arguments.json` runs three
alternating independent pairs of 100 operations after 20 warmups for five tool
argument shapes in each checked Anthropic/Responses adapter. The reference validator
is extracted from revision `041035db5`; only argument validation differs. Each
latency arm has a separate full-cohort peak-RSS worker. Exact ConversionResult
traces and unchanged input digests are checked outside latency timing. The source
witnesses cover the validator, adapters and driver. `--samples 2 --pairs 1` is a
smoke only. Imports, fixture creation and oracle hashing are outside timing; RSS
includes oracle work. No retained-memory, launch-capacity, token-admission, HTTP,
durable-history or inference claim follows from this scoped comparison.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
