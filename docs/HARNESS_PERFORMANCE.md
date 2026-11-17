# Harness performance

What the agent harness itself costs per model round, measured with the model
removed, and how that compares with the reference harnesses (codex,
claude-code, opencode, pi). Owner of the method: `benchmarks/harness_perf/`.
This document is the current baseline, the hotspot list and the targets that
optimization work is judged against.

## Method

`python3 benchmarks/harness_perf/run.py [--smoke] [--references all]`

- **Model removed.** `stub_provider.py` answers every request at once (with
  `TCP_NODELAY`, as provider front ends send) from a
  script on all three wire faces Tofu and the references speak (Chat
  Completions, Responses, Anthropic Messages). A marker in the user message
  (`[harness-perf scenario=S rounds=N session=ID]`) selects the script; round
  k is the number of tool calls already in the history. The stub records each
  request's monotonic arrival and finish, body bytes, rendered prompt and its
  common prefix with the same session's previous request.
- **Tofu as deployed.** The driver boots, in a private 0700 root on local
  disk, the native TofuDB engine (pinned copy of `$TOFUDB_BIN`), the execution
  host through the production `ExecutionService`, and `server.py` as the API
  host (`TOFU_PROCESS_ROLE=api`, `TOFU_RUN_SERVER=1`, free ports). The owner's
  only model route is an Anthropic connection to the stub. Turns enter through
  `POST /api/v4/conversations/<id>/turns`, as the browser sends them. The live
  store, the running Tofu and its manager are never touched; the run stops
  every process it started.
- **Scenarios** (each harness gets the same script; tool arguments vary per
  round so no loop breaker sees a repeated call):

  | scenario | rounds | tool per round |
  |---|---|---|
  | `chat` | 0 | none: one request, answer `done` |
  | `true` | 1 | shell tool running `true` (Tofu `run_command`, codex `exec_command`, claude `Bash`, opencode/pi `bash`) |
  | `noop` | N | plan tool where offered (codex `update_plan`, claude `TaskCreate`, opencode `todowrite`); otherwise a one-line file read (Tofu `read_files`: its `todo_write` is Tool-Search deferred; pi `read`) |
  | `shell` | N | shell tool running `echo r<k>` |

  `true` is one round because Tofu's loop breaker stops a repeated no-op
  command by design.
- **Metrics.** TTFR: user message to first provider request (Tofu: from the
  HTTP POST; references: from process launch, so their figure includes CLI
  start-up). Round overhead: provider response finished to next provider
  request; it contains the whole tool round trip. Split into the first tool
  round of a Turn and steady state. Settle: last response finished to the
  attempt's durable `settledAt` (references: process exit). Request bytes,
  approximate tokens (chars/4), growth per round, prefix ratio (common prefix
  over the previous request; 1.0 is append-only, a full prompt-cache hit) and
  prefix breaks. RSS and CPU of the API host, execution host and engine at idle
  and under N concurrent sessions; CPU per model request. Cold start of
  storage, execution host and API host (full mode restarts the API host four
  more times). Tofu's own `[TurnTraceSummary]` split and harness intervention
  log markers per phase.
- **Profiles.** py-spy (100 Hz, non-idle threads) of both hosts over the
  sequential phases and the concurrent phase: raw folded stacks and flamegraph
  SVGs beside the report, plus stage attribution (`STAGES` in `run.py`).
- **Shapes.** Smoke: 3 chat, 3 `true`, 1×8-round `noop`/`shell`, 4 concurrent
  sessions, 1 run per reference scenario. Full: 10 chat, 10 `true`, 3×20-round
  `noop`/`shell`, 20 concurrent sessions × 2 Turns × 20 rounds, 3 runs per
  reference scenario. These are measurement design, not product settings.
- **Outputs.** `<out>/<run-id>/report.{json,md}`, `*.folded`, `*.svg`
  (default `output/harness_perf/`).
- **CI smoke.** `--smoke` takes about 35 s for Tofu alone and about 60 s with
  `--references all` on this host (requires `$TOFUDB_BIN`; references require
  the evaluation artifacts listed in `references.py`). The pure parts are
  specified by `tests/meta/test_harness_perf_measure.py`.

Reference harness set-up (`references.py`): versions from the
takeover-smoke-20261117 artifacts (codex 0.148.0, claude-code 2.1.207,
opencode 1.19.0, pi 1.4.0), private HOME, all approvals bypassed, every
non-loopback connection refused through a dead proxy (`127.0.0.1:9`).
Codex background calls to github.com/chatgpt.com and opencode's npm registry
call fail at once; no run waits on a network timeout. All four could be
pointed at the stub; none needed its own fake-provider mode.

## Baseline (2026-11-17)

Run `baseline-20261117` (full; suite at `d960d30487`, product at
`68e87e704b`), host `codelab-prod-617731514-89728106-9h7mn`: 180 CPUs (`os.cpu_count()` 58
visible to the run), load average 25.7 at start and 18.8 at end, the live Tofu
running beside it. Artifacts:
`output/harness_perf/baseline-20261117/` in the canonical checkout. Values are
median / p95 (n). Shared-host clocks vary by up to 2x between runs; compare
fresh alternating runs, not single numbers.

### Per round, one session

| metric | Tofu | codex | claude-code | opencode | pi | best |
|---|---|---|---|---|---|---|
| round overhead `noop`, ms | 77.6 / 84.4 (60) | **37.7** / 40.7 (60) | 50.1 / 62.7 | 74.6 / 79.3 | 43.9 / 45.7 | codex |
| round overhead `shell` (`echo`), ms | 89.3 / 535 (60) | 68.0 / 76.7 | 61.8 / 89.5 | 48.9 / 64.7 | **47.9** / 50.0 | pi |
| tool round trip `run_command true`, ms | 285 / 315 (10) | 189 / 189 (3) | 167 / 168 | 112 / 125 | **21.4** / 21.6 | pi |
| first tool round of a Turn (`shell`), ms | 83.1 / 84.6 (3) | | | | | |
| TTFR, ms | 102 / 120 (10) from POST | **170** / 260 from launch | 444 / 514 | 1965 / 2042 | 384 / 386 | (not comparable: Tofu is a warm server) |
| settle after last response, ms | **14.3** / 17.3 (10) | 44 (exit) | 70 | 111 | 31 | Tofu |
| CPU per model request, ms | 113 exec host + 51 engine + 2 API (20 sessions) | 84 | 61 | 264 | **34** | pi (reference figures include process start-up) |

### Request size and prompt-cache prefix

| metric | Tofu | codex | claude-code | opencode | pi |
|---|---|---|---|---|---|
| round-0 body, bytes | 71,023 (chat) – 76,613 | 42,482 | 80,655 | 31,122 | **6,481** |
| round-0 prompt, ≈tokens | 17,400 – 18,800 | | | | |
| growth per tool round, bytes | 313 (`shell`/`noop`), 887 (`true`) | 466 – 528 | 341 – 407 | 282 – 428 | **282 – 285** |
| prefix ratio (median) | 1.0 | 1.0 | 1.0 (0.905 `true`) | 1.0 | 1.0 |
| prefix breaks | **0 of 930** compared requests | 0 | 3 – 12 per 60 | 0 | 0 |

Tofu's request is append-only in every recorded round: no prompt-cache
breaks.

### Concurrency (20 sessions, `shell`, 2 Turns × 20 rounds each)

| metric | 1 session | 20 sessions |
|---|---|---|
| round overhead, ms | 89.3 / 535 | **2,029 / 2,866** (800) |
| TTFR, ms | 125 | 2,479 / 3,714 (40) |
| settle, ms | 25 | 173 / 562 |
| execution host CPU | | **1.03 cores** (one interpreter, saturated) |

References run one process per session, so their per-round overhead does not
depend on the number of sessions.

### Resources and start-up

| | API host | execution host | engine |
|---|---|---|---|
| idle RSS, MB | 132 | 213 | 31 |
| idle CPU, cores | 0.039 | 0.002 | 0.005 |
| 20 sessions RSS mean / max, MB | 134 / 134 | 623 / 886 | **1,106 / 1,856** |
| 20 sessions CPU, cores | 0.021 | 1.029 | 0.467 |
| cold start to ready, s | 2.2 – 4.2 (5 starts; median 2.5) | 14.4 | 0.7 |

Reference peak RSS per session: pi 123–139 MB, codex 137–166 MB, claude-code
293 MB, opencode 609–673 MB. Reference time to first request from launch is
in the TTFR row.

Tofu's own clock (`[TurnTraceSummary]` medians per Turn, 21-round `shell`):
total 3,137 ms = llm 519 + tool 1,830 + orchestration 843 (round prep 393,
round settle 325, start-up 75). The `llm` share is harness time too: the stub
answers in under 1 ms.

## Hotspots (ranked)

Evidence: `concurrent20-execution_host.folded` (8,300 samples) and
`sequential-execution_host.folded` (1,220 samples) in the baseline directory;
shares are of non-idle samples, self time unless marked inclusive.

1. **Throughput ceiling of one execution interpreter.** All sessions run in
   the execution host, which spends 113 ms of CPU per model request (plus 51 ms
   in the engine). At 1.03 cores that caps the host at about 9 model rounds per
   second, so 20 sessions see 2.0 s per round instead of 0.09 s. Every item
   below is per-round CPU that this ceiling multiplies.
   Addressed by execution shards: see "Execution shards and `run_command`".
2. **Synchronous storage round trips on the round path.**
   `StorageV2Client._request_once` (`lib/storage/v2_client.py:604`) 21.3 % self
   under load (11.2 % sequential); `wait_for_persistence`
   (`lib/tasks_pkg/persistence_wait.py`) 20.6 % inclusive: a round waits for
   its task events to commit before continuing.
3. **Event delivery coordinator lock.** `EventDeliveryCoordinator.submit`
   (`lib/tasks_pkg/event_delivery.py:342`, entering the shared `condition`)
   12.1 %, plus `_run`, `_await_target`, `_await_ready` 6.2 %, only under
   concurrency: one condition serializes every lane's submissions.
4. **Whole-history hashing and re-encoding every round.** `span_hash`
   (`lib/llm_sanitize/lineage.py:32`) 3.9 %, `block_content_hash`
   (`lib/llm_sanitize/context_sections.py:33`) 2.2 %, `wire_fingerprint._md5`
   and `_strip_cache_control` (`lib/tasks_pkg/wire_fingerprint.py:96,729`)
   2.8 %, `lib/llm/request.py` `_encode`/`_detached_json` 4.2 % and tokenizer
   `_dump_tool_schemas`/`count_text` 3.1 % sequential. Stage attribution:
   request assembly 27.5 %, context build 5.6–11 %. Work grows with history
   length, while each round appends about 313 bytes.
5. **Settled-row copies.** `_SharingCopy.copy` and `_keep_alive_during_copy`
   (`lib/conversation_sync/settled_rows.py:565–689`) 1.9 % under load, 7.3 %
   sequential.
6. **`run_command true` tool time.** 226 ms of `toolMs` for one `true` call,
   against 21 ms in pi, and every `true` Turn logs a `Tool-progress correction`
   (explicit no-op). `echo` rounds cost 83–90 ms.
   The 200 ms idle poll tick is gone (281-286 -> 81-83 ms round trip); see
   "Execution shards and `run_command`".
7. **Engine memory under load.** Engine RSS grows from 31 MB to 1.1 GB mean
   (1.86 GB max) over 40 Turns × 21 rounds.
8. **Execution host cold start.** 14.4 s to ready, against 0.17 s (codex) to
   2 s (opencode) to a reference's first request.

`SUSPICIOUS COMPLETION` warnings in the intervention counts come from the
stub's two-character final answer and do not change the request sequence.
Every scenario reached its scripted final round with no extra requests.

## Targets

"Top-tier" is the best reference per metric, measured by this suite on the
same host in the same run. A target is met when Tofu's median in a fresh
full run is at or below the figure (p95 within 2x of it), with the comparison
re-run in the same session to cancel host load.

| metric | baseline | target (source) |
|---|---|---|
| steady round overhead, `noop` | 77.6 ms | ≤ 3 ms (pi, since the stub fix; codex 38 ms) |
| steady round overhead, `shell` | 89.3 ms, p95 535 ms | ≤ 7 ms (pi, since the stub fix) |
| tool round trip, `run_command true` | 285 ms | ≤ 21 ms (pi) |
| round overhead at 20 sessions | 2,029 ms | ≤ 1.5x the one-session figure (references: flat) |
| CPU per model request (all Tofu processes) | 166 ms | ≤ 34 ms (pi) |
| settle after last response | 14.3 ms | keep ≤ 31 ms (Tofu already best) |
| round-0 request | 71–77 KB | ≤ 31 KB (opencode, the smallest full coding toolset); pi's 6.5 KB has 4 tools |
| growth per tool round | 313 B | ≤ 285 B (pi) |
| prefix breaks | 0 | stay 0 |
| idle RSS, all processes | 376 MB | ≤ 300 MB (claude-code single process) |
| execution host cold start | 14.4 s | ≤ 2 s (opencode, the slowest reference to first request) |

## Agent-loop round overhead (in-process)

### Method

`python3 benchmarks/agent_round_overhead.py --rounds N [--json F]`

- One real storage-free `tofu_agent` session (`AgentRuntime.local`): tool
  assembly, context composition, request building and sealing, wire
  projection, stream parsing, argument normalization, gateway dispatch,
  result settlement, segment mirror, event fan-out. Storage, push, the API host
  and the execution-host boundary are not in it.
- A loopback OpenAI-compatible provider answers at once and stamps request
  arrival and response flush. Round k calls `read_files` on its own one-line
  file (distinct arguments, so no loop breaker fires); round N answers text.
- **Round gap**: response flushed to next request arrival. It is the whole
  no-op tool round trip plus the next request's assembly: pure harness time.
- **Prefix**: whether every request's `tools` and system prompt are
  byte-identical to the first.
- Arms run in fresh alternating processes (ABAB) with `PYTHONPATH` set to an
  exported tree; the report prints `lib.__file__`.
- Profiles: py-spy (`record -f raw|speedscope`), because cProfile sees only
  the calling thread and the loop runs on a task worker thread.

Component checks: `benchmarks/mcp_live_schema_projection.py` (gateway catalog
projection with 150 MCP + 60 native tools) and the existing
`benchmarks/tool_dispatch_overhead.py` (pipeline only, one call at a time).

### Results (2026-11-17)

Arm A: shared branch `7ac8d17f9a`. Arm B: `ee5c3be46f` (A plus the changes
below). Same host, alternating fresh processes, every session prefix-stable.
Values are pooled over all sessions. Artifacts and py-spy profiles:
`output/harness_perf/abab3/` and `output/harness_perf/*.folded|*.speedscope.json`
in the canonical checkout.

| workload | arm A median / p95 (n) | arm B median / p95 (n) | change |
|---|---|---|---|
| round gap, 30-round session (10 pairs) | 18.92 / 21.97 ms (300) | **16.42 / 18.29 ms** (300) | −13 % / −17 % |
| round gap, 200-round session (4 pairs) | 29.69 / 42.88 ms (800) | **26.10 / 36.06 ms** (800) | −12 % / −16 % |
| `_executable_catalog`, 150 MCP tools (6 pairs × 2,000 calls) | 253 / 260 µs | **4.2 / 4.4 µs** | −98 % |
| pipeline only, `read_files` p50 (6 pairs × 40) | 4.57 ms | 4.48 ms | within noise |

Per-session medians did not overlap (A 18.6–20.0 ms, B 16.1–17.2 ms at 30
rounds). Steady-state loop samples for the same 200-round session fell from
10,820 to 8,911 before the last two changes. The pipeline-only component is
unchanged: the gains are in round assembly and settlement around dispatch.

Deployment suite (`benchmarks/harness_perf/run.py --smoke`, both hosts and
native storage), three ABAB pairs, arm A `76fa978731`, arm B `a6b200bfb3`,
medians of each run (n = 7 steady rounds per run):

| metric | arm A runs | arm B runs |
|---|---|---|
| steady round overhead `noop`, ms | 68.0, 71.7, 75.0 | 67.2, 66.6, 72.6 |
| steady round overhead `shell`, ms | 85.3, 93.0, 93.8 | 85.7, 84.9, 100.6 |
| tool round trip `run_command true`, ms | 283.8, 281.6, 283.1 | 283.4, 284.5, 295.4 |
| chat TTFR, ms | 93.9, 96.5, 99.2 | 187.0 (one 1.5 s outlier), 107.6, 106.7 |
| round-0 body, bytes / growth per round | 70,084 / 312 | 70,084 / 312 |
| prefix breaks | 0 | 0 |

At the deployment level the in-process saving (about 2.5 ms per round) is
small beside storage round trips and event delivery, so the smoke shape does
not resolve it; request bytes are identical, as intended. The assembly-time
settling added by changes 2 and 3 costs about 2–3 ms once per Turn (measured
directly); the higher arm-B chat TTFR in two smoke runs is not explained by it
and is not resolved at n = 3. Artifacts: `output/harness_perf/suite_abab/`.

### Changes

Each is an exact cache with explicit invalidation or a removed redundant
copy; none adds a setting, threshold or bound.

1. **Live MCP schema projection.** `tool_gateway._executable_catalog` stamps
   `(bridge, tool-catalog generation, catalog object)` on the task and skips
   the per-call re-projection while all three are unchanged. The bridge's
   generation (`MCPBridge.tool_catalog_generation`) advances on every tool-index
   or grant change; a replaced catalog list is a new key.
2. **Prefetch occurrence identity.** It hashed the whole task config (about
   151 KB, mostly assembled contract documents and search text) on every tool
   call. Assembly now settles those two maps (deep-immutable, byte-identical
   JSON; every writer already replaced them copy-on-write) and the identity
   stands in each settled value by its canonical digest, memoized on the row and
   tagged so it can never equal a plain value.
3. **Settled wire tool surface.** The final `project_tool_wire` surface is
   settled, reusing the task's previous rows wherever interchangeable
   (`settle_items(previous=...)`), and initial assembly settles the schemas it
   hands every round. Unchanged schemas keep their identity across rounds, so
   `detach_input` shares them and the consumers (`seal_request` field encode,
   `semantic_request_id`, schema token text and count, schema fingerprints,
   wire byte region, preflight verdicts) memoize on the exact run of rows
   through `settled_rows.rows_memo`. Wire bytes are unchanged.
4. **Segment mirror.** Rebuilt from every tool round twice per round and then
   deep-compared with the previous mirror. A settled round's segments are a
   pure function of the row, its position and its batch identity; they are
   built and settled once in the row's memo slot behind those guards.
5. **`spawn_agents` wire schema.** The local multi-agent projection copied the
   whole schema every round to drop one key; it now copies only that path.

### Prompt-cache prefix

`tests/tasks_pkg/test_request_prefix_stability.py` drives the same scripted
session on the OpenAI and the Anthropic wire and diffs consecutive requests:
tools and system bytes identical, every earlier message byte-identical at the
same index (Anthropic messages compared without `cache_control`, since the
conversation-tail breakpoint moves to the newest message by design; system and
tool breakpoints never move). The output budget (`max_tokens`) follows the
context left and is outside every provider's cache key. No instability was
found.

### Remaining per-round cost (steady state, arm B)

Shares of loop-thread samples: token counting for admission and compaction
about 10 %, context composer refresh (`refresh_programmatic_inputs`) about 4 %,
messages snapshot event 4 %, `_seal_context_lineage` (a sorted digest of the
whole wire body) 3 %, tool settlement and freshness checks around reuse 5 %.
The deployment suite's storage round trips and event-delivery lock are outside
this in-process measurement.

### Alternatives considered

- **Process-global content cache of tool schemas.** Rejected: it would need
  the very serialization it saves to build a key. Settled rows give identity
  that is exact by construction (any change is a copy-on-write replacement).
- **Caching the MCP projection by time or call count.** Rejected: a knob, and
  inexact across a server restart; the bridge generation is exact.
- **Dropping config from the prefetch identity.** Rejected: it would change
  which changes invalidate a prefetched read. Digesting frozen values keeps
  identity equality exactly as before.

## Execution shards and `run_command` (2026-11-17)

Hotspots 1 and 6. Owners: `serverctl_pkg/execution_shards.py` (decision record
in its header) and `lib/project_mod/run_command.py`.

### Changes

- **Concurrency ceiling (hotspot 1).** The resident execution host starts
  `execution_shard` processes on demand: one per job it had to claim beside
  another. A shard runs the host's own frozen source generation (exported
  once, served by a stdlib bootstrap), claims the same durable jobs with
  process-bound claims, runs one job at a time, imports the modules the host
  had imported before it admits work, and retires itself after
  `TOFU_EXECUTOR_IDLE_SECONDS` idle. The host defers a claim to an idle shard
  while it is busy and otherwise claims itself, so no job waits for capacity;
  command controls fan out from the host, and shards relay MCP calls to the
  host's one server set (docs/modules/tools_execution.md, "MCP servers run
  once per execution plane"). No count,
  CPU probe or new setting exists. The per-interpreter serialization points
  of hotspots 2-3 (one storage client, one delivery coordinator, one GIL for
  every session) are per process now; their per-round cost is unchanged.
- **`run_command true` (hotspot 6).** 200 of its 226 ms were one idle
  `safe_select_pipes([])`: once both pipes reached EOF the interactive runner
  slept out the whole 0.2 s poll tick before observing the exit. It now waits
  on the process exit itself (returns at exit); abort, interrupt and stdin
  sensing keep the same tick. Direct `tool_run_command('true')` with a stdin
  callback: 277 ms -> 7 ms.

### ABAB (full mode, `--api-restarts 0`)

Arm A: product at `76fa978731` (before); arm B: `d382d8f255` (this change,
merged with the shared branch); both measured by the same harness (B's
`run.py`, which also counts shard CPU/RSS and splits the concurrent phase by
Turn). Runs alternated A, B, A, B, A, B on this host with load average 36-231
(other tenants); artifacts in `output/harness_perf/shards-abab-20261117/`.
Values are median / p95 per run; `true` has n=10 per run (30 per arm), every
other row n≥60 per run.

| metric | A r1 | A r2 | A r3 | B r1 | B r2 | B r3 |
|---|---|---|---|---|---|---|
| round overhead `run_command true`, ms | 285 / 313 | 283 / 337 | 286 / 314 | 171 / 665 | **83** / 176 | **81** / 104 |
| round overhead `shell`, 1 session, ms | 95 / 304 | 89 / 339 | 117 / 771 | 100 / 697 | 97 / 192 | 86 / 131 |
| round overhead `noop`, 1 session, ms | 75 / 84 | 76 / 84 | 75 / 90 | 100 / 663 | 81 / 123 | 78 / 128 |
| round overhead, 20 sessions, all (n=800), ms | 3,211 / 8,637 | 2,063 / 2,841 | 6,451 / 8,849 | **221** / 4,643 | **125** / 1,397 | **171** / 2,911 |
| ... first Turn of each session, ms | 2,100 / 2,651 | 2,052 / 2,657 | 6,402 / 8,735 | 1,348 / 5,003 | 1,123 / 1,437 | 1,080 / 3,713 |
| ... second Turn of each session, ms | 6,967 / 9,239 | 2,065 / 2,937 | 6,515 / 8,917 | **108** / 733 | **99** / 253 | **127** / 893 |
| TTFR second Turn, 20 sessions, ms | 2,746 | 2,285 | 3,753 | 471 | 284 | 299 |
| CPU cores, 20 sessions: host + shards | 0.54 + 0 | 1.03 + 0 | 0.38 + 0 | 0.44 + 1.17 | 0.95 + 2.64 | 0.63 + 1.83 |
| CPU ms per model request: host + shards + engine | 123 + 4 + 71 | 114 + 3 + 53 | 122 + 4 + 70 | 36 + 96 + 40 | 34 + 94 + 38 | 33 + 97 + 40 |
| RSS max, 20 sessions: host / all shards, MB | 880 / - | 890 / - | 898 / - | 400 / 4,202 | 383 / 4,495 | 392 / 4,271 |

Reading:

- At 20 sessions per-round overhead falls from 2.1-6.5 s to 125-221 ms
  (whole phase) and to 99-127 ms in the second Turn, 1.0-1.3x the one-session
  `shell` figure of the same run: the "≤ 1.5x the one-session figure" target
  is met once shards exist. Arm A's second Turn is as slow as its first: one
  interpreter serves all 20 sessions.
- Jobs never migrate. The first Turn of a 20-session burst starts before any
  shard exists, so those Turns run on the host as before (about 2 s per
  round); the shards started for them (ready about 5 s later) serve every
  later Turn. (Since superseded: claims beyond the primary's own job are now
  placed on shards before entry; see "Burst placement, forked shards and
  settled tool translation".) Under heavy host load the burst's 20 shard boots compete for CPU
  (r1-B first-Turn p95 5.0 s).
- One session is unchanged within noise (`shell`, `noop`); `run_command true`
  drops from 285 ms to 81-83 ms, the plain round overhead: the tool itself now
  costs about 25 ms of `toolMs` (was 226-228).
- CPU per request is unchanged in total (about 130-135 ms in Python, now spread
  over processes; hotspots 2-5 remain per-round work). Each shard holds about
  210-230 MB while serving; idle shards retire, so idle RSS is the host's.

Alternatives considered: worker processes sized from the CPU count
(forbidden: hardware never limits work); forking the multi-threaded host
(inherits held locks); shards importing the checkout (a later merge would
split one generation across processes); deferring the burst's first jobs to
starting shards (adds about 5 s TTFR to every burst arrival, and the number of
waiting jobs is unknown without a sizing heuristic); moving only event
persistence to another process (at most the 21 % of host CPU it uses).

## Burst placement, forked shards and settled tool translation (2026-11-17)

Follow-up to "Execution shards and `run_command`". Owners:
`serverctl_pkg/execution_shards.py` (decision record in its header),
`serverctl_pkg/execution_shard_boot.py`, `lib/durable_worker/service.py`,
`lib/llm/anthropic_outbound/_to_anthropic.py`, `lib/llm/cache.py`.

### Root cause of the slow first Turn

py-spy of the 20-session phase on the merged tree (`eda37e9714e8`): every
first-Turn job ran on the primary, because jobs never migrate and the shards
started for them took 9-15 s to boot under host load (a fresh interpreter,
`import server`, re-importing the primary's modules, full startup phases).
The primary's interpreter was again the one serialization point (event
delivery `submit` lock wait 33 % of loop wall time, `_await_target` 13 %).
Second Turns were fast only because the shards existed by then.

### Changes

1. **Placement at the claim boundary.** A claim the busy primary owns is
   decided before any handler effect (`DurableWorkerService` claim gate
   `place`, under the claim's lease monitor). If the primary's own job ended
   meanwhile it enters the claim; otherwise the claim is parked, one shard is
   started for it, and the first shard to report idle is reserved for it while
   the unentered claim is handed back to the queue (deferral, available now)
   for that shard to claim. A refused or stopping pool enters parked claims
   itself. No count, window or prewarm number exists.
2. **Shards fork from a preloaded image.** One single-threaded fork server
   (`execution_shard_boot.py --fork-server`) loads the primary's frozen
   generation, imports the application and the primary's modules, refuses to
   fork once any thread exists, and forks each shard with its lease, manager
   and report descriptors (SCM_RIGHTS). The primary's first job starts it; it
   retires after `TOFU_EXECUTOR_IDLE_SECONDS` without work. Shard boot fell
   from 9-15 s to 2-6 s on this host (fork server itself 1.2-4 s).
   Process identities captured at import (`boot_identity.BOOT_ID/PID`,
   `turn_lifecycle`'s attempt dispatch owner) are re-derived after fork, so no
   shard can replay another process's attempt claim.
3. **Settled tool rows survive Anthropic translation.** The cache-marked last
   tool and each translated Anthropic tool row are settled once per settled
   source row, so the sealed encode, byte fingerprint and schema-token memos
   answer later rounds instead of re-serializing the tool surface every
   request (`prepare_request`: `wire_byte_region` 4.5 -> 1.9 ms per request).
   The Claude OAuth cloak renames tools copy-on-write.

### ABAB (full mode, `--api-restarts 0`)

Arm A: `90544031fc` (shared branch with the first shard change merged); arm B:
`bc55214919` (changes 1-3). Fresh private root per run, TofuDB engine built
from this source; runs alternated A, B on this host. r2-B coincided with a
foreign load spike to 250 (it lost sessions to turn timeouts) and is replaced
by r4. `true` n=10 per run (40 per arm across r1-r4), one-session rows n=60,
20-session rows n=400 per Turn. Median / p95, ms. Artifacts:
`output/harness_perf/burst-abab-20261117/`.

| metric | A r1 | A r2 | A r3 | A r4 | B r1 | B r3 | B r4 |
|---|---|---|---|---|---|---|---|
| 20 sessions, first Turn rounds | 1,241 / 1,624 | 1,318 / 1,663 | 1,731 / 2,086 | 1,586 / 2,110 | **116** / 270 | **113** / 168 | **122** / 252 |
| 20 sessions, second Turn rounds | 135 / 282 | 121 / 203 | 102 / 967 | 104 / 165 | 115 / 167 | 108 / 149 | 115 / 181 |
| 20 sessions, all rounds (n=800) | 175 / 1,554 | 147 / 1,609 | 146 / 2,025 | 136 / 1,928 | **115** / 223 | **110** / 157 | **117** / 241 |
| 20 sessions, first Turn TTFR | 2,750 | 2,001 | 1,698 | 1,615 | 9,168 | 1,981 | 2,619 |
| 1 session, `noop` steady rounds | 79 / 133 | 79 / 139 | 72 / 122 | 71 / 142 | 73 / 83 | 70 / 81 | 74 / 129 |
| 1 session, `shell` steady rounds | 127 / 191 | 108 / 224 | 89 / 137 | 93 / 144 | 103 / 156 | 89 / 130 | 89 / 202 |
| `run_command true` round trip | 100 / 237 | 98 / 419 | 83 / 160 | 80 / 141 | 112 / 1,184 | 80 / 143 | 82 / 151 |

Reading:

- The first Turn of a 20-session burst no longer runs at 1.2-1.7 s per round:
  113-122 ms, the second-Turn figure, 1.1-1.3x the one-session `shell`
  figure of the same run. p95 over the whole phase fell from 1.5-2.0 s to
  0.16-0.24 s.
- The cost moved to the burst's first request: parked claims wait for their
  shard (2-6 s here), so first-Turn TTFR is 2.0-2.6 s against 1.6-2.7 s
  (r1-B, 9.2 s, was the first fork server of a cold, loaded run). Every
  later round of those Turns gains 1.1-1.6 s, so a 20-round Turn finishes
  about 25 s sooner.
- One session is unchanged within noise; `true` stays at 80-82 ms (r1-B's
  p95 is one 1.2 s outlier). In-process, change 3 removes about 2.6 ms of
  request assembly per round (`prepare_request` 21 -> 20 ms per request
  with the larger `seal_request`/`project_tool_wire` share unchanged); the
  deployment suite cannot resolve it beside storage and event delivery.
- Arm B's export predates the harness fix that finds forked shards
  (`live_shard_pids` by ancestry), so its shard CPU/RSS columns read zero;
  per-request CPU of the execution host fell from 43-48 to 4-9 ms because
  the work runs in shards.

### Remaining per-round cost (1 session, `true`, 40 Turns, py-spy 400 Hz)

77-89 ms per round of loop wall time: provider request preparation and send
32 ms (`prepare_request` 20 ms: `seal_request` 4.6 incl. a deep copy and
sorted digest of the context lineage, Anthropic translation 3.1,
`project_tool_wire` 2.2, `wire_byte_region` 1.9, token recount 2.2), tool
dispatch 16-19 ms (the `true` child itself about 7 ms), event-durability
barriers 8 ms (`checkpoint_invocation` flushes after tools and at round
close), the coordinator-binding read `coordinator_scope` 4 ms per request for
a conversation without a binding (one storage query each round, uncached
because only a positive answer is pinned), working-facts/requirements reads
3 ms. These are the next structural targets toward codex's 38 ms; none was
changed here beyond change 3.

### Alternatives considered

- **Run the burst's extra claims on the primary** (before): 1.2-1.7 s per
  round for the whole first Turn.
- **Migrate a running job to a ready shard at a round boundary.** Its entered
  effects, open provider streams and process-local carriers belong to one
  process; moving them needs a second recovery path. Placement before entry
  hands over only unentered claims.
- **A prewarmed pool of idle shards.** A count knob (forbidden).
- **Fork the multi-threaded primary.** Inherits held locks; the fork server
  is a separate single-threaded image of the same generation.
- **Cache negative coordinator bindings.** A binding can appear mid-Turn;
  needs a revision-checked invalidation first.

## Per-round cost follow-up (2026-11-18)

Owners: `lib/tasks_pkg/persistence_wait.py` (deferred ready boundary),
`lib/tasks_pkg/event_delivery.py` (`through` barriers), `lib/tasks_pkg/coordinator_policy.py`,
`lib/tasks_pkg/wire_fingerprint.py`, `serverctl_pkg/execution_shards.py`.

### Changes

1. **Ready receipts persist while the next round prepares.** The root's
   `ready` round end recorded and awaited every accepted event (about 4 ms a
   round). It now records the lane position; the pre-request boundary awaits
   exactly the events accepted up to it (`flush_task_events(through=...)`)
   before the paid request, after the next round's context preparation,
   admission and body build ran beside their persistence. The durability
   contract is unchanged: ready receipts still precede every paid request of
   the task (`stream_llm_response`, compaction summaries call
   `settle_deferred_ready_events` first) and every awaited checkpoint covers
   them. A domain sink (Swarm write-ahead) keeps the boundary awaited.
   Measured `close_round` 4.3 -> 0.3 ms per Turn.
2. **A Turn reads its coordinator role once.** `coordinator_scope` pinned a
   positive answer but re-read storage for every ordinary Turn on every request
   (4 ms). The negative answer is now kept for the Turn, keyed by the exact
   (owner, conversation, project). Decision: the role is a property of the
   Turn, in both directions, as the tool surface assembled from the same
   lookup already is. A binding made while a Turn runs applies from the next
   Turn. Alternative not taken: a process-wide cache invalidated by binding
   writes needs a cross-process binding watch (the API host writes, execution
   processes read); the Turn scope needs no invalidation because it ends.
3. **The static-prefix hash is kept per settled floor** (3.8 ms per Turn): the
   hash of the system rows plus first user row is memoized on that settled
   row together with the exact rows it covers.

### ABAB (full mode, 4 pairs, pooled samples)

Arm A `507ebe6785` (shared branch merged, before these changes), arm B
`75390e9742`. Same engine build, fresh roots, alternated A, B (host load
90-115). Median / p95, ms. Artifacts: `output/harness_perf/rounds-abab-20261118/`.

| metric | n per arm | A | B |
|---|---|---|---|
| `run_command true` Turn (two model requests) | 40 | 91.5 / 167.0 | **75.7** / 153.7 |
| `noop` steady round | 228 | 74.1 / 88.1 | **70.8** / 80.5 |
| `shell` steady round | 228 | 92.5 / 144.2 | 90.7 / 139.4 |
| 20 sessions, first Turn rounds | 1,600 | 114.1 / 197.2 | 110.4 / 202.7 |
| 20 sessions, second Turn rounds | 1,600 | 114.4 / 159.3 | 110.3 / 169.6 |

`true` improved 17 % and `noop` 4 %. Check 2 of the assignment (codex 38 ms
per round, pi 21 ms per `true`) is **not met**.

### Where the remaining time goes (`true`, 40 Turns, py-spy 400 Hz, arm B)

About 89 ms of loop wall time per Turn (two requests):

| part | ms per Turn |
|---|---|
| provider request preparation (`prepare_request`: Anthropic translation 3.7 with lineage deep copy, sealing 3.1, wire byte region 1.9, request id digest 1.1, raw archive 1.1, token recount 0.9) | 23.5 |
| provider exchange with the stub, SSE parse, request diagnostics | 14 |
| round context (programmatic inputs, working facts, steer/notice reads, micro compaction) | 10 |
| request body build incl. Request Inspector snapshot event | 6.8 |
| tool execution: command 4.8 (of it `which` over PATH for the Hope shim 2.1, the spawn+select 1.8), tool-effect intent persisted before the effect 2.6, `tools_in_flight` barrier 4.0, post-tool snapshot 5.2 | 17 |
| terminal `done` event persisted before settlement | 10.7 |
| admission token count | 4.2 |

A bare `subprocess.run(['true'])` costs 2.9 ms here, so the command is not
the floor. What separates Tofu from pi/codex is per-request durable
evidence they do not keep: a sealed request with context lineage, a Request
Inspector snapshot, raw archive capture, and Turn events persisted to a
separate storage process with intent-before-effect and terminal-before-
settlement barriers (about 17 ms of awaited persistence per Turn). Those
barriers are durability contracts and stay. The preparation work (about
40 ms per Turn) is the remaining structural target: the lineage deep copy
and digest, translation and request-id digests re-walk the whole history
each request instead of extending the previous request's settled prefix.
"Incremental request building" below removes that re-walk.

### Fork safety of the shard image

`tests/server/test_execution_shards.py::test_fork_audit_of_the_real_shard_image`
inventories the preloaded image (`tests/server/fork_audit_probe.py`): one
thread, 159 module locks of which none is held at the fork point, no socket
or storage connection (each shard opens its own; the fork server is spawned
fresh, never forked from the primary), and import-time identities limited to
those re-derived after fork (`boot_identity.BOOT_ID/PID`,
`turn_lifecycle._ATTEMPT_DISPATCH_OWNER_ID`). The audit found a third:
`conversation_sync.service` copied `BOOT_ID` at import, which the post-fork
reset never reached; it now reads the live value. Inherited descriptors are
the instance's append-mode log files, which every process opens the same way.
The fork server also refuses to fork whenever more than one thread exists.

`tests/chaos/test_chaos_faults.py::test_execution_shard_killed_mid_tool` kills
the shard running a tool while the primary is busy with another Turn. It
found that the dead shard's foreground command group outlived it (boot
recovery retires a dead owner's groups only when an execution host restarts);
the primary now runs the same owner-death sweep when it observes a shard exit.

## Incremental request building (2026-11-18)

Owners: `lib/llm/request.py` (`detach_input`, `_content_digest`),
`lib/tasks_pkg/orchestrator/_round_request_prep.py`, `lib/llm/cache.py`,
`lib/llm/anthropic_outbound/_to_anthropic.py`, `lib/llm_sanitize/lineage.py`,
`lib/tasks_pkg/round_evidence.py`, `lib/tasks_pkg/wire_fingerprint.py`.
Contract: `docs/CONTEXT_INJECTIONS.md`, "Incremental request sealing".

### Problem

Every provider request rebuilt its evidence from the whole history: Anthropic
translation, the lineage deep copy and span verification, the request-id and
content digests, the recount, the sealed per-item encodes, transport
fingerprints, the raw-archive capture and the context-telemetry token counts.
The settled-row memos built for that (per-row encodings, the semantic chain)
never fired, because nothing gave request messages a stable identity: the
body builder copies every message every round.

### Changes

1. **Unchanged history is the same immutable rows from round to round.**
   `build_round_request` settles the canonical messages against the task's
   previous round (`settle_items`: a message interchangeable with the row at
   its position is that row). Cache-marker rewrites are memoized per settled
   row with the marker values in the key, so ingress detachment reuses the
   post-marker rows by identity (`_wire_message_rows`, task-owned, released
   with the task's heavy state).
2. **Every stage memoizes on the row, keyed by everything else it reads.**
   Anthropic messages (replay policy and respelled tool-call IDs in the key;
   consecutive tool results become one memoized batch), conversion
   validation, rejected tool-call IDs, the recount projection and its token
   slot, sealed transport evidence, lineage range hashes (per settled carrier)
   and the Request Inspector snapshot row (a row with nothing to strip is its
   own snapshot row). `requestContentHash` resumes a SHA-256 chain per
   settled wire item. Lineage traces are copied as JSON instead of
   `deepcopy`; the raw-archive capture shares exact settled JSON; the OAuth
   cloak renames copy-on-write.
3. **Exactness.** Invalidation is by construction: any change to a message
   (compaction, injection, in-place edit of a loop message, moved cache
   marker, changed system prompt) yields a row that is not interchangeable
   with the previous one, and everything derived from it is recomputed.
   `tests/llm/test_incremental_request_build.py` drives 56 rounds per wire
   (Anthropic, Chat with and without cache markers, Responses) through those
   changes and asserts that the sealed payload, both identities, local tokens,
   sealed lineage, usage evidence, transport fingerprints, headers, the raw
   archive request bytes and the Inspector snapshot equal a fresh full
   rebuild at every round; a deliberately loosened reuse check fails it.

### Request build cost against history length

`benchmarks/request_build_scaling.py`, calling-thread CPU ms, median of 10
rounds per checkpoint; `build` is `build_round_request`, `prepare` the slot
adaptation plus Anthropic `prepare_request`; `cold` is the same history's
first request on a fresh task (the first request of a Turn). Arm A is
`247fc45044` (the checkpoint this work starts from), arm B this change.

| messages | A build | A prepare | A total | B build | B prepare | B total | A cold | B cold |
|---|---|---|---|---|---|---|---|---|
| 113 | 2.9 | 5.5 | 8.5 | 1.7 | 2.4 | 4.0 | 10.5 | 14.2 |
| 228 | 5.4 | 9.4 | 14.8 | 3.0 | 3.4 | 6.5 | 18.8 | 24.0 |
| 458 | 10.6 | 17.4 | 28.0 | 5.8 | 6.0 | 11.8 | 35.7 | 46.6 |
| 918 | 21.2 | 34.4 | 55.7 | 11.4 | 11.0 | 22.7 | 70.5 | 92.8 |

A warm round now grows by about 20 µs per message instead of 58 (`prepare`
by 10 instead of 37). It is near-flat rather than flat: one
interchangeable comparison per message (about 1.5 µs) stays, and so does
`build_body`'s own pipeline (about 8 µs per message: field projection,
protocol repair, same-role merge, canonical ordering, lineage seeding over
the loop's mutable message dicts). The cold request pays the memo
construction (settled twins of the translated, rewritten and recount rows,
per-row digest links): about 30 % more than before. Over a Turn the warm
rounds repay it from the second request on (228 messages: A 18.8 + 14.8 per
further request, B 24.0 + 6.5); only a single-request Turn is slower, by
about 2 ms at 100 messages.

### ABAB (full mode, `--api-restarts 0`, 6 pairs)

Arm A `247fc45044`, arm B `7fa61c098c`, same engine build, fresh roots,
alternating. Pairs 2–5 ran at host load 80–100; pairs 1 and 6 at 180–190.
Median / p95, ms. Artifacts: `output/harness_perf/request-build-abab-20261118/`.

| metric | n per arm | A (pairs 2–5) | B (pairs 2–5) | A (all 6) | B (all 6) |
|---|---|---|---|---|---|
| `run_command true` round | 40 / 60 | 74.1 / 175.8 | **68.3** / 116.0 | 80.2 / 679.2 | **69.4** / 323.9 |
| `true` TTFR (cold request) | 40 / 60 | 93.6 / 317.1 | 93.9 / 179.5 | 100.0 / 586.5 | 96.4 / 481.7 |
| `noop` steady round | 228 / 342 | 68.5 / 111.1 | **64.0** / 106.6 | 70.9 / 558.0 | **64.2** / 106.6 |
| `shell` steady round | 228 / 342 | 104.9 / 157.3 | 114.4 / 135.0 | 114.9 / 527.4 | 114.2 / 132.5 |
| `chat` TTFR | 40 / 60 | 83.5 / 91.2 | 82.7 / 87.1 | 85.3 / 225.0 | 83.1 / 103.6 |
| 20 sessions, all rounds | 3200 / 4800 | 103.0 / 176.9 | 101.7 / 164.6 | 111.4 / 627.2 | 100.7 / 161.3 |

`shell` rounds are bimodal in both arms (about 75 and 115 ms; a fixed delay
in the command path, not request building): each mode is faster in B, and
the median follows the share of rounds in the fast mode, which varies from
run to run (A 14–32 of 57, B 19–21).

Request build time per round, from the Turn trace (`[TurnTraceSummary]`,
median of run medians, pairs 2–5, `noop`, 21 rounds per Turn): model-call
time per round 23.1 → 21.6 ms (it includes provider ingress and the stub
exchange) and round preparation per round 19.1 → 16.0 ms.

### What remains (floor)

The harness histories are short (75–160 messages), so the per-round build
saving there is about 4–5 ms and the rounds stay near 64–69 ms against
codex 38 / pi 21. What remains per `noop` round is not history-length work:
round context preparation (inbox, working facts, programmatic inputs,
hygiene), the provider exchange and stream parse, tool dispatch, and the
awaited persistence that the durability contracts keep (intent before
effect, terminal before settlement, ready receipts before the next paid
request). The first request of each Turn builds from scratch because the
row holder dies with the task; carrying it across Turns needs a retention
owner for conversation-sized state between Turns, which the terminal
heavy-state release deliberately refuses.

### Alternatives considered

- **A delta request over the previous sealed request.** Sealed evidence
  would depend on request history, need its own invalidation protocol, and
  the provider still needs the full body; identity reuse gives the same
  saving with every request still a pure function of its body.
- **Content-hash keyed caches.** Computing the key costs the encode being
  saved.
- **Identity memos on the loop's message dicts.** They are mutable and
  edited in place, so identity proves nothing without a comparison.
- **Dropping the recount-projection memo** to make the first request
  cheaper: measured 2 ms less cold and 1.2 ms more per warm round at 458
  messages; kept, because Turns with a tool round outnumber single-request
  Turns.

## Round hops (2026-11-18)

Owners: `benchmarks/harness_perf/` (stub, `round_profile.py`),
`lib/tasks_pkg/manager/_registry.py` (provider abort check),
`lib/tasks_pkg/streaming_tool_executor.py` (prefetch pool),
`lib/tasks_pkg/tool_dispatch/_flags.py` (round partitions, effect-free
batches), `lib/tasks_pkg/round_evidence.py`,
`lib/tasks_pkg/cache_tracking/_hashing.py`,
`lib/tools/hope_queue_policy_launcher.py`. Contracts:
`docs/TASK_EVENT_DELIVERY.md` (effect-free batches), `docs/ROUTER.md` R16
(PATH search reuse).

### Method

`benchmarks/harness_perf/round_profile.py` boots the deployment of `run.py`,
runs `noop` Turns (20 read rounds) and `true` Turns, and samples the execution
host and its shards with `py-spy --idle --subprocesses`, so waits count as
wall time. Every sample of the thread running the Turn is charged to the
first matching component (`COMPONENTS`); a share times the Turn's wall time
per request is that component's wall clock. Profiling slows rounds, so shares
come from profiled runs and milliseconds from the unprofiled ABAB arms'
`[TurnTraceSummary]` (`noop` 66.0 / 63.1 ms per request, `true` 173.5 /
158.0 ms per Turn of two requests, arms A / B). `--no-spy` is the timing
sample of one ABAB arm.

### The stub charged every client a delayed ACK

The stub wrote headers and body in two sends with Nagle on: on a reused
connection the body waited for the client's delayed ACK (40 ms) after the stub
had recorded `finished_ns`, so that wait counted as harness time. A pooled
`urllib3` client measured 41.0 ms per request against the old stub and 0.22 ms
with `TCP_NODELAY` (`test_stub_bodies_never_wait_for_a_delayed_ack`). The
references re-measured with the fixed stub (codex 0.148.0, pi 1.4.0, 3 runs
each): pi `noop` 44.4 → **3.0 ms**, pi `shell` 47.9 → **6.6 ms**, pi
`true` 21.4 → 21.5 ms, codex `noop` 37.6 → 38.3 ms, codex `shell` 68.2 →
68.0 ms, codex `true` 187.8 → 187.9 ms. pi had paid the stall on every
round; codex and Tofu mostly had not (Tofu's body read was 9.6 % of a profiled
`noop` round before the fix, 0.1 % after). The baseline tables above predate
the fix. A `--references-only` run also crashed closing storage it never
started; it now skips that.

### Where a round goes

`noop` steady round, profiled shares and ms at the unprofiled medians. A =
`90be3c5a3b` (merged tree with the stub fix), B = this change. Rows under 1 %
in both arms are omitted (round close, tool loop breaker, prefetch submit,
other synchronous storage reads, the pre-request barrier: together under 3 %).

| component | A share | A ms | B share | B ms |
|---|---|---|---|---|
| provider request preparation (`prepare_request`: seal, Anthropic translation, recount, wire fingerprints, raw-archive capture) | 13.1 % | 8.6 | 18.7 % | 11.8 |
| post-tool state snapshot (wire sanitize, lineage, frame detach) | 8.3 % | 5.5 | 8.4 % | 5.3 |
| round context (composer refresh, compaction pipeline, inbox) | 7.6 % | 5.0 | 9.1 % | 5.7 |
| `tools_in_flight` event barrier | 7.3 % | 4.8 | 0 | 0 |
| thread creation (abort probe, prefetch carrier, post and watchdog threads, key-stats flusher) | 6.4 % | 4.2 | 1.3 % | 0.8 |
| tool execution (`read_files` on the prefetch worker, awaited) | 6.1 % | 4.0 | 8.0 % | 5.0 |
| tool pipeline CPU (parse, settle, budget) | 5.6 % | 3.7 | 6.9 % | 4.4 |
| per-Turn finalize, spread over 21 requests | 5.6 % | 3.7 | 5.0 % | 3.2 |
| provider exchange: send and response headers (post thread; stub service 1.6 ms median for a 95 KB body) | 5.2 % | 3.4 | 5.9 % | 3.7 |
| `build_round_request` and request snapshot | 4.7 % | 3.1 | 5.9 % | 3.7 |
| slot adaptation, admission count, dispatcher pick | 3.8 % | 2.5 | 4.9 % | 3.1 |
| prompt admission count | 3.3 % | 2.2 | 3.3 % | 2.1 |
| MCP relay RPCs from a shard to the execution host | 3.2 % | 2.1 | 1.3 % | 0.8 |
| SSE parse and stream state | 2.8 % | 1.8 | 2.1 % | 1.3 |
| event append (projection, detach) | 2.3 % | 1.5 | 2.9 % | 1.8 |
| storage read: steers (Turn head) | 2.3 % | 1.5 | 3.3 % | 2.1 |
| cache-break accounting (system-prompt hash) | 2.0 % | 1.3 | 1.1 % | 0.7 |
| storage reads: Turn requirements, working facts | 2.7 % | 1.8 | 2.7 % | 1.7 |
| other loop work | 4.6 % | 3.0 | 5.4 % | 3.4 |

The `true` Turn (two requests, 158 ms in B) spends most of its time outside
the measured round: Turn start-up (context composition, tool assembly) 26 %,
finalize 17 %. The round itself is the tool batch (`tools_in_flight` barrier
2.2 %; the awaited command 6.1 %, of which before this change 73 % was the
`hope`/`mlp` PATH search, about 50 lookups each releasing the GIL; the
tool-effect intent ACK; the post-tool snapshot 3.2 %) followed by the second
request's preparation (the `noop` components).

A steady round has no process hop between the API host and the execution
plane: the Turn runs in one shard (or the primary), and its only
cross-process calls are storage RPCs to the engine and, from a shard, MCP
catalog reads relayed to the execution host. No SSE subscriber is attached
in the benchmark, so fan-out costs only the append-side projection.

Against the references, component by component (their traces are the
stub's arrival/finish records and bodies): pi's 3.0 ms `noop` round is its
entire harness (a 6.5 KB request, the read, SSE parse), while Tofu's request
preparation alone is 12 ms for a 95 KB body with sealed evidence. codex's
38 ms `noop` and 188 ms `true` are its own loop and its sandboxed (`bwrap`)
spawn; pi's 21.5 ms `true` is a bash spawn. Neither reference persists
per-round evidence to another process, takes event barriers, reads storage,
fans events out or hops processes: those rows are zero for both. In Tofu
they add up to about 9 ms per `noop` round in B (pre-request barrier 0.8,
storage reads 3.8, event append 1.8, relay 0.8, post thread 2.1 beyond the
stub's service time).

### Changes

1. **The stub sends with `TCP_NODELAY`** (above).
2. **One provider abort check per task.** The check was rebuilt for every
   request, so its 5 s tombstone throttle never spanned requests: every
   request started a probe thread and paid one storage read beside its
   stream. It is now task-owned (`make_provider_abort_check`).
3. **One prefetch executor per process.** Each round's accumulator created a
   pool and started (then retired) a carrier thread. Accumulators share one
   pool and own exactly their futures (`close` cancels or waits for those).
4. **Tool partitions resolve once per round.** Dispatch and the loop breaker
   resolved the write/reuse partitions four times per round; in a shard each
   resolution asked the execution host's MCP bridge twice over the relay.
   The resolution is memoized per `_activeRoundNum`; a catalog change applies
   from the next round, as it already did for calls dispatched before it.
5. **An effect-free batch defers its event barrier**
   (`docs/TASK_EVENT_DELIVERY.md`): a response of only result-reusable reads
   leaves the accepted events to the next pre-request (or terminal) barrier,
   which persists them while the reads and the next round's preparation run.
6. **State snapshots settle against the previous state snapshot.** Request
   and post-tool snapshots shared one settled-row baseline; the request
   wire's injected rows shifted every position, so a state snapshot reused
   nothing and its frame detach copied the whole history (0.55 → 0.06 ms in
   the isolated case).
7. **The system-prompt hash is kept with its text.** The cache-break detector
   hashed the full system text every round; equal text returns the last
   digest after a comparison.
8. **The `hope`/`mlp` PATH search is reused** while the PATH, the working
   directory and every searched directory's identity and mtime are unchanged
   (`docs/ROUTER.md` R16). Statting the directories is answered from
   attribute caches (0.05 ms for 29 entries here against 1.3 ms of lookups,
   more in the busy execution host).

### ABAB (`round_profile.py --no-spy`, 5 pairs, fresh roots, same engine)

Arm A `90be3c5a3b`, arm B `2542abbf19` (changes 2–8). Each run: 3 `noop`
Turns of 20 rounds and 12 `true` Turns after warm-up. Pairs 1–2 ran at host
load 95–127, pairs 3–5 at 17–65. Median / p95 (n), ms; codex and pi from
the fixed-stub reference run. Artifacts:
`output/harness_perf/round-hops-abab-20261118/`.

| metric | A | B | codex | pi |
|---|---|---|---|---|
| `noop` steady round | 66.6 / 351 (285) | **60.5** / 257 (285) | 38.3 | 3.0 |
| `run_command true` round | 73.0 / 414 (60) | **66.7** / 293 (60) | 187.9 | 21.5 |
| `true` TTFR | 96.1 / 623 (60) | 91.2 / 288 (60) | | |

B is faster in every pair (low-load pairs: `noop` 60.9/64.7/65.0 →
60.2/58.8/59.2, `true` 72.9/70.9/72.1 → 64.1/66.6/65.2). An earlier ABAB of
changes 2–7 (4 pairs, load 11–33) gave `noop` 65.9 → 60.1 ms and `true`
72.3 → 71.0 ms (n = 228 / 48 per arm): the `true` gain is change 8.

### What remains (floor)

Tofu's `noop` round is 60 ms against pi's 3 ms and codex's 38 ms; its `true`
round is 67 ms against pi's 21.5 ms (codex 188 ms). What remains is not one
component:

- About 35 ms of per-request Python work on the Turn thread: request
  preparation (12 ms: sealing, translation, recount, wire fingerprints and
  raw-archive capture of the evidence the Request Inspector and cache
  accounting rely on), the post-tool state snapshot (5 ms), round context
  (6 ms), tool pipeline and settlement (4 ms), admission counts (5 ms), the
  request snapshot (4 ms). It runs under the GIL beside the process's own
  event-delivery, audit-log and raw-archive threads; every wait on another
  thread (the provider post thread, the prefetch worker, storage replies)
  costs a GIL hand-off, which is why lookups and hashes that release the GIL
  were worth removing.
- About 9 ms of durable evidence work the references do not do (above). The
  durability contracts stay: tool intent before effect, ready and tool-round
  receipts before the next paid request, terminal before settlement.
- Next structural targets, not changed here: one pipelined storage exchange
  for the round's three reads (Turn head, requirements, working facts: three
  owners); deriving the post-tool state snapshot from the previous request
  snapshot plus the appended rows instead of re-sanitizing the history;
  reusing a carrier thread for the provider post and the stream watchdog;
  and the per-request sealing work in `prepare_request`, which incremental
  request building made near-flat in history length but not cheaper per
  request.

### Alternatives considered

- **A shorter interpreter switch interval** to shorten GIL hand-offs: a
  tuning knob that taxes every thread.
- **Dropping the post-tool state snapshot** as superseded by the next request
  snapshot: the Request Inspector shows the state snapshot first and labels a
  request snapshot as the fallback, and a round that halts after its tools
  has no next request.
- **A process-wide MCP catalog push to shards** instead of the round memo: a
  new invalidation protocol for a cost the memo removes.
- **Caching the PATH search by PATH alone**: misses a CLI installed later and
  silently disables the guard.

## Alternatives considered

- **Stub `lib.llm.stream_chat` in process.** Rejected: it removes request
  encoding, transport and stream parsing, which are harness work.
- **Reuse `scripts/agent_loop_load_test.py`.** Its fake model is paced like
  production and speaks only Anthropic; this suite needs zero latency and all
  three wire faces, and it reuses that script's storage, route and
  execution-host bring-up by import instead.
- **A single connection document for both hosts.** Not possible: attachment
  requires `projectPath == TOFU_PROJECT_PATH`, and the execution host must name
  the code checkout while the API host must name the private project (where
  the `ExecutionService` record lives), so the driver writes one document per
  host for the same engine.
- **Polling the attempt for settle.** Replaced by the durable `settledAt`
  mapped onto the monotonic clock; polling over-states settle by up to one
  poll interval.

## Startup, idle and dispatch

Owner: process start-up, idle cost and the submit-to-provider handoff of the
API host (`server.py`), the execution host (`serverctl_pkg/execution_host.py`)
and the manager's preparation for them (`serverctl.py start`).

### Method

- **Manager-composed instance.** `benchmarks/startup_idle_dispatch.py` runs
  the real `serverctl.py start` composition of the checkout it lives in
  (manager, API host, execution host, SQL Sidecar) with a scrubbed
  environment, private manager/API ports, a private `TOFU_HEARTBEAT_DIR` and
  `TMPDIR`, and in-tree `data/`. `cold-start` stops and restarts the instance
  (median of 5, after one warm-up start), `idle` samples both hosts over 600 s
  after a 60 s settle (CPU from `/proc/<pid>/stat`, RSS, voluntary plus
  involuntary context switches of every thread as wakeups, threads named via
  py-spy), `dispatch` measures `POST /turns` to the first request byte at a
  loopback provider (20 Turns, plus the first Turn after ready).
- **Suite.** `benchmarks/harness_perf/run.py --smoke` (TofuDB engine, fresh
  private root per run, so the storage-boundary cache starts cold) for the
  execution-host cold start and idle RSS, 3 alternating pairs.
- **Arms.** Before is `08e08feec55f` exported to a private directory, after is
  this change; each arm runs from its own tree, alternating, on local disk of
  the shared host (`codelab-prod-…-9h7mn`, load 15–25). The SQL Sidecar is the
  manager-composed backend because a worktree has no TofuDB certification
  fixture; nothing measured here depends on the engine.
- **Import profile.** `python -X importtime -c "import server"` and cProfile of
  the same import; stage clocks of `execution_host.create_application`
  reproduced step by step in one process.

### Results (2026-11-17)

| metric | before | after |
|---|---|---|
| `serverctl start` to both hosts ready, median of 5 | 12.0 s (10.6–14.1) | **7.4 s** (6.5–8.8) |
| host spawn to ready, median of 5 (API ready waits for execution) | 7.8 s | **3.55 s** |
| suite execution host cold start (prepare + launch + ready), 3 runs | 11.5 / 15.0 / 13.7 s | **8.1 / 7.9 / 7.3 s** |
| execution host after the manager's plan: plan check, import, pin, construct, recheck | 6.5 s | **2.9 s** |
| storage boundary scan, cold / warm (this checkout) | 4.4 / 0.12 s | 2.45 / 0.12 s |
| storage boundary scan on the live DolphinFS checkout, warm | 0.42 s | 0.23–0.35 s |
| API host idle wakeups (600 s) | 42.0 /s | **21.8 /s** |
| API host idle CPU / RSS | 0.40 % / 137 MiB | 0.29 % / 138 MiB |
| execution host idle wakeups | 12.5 /s | 12.1 /s |
| execution host idle CPU / RSS (manager-composed) | 0.18 % / 165 MiB | 0.17 % / **141 MiB** |
| execution host idle RSS (suite, TofuDB) | 213 MB | **191 MB** |
| submit to first provider byte, steady (20 Turns) | 73 ms | 73 ms |
| `POST /turns` admission | 8 ms | 8 ms |

Execution host stages (one process, local disk; before → after): plan check
1.1–1.7 s → 0.3 s; `import server` 1.0–1.2 s (unchanged); freeze 2.4–3.1 s →
1.4 s; second application assembly 0.14 s → 0; post-construction recheck
0.9–1.2 s → 0.2 s; the manager's plan child 1.3 s → 0.85 s (warm boundary
cache). On DolphinFS each of the removed source passes costs 5–6 s (one read
of 8,098 files, 89 MiB, warm page cache); live logs show the execution host
spending 33 s between "starting up" and phase 1.

Where the remaining time goes. API host: `import server` about 1.2 s
cumulative, of which route assembly and werkzeug rule compilation are 0.45 s,
`lib/__init__` 85 ms (plugin flag entry points), `lib.pricing._provider` 75 ms
(bundle entry points), the Chromium fontconfig probe 0.27 s. Execution host:
the same import, pinning the frozen generation 1.4 s, the cold boundary scan
2.45 s once per data root. Idle: the storage liveness probe (6.4 wakeups/s in
each host: a 1 s probe on a new thread, the declared liveness contract in
`docs/STORAGE.md`), the manager's TCP readiness probe of the API port and the
1 s loop heartbeat. Dispatch (`[TaskStartupTiming]`): API admission to
execution-host claim wake 1 ms; the claim 4 ms; per-Turn preparation reaches
context-ready at +44 ms (`registration_route` 9, `startup_tools` 8, context
compose 10 ms); round assembly to the first byte about 25 ms. The first Turn
after ready takes 0.46–0.78 s: 44 % first-use imports and 0.11 s loading the
token encoder.

### Decisions

1. **The test suite is not execution source.** Problem: the resident
   execution generation listed, read and zlib-pinned the whole `tests/` tree
   (4,988 of 8,098 files, 51 of 89 MiB) in every plan pass and in freeze, and a
   test-only edit replaced the execution host. Decision:
   `pinned_import_source.NON_APPLICATION_PACKAGES` names it once;
   `application_sources()` applies it to plan preparation, pin sweeps, freeze
   and frozen lookup; `tests/server/test_execution_plan.py` asserts that no
   generation member imports it. Alternatives: reuse `pyproject.toml`'s wheel
   package list (it also excludes `routes` and storage authorities the server
   runs); a per-package allow list (a second inventory to keep in step).
2. **Recheck a constructed host by pin stat.** Problem: after construction
   the host re-read and re-hashed every source to learn whether the checkout
   moved. Decision: the retained pins already equal the plan, so
   `unchanged_since_freeze()` compares one listing and one stat per pin and
   byte-compares only files whose size or mtime moved; only a changed checkout
   pays the exact `prepare_plan`. Alternative: drop the recheck (loses the
   source-refresh authorization `serverctl_pkg/AGENTS.md` grants to a
   construction-time change).
3. **One application per execution host.** `import server` already assembles
   the application `server.py` serves; the host adopts it instead of a second
   `create_app()`.
4. **The storage boundary caches every file.** Problem: each of the three to
   four boundary checks per boot ran `rg` over all of `lib/` and `routes/`
   before consulting a cache that held only marker-bearing files; the scanner
   walked each AST three times. Decision: every listed file is cached by stat
   signature (markerless files with no violations), only changed files are
   searched (`rg`, or the same regex in Python) and parsed, in one traversal;
   identical verdicts on all 9,165 tracked Python files. Alternative: key the
   verdict on the plan's source digest (the boundary also covers non-package
   directories outside the plan, so it would not be exact).
5. **Shutdown is awaited, not polled.** Problem: `wait_for_shutdown_trigger`
   slept 50 ms in a loop for the life of the API host: 20 wakeups/s idle and
   up to 50 ms before a drain began. Decision: `ShutdownRequest` (a
   `threading.Event`) resolves the futures of awaiting loops from `set()` on
   any thread. Alternative: `run_in_executor(event.wait)` parks a pool thread
   that `asyncio.run` then joins forever if shutdown never comes.

### Considered and not taken

- **Preload the token encoder and first-Turn modules after ready.** Saves
  most of the first Turn's 0.46–0.78 s, but the encoder alone retains
  +53 MiB (0.5 s to load), moving the execution host's idle RSS from 141 to
  about 194 MiB against the ≤ 300 MB all-process idle target.
- **One process-wide entry-point index.** Each `entry_points(group=…)` rescans
  every distribution: 69 ms per group here (270 distributions), 29 ms with the
  live interpreter (116). Six owners each scan their group, and their tests
  replace `importlib.metadata.entry_points` with fakes of several signatures;
  about 0.15 s per process does not justify a shared seam across them.
- **Defer the Chromium environment probe.** `ensure_chromium_env` mutates
  `os.environ` so every later child (including agents' shell commands)
  inherits library and font paths; per-launch-site setup would cost more
  machinery than its 0.27 s.
- **A headless execution composition without routes** (≈0.45 s import and
  rule compilation, and their RSS). The Router's `owner_settings` in the
  execution host reaches the Settings backend that assembly registers
  (`docs/ROUTER.md` R11); it needs a composition split, not a skip.
- **Reuse one storage liveness probe thread.** Saves a thread start per
  second per host; the per-probe thread is the watchdog's isolation from a
  stuck probe, and the cadence is a liveness contract, not a tuning value.

### Second pass (2026-11-17): safety, boot recovery, first Turn

Method additions. **Boot recovery:** `benchmarks/startup_recovery_batch.py`
runs `recover_active_work_items` against a disposable **TofuDB on DolphinFS**
(`.eval-runs/`), 32 restart-paused work items over 4 Git checkouts on the
same volume (the live shape of 2026-11-17 00:15), 5 passes per process, arms
alternating in fresh processes (`TOFU_BENCH_CHECKOUT`). **First Turn:** the
manager-composed instance of `startup_idle_dispatch.py` per arm (exported
pre-change tree vs. this tree, alternating, 3 rounds), first Turn submit to
first provider byte after ready, then 5 more Turns, execution-host RSS 30 s
after ready and after the Turns. The shared host was loaded (both hosts ready
in 10–28 s), so only medians and ranges are meaningful. py-spy (250 Hz) of
the execution host located first-Turn and steady-Turn costs.

| metric | before | after |
|---|---|---|
| active work-item recovery, 32 paused items / 4 projects, TofuDB on DolphinFS, median of 5 (2 alternating pairs) | 0.462 / 0.443 s | **0.102 / 0.106 s** |
| Git identity children per recovery pass (same shape; before from cProfile, after by the per-path memo, 2 per checkout) | 64 | **8** |
| storage exchanges for the page's task results | 32 | **1** |
| first Turn after ready, submit to first provider byte (3 alternating runs) | 1.20 / 2.13 / 1.50 s | **0.87 / 1.20 / 0.50 s** |
| execution host RSS 30 s after ready, before any Turn | 145 / 131 / 127 MiB | 145 / 142 / 145 MiB |
| execution host RSS after six Turns | 205 / 196 / 192 MiB | 198 / 192 / 196 MiB |
| steady submit to first provider byte (median of later Turns) | 78 ms | 79 ms |

Live recovery mapped onto the bench: the 10 s work-item part of the 22.4 s
phase is the per-item Git identity (two children plus an ancestor walk on
DolphinFS) and per-item reads; the remaining 13 s is dispatch recovery
(`recover_persisted_dispatch`, below). The live gain is observed only after
the next execution-host restart.

6. **A start never acts on another live instance.** Problem: a Tofu shell
   exports the serving instance's `TOFU_PROJECT_PATH`; `serverctl.py` took
   its project from it, so `serverctl.py start` in a worktree deployed the
   storage engine into the live checkout and launched a second manager. The
   manager read its worker's lock at `<project>/data` while a worker on
   `TOFU_DATA_DIR` writes it under that data root (its own worker reported as
   a foreign port owner), and every instance stamped one host-global
   heartbeat that the live manager reads for wedge evidence. Decision:
   `serverctl.py` controls the checkout it belongs to; instance actions
   (start, stop, restart, ensure, install-recovery, prepare-frontend) refuse
   with exit 2 under another checkout's `TOFU_PROJECT_PATH`, and `ensure`
   refuses a manager port answered by a manager of other projects instead of
   launching a watchdog that can never bind. `server_manager.read_lock_status`
   resolves the worker's data root from the composition the worker receives
   (ambient, project `.env`, `serverEnv`). The heartbeat defaults to
   `/tmp/tofu/heartbeat/<sha256 of the data directory>/server.heartbeat`
   (`runtime_path_policy.instance_heartbeat_path`), read the same way by the
   instance lock, the manager and `deploy/tofu_guard.sh`. Alternatives:
   scrubbing a deny list of inherited names in serverctl (the scratch-server
   rule already rejected deny lists: the exported identity grows over time);
   keeping the shared heartbeat and requiring `TOFU_HEARTBEAT_DIR` (an
   unenforced ritual that every new launcher forgets). Specification:
   `tests/server/test_start_never_acts_on_another_instance.py`.
7. **A recovery page reads its tasks in one exchange and each checkout
   once.** Problem: boot recovery read each active work item's task result
   and resolved each item's checkout identity one item at a time.
   Decision: the page's unique active items are collected first, their
   `task_results.replay_get` reads go through one `query_many`, and the
   pass memoizes `_checkout` per path (`record_task_workspace_intents(...,
   checkouts=)`; failures are not remembered). Alternatives: batching
   `find_workspace` too (0.1 s of 0.76 s here, and it needs a new batch read
   in integration state); not re-recording intents of items that stay paused
   (the acknowledgement lives only on the in-memory task, so persisting it
   is a contract change of workspace delivery).
8. **The execution host imports its turn executor with the application.**
   Problem: py-spy of the first Turn after ready showed 0.2 s of first-use
   imports (`lib.tasks_pkg.orchestrator.api` from `spawn._prepare_task_worker`,
   `lib.llm.stream` through the `lib.llm` facade) inside the claimed attempt.
   Decision: `execution_host.create_application` imports both after pinning;
   the API host still never does. The cost is about 13 MiB before the first
   Turn only: RSS after any Turn is unchanged (the same modules), and the
   host's ready time moved within the noise of this host. Alternatives:
   importing in a background thread after ready (same memory, and a Turn
   arriving meanwhile waits on the import locks anyway); preloading the token
   encoder (rejected above: +53 MiB).

Considered and not taken in this pass:

- **A byte bound instead of the encoder for small requests.** Byte-level BPE
  never yields more tokens than bytes, but admission counts feed serving-unit
  conversion, telemetry and the memoized-count parity contract of
  `lib/token_counter`; the encoder load is about 80 ms of the first Turn.

### Third pass (2026-11-17): dispatch recovery, per-Turn preparation

Method. **Dispatch recovery:** a private authority restored with `tofu-db
restore` from the live store's closed backup (`data/backups/tofudb/closed`,
durable sequence 1,005,251, 02:04 that day) into `.eval-runs/` on DolphinFS,
opened by an engine built from this source; `benchmarks/startup_dispatch_recovery.py`
runs one `recover_persisted_dispatch` pass per fresh process (the boot shape:
4 project scopes, 188 assignments, 5.4 MB of rows), arms alternating on the
same converged copy after one warm-up pass, storage exchanges counted by
operation (a pipelined batch is one). cProfile of the before arm located the
cost. **Per-Turn preparation:** `[TaskStartupTiming]` stages of harness Turns,
`ModelRoutingRepository.get` against TofuDB with the 91-model aggregate (60
reads per process, 3 alternating pairs), and the harness suite
(`harness_perf/run.py --smoke`, 5 alternating pairs, chat and `true` TTFR
pooled, n = 30 per arm). `scripts/agent_loop_load_test.py` now installs the
live catalog size (91 Models): every Turn reads and validates the whole owner
aggregate, so the former one-model document hid that cost. The shared host
ran at load average 170–310 (58 CPUs) throughout; clocks are observations,
counts are the result.

| metric | before | after |
|---|---|---|
| dispatch recovery pass, fresh process, median of 5 (alternating) | 29.6 s (6.7–38.8) | **21.5 s** (13.2–23.3) |
| same, an earlier single pair on a less loaded host | 2.27 s | **1.80 s** |
| storage exchanges per pass | 574 | **200** |
| `project.dispatch.get` point reads per pass | 415 | 65 (+ 12 page batches) |
| `turn.attempt.get` per pass | 57 | 17 (+ 4 batches, one per scope) |
| schema walk of the 188 stored rows, cold process | 0.92 s | **0.50 s** |
| `ModelRoutingRepository.get`, 91 models, median of 60 (3 pairs) | 8.4 / 10.2 / 8.1 ms | **3.6 / 3.0 / 3.3 ms** |
| `worker_entry_signal` per Turn (git child), loaded host | 15–35 ms | 0 (no child) |
| harness TTFR chat + `true`, pooled n = 30, median (p25–p75) | 2,364 ms (270–3,404) | 1,551 ms (116–2,596) |

The pooled suite medians are bimodal with host load (per-run medians 99 ms to
4.7 s in both arms) and do not isolate this change; the component rows do.
Live recovery took 13 s at the 00:15 boot; the copy is post-boot, so its
Coordinator resumed in place at that boot is no longer interrupted and its
births (the two 6 s gaps of the live log) are not in the reproduced pass.

9. **A whole-project dispatch read takes one exchange per index page.**
   Problem: `SegmentedDispatch.discover` and full or cohort walks read every
   assignment document with its own `project.dispatch.get` (415 round trips
   for 188 rows). Decision: `index_pages` yields each stored index page, and
   its documents are read together through `get_many` (`query_many`, one
   pipelined exchange); every document is still checked alone against its
   index row. Specification:
   `test_whole_project_reads_take_one_exchange_per_index_page_not_per_row`
   on SQLite, PostgreSQL and native authorities.
10. **Recovery reads the attempts of the lanes it decides together.**
    Problem: `_recovery_skip_reason` read each executed lane's attempt in its
    own round trip. Decision: the pass applies the row-only filters
    (`_filter_reason`) first and reads the attempts of the remaining executed,
    unreviewed lanes with one `turn_lifecycle.get_attempts`
    (`repository.read_attempts`); `restart_resume(..., attempt=)` decides from
    that row, and a read error fails only its own lane. Every write still
    rereads under its CAS or live admission, as before. Alternatives: a
    pass-scoped attempt cache (a second reader of the same fact with its own
    staleness rule); prefetching Turns as well (only interrupted attempts read
    one, 18 per pass).
11. **A stored row repeats no schema walk of a value already proven.**
    Problem: a fresh process (every boot) walked each stored assignment whole
    with jsonschema, although the grants of one Coordinator request carry the
    same delegation policy and profile verbatim (12 and 8 distinct values in
    188 rows; 2.7 of 5.4 MB). Decision: a cold row is decided member by member
    (the `_members` rule already used for changed rows), and a member whose
    schema only references a definition is skipped when that exact canonical
    content was already proven valid under it in this process
    (`_VALID_SHARED_VALUE_DIGESTS`, digests only, failures never recorded).
    3,000 random corruptions of the real rows got the same verdict as a whole
    walk with the memo warm. Alternatives: persisting proofs beside the
    documents (a storage-contract change, and a forged proof would skip
    validation); skipping validation in passive discovery (decisions read the
    discovered rows before their own reread); a faster validator library (a
    new dependency for one owner). The two ledger arrays (`modelAttempts`,
    `toolAttempts`, 0.35 s) are distinct per row and are still walked.
12. **A model-routing read skips the walk only for content proven canonical.**
    Problem: `ModelRoutingRepository.get` ran `normalize_document` over the
    whole owner aggregate on every read, twice per Turn (5–7 ms each at 91
    Models). `lib/model_routing/AGENTS.md` forbids a global authority cache
    because every request must read and validate its own owner revision.
    Decision: every read still fetches and decodes its own revision; per owner
    boundary the repository keeps only the canonical digest of the last stored
    document that normalization returned unchanged (equal value and equal
    canonical JSON, so `1` vs `1.0` or a tuple never pass), and a read whose
    decoded content has that digest is already its validated form. No
    document, revision or secret is retained, so the invariant holds: the
    verdict is a pure function of the exact bytes the request read under this
    process's pinned schema and defaults. Alternatives: a document cache keyed
    by revision (an authority cache, and revision is not content after a
    restore); validating only at write time (rows written by other versions or
    restored from backup would never be checked); reading once per Turn
    (two owners read independently by design). Specification:
    `tests/model_routing/test_repository_read_validation.py`.
13. **Worker entry asks Git for a checkout's identity once.** Problem: the
    isolated-work signal at worker entry spawned `git rev-parse
    --show-toplevel` on every Turn with a project, right after binding had
    identified the same checkout from its metadata (`worker_entry_signal`,
    15–35 ms on the loaded host; a folder that is not a checkout paid a failing
    child). Decision: binding records each writable root's top level, or that
    it has none (`admitted_checkout_root`); the signal passes it to
    `has_active_workspace_for_work(checkout_root=)` or returns at once.
    Specification: `tests/project_mod/test_worker_entry_checkout_identity.py`
    (the recorded top level equals Git's for a checkout, a subdirectory, a
    symlink alias and a linked worktree).

### Remaining

- Dispatch recovery: per-item reads left are `conversation.get` and Stop
  watermark `record.get` (21 each, the Stop checks of eligible lanes), and
  the Coordinator continuation's Turn reads; the ledger-array schema walk
  (0.35 s per boot) is the largest compute left.
- Steady submit to first byte: tool assembly (`startup_tools`, about 10 ms)
  and context composition (`startup_context_compose`, about 15 ms) are the
  largest stages left; the API to execution-host handoff is about 10 ms.
- Execution host to ready is 2.9 s of host work plus the manager's plan child
  against the ≤ 2 s target: `import server` and pinning remain.

