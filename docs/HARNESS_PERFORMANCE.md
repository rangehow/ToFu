# Harness performance

What the agent harness itself costs per model round, measured with the model
removed, and how that compares with the reference harnesses (codex,
claude-code, opencode, pi). Owner of the method: `benchmarks/harness_perf/`.
This document is the current baseline, the hotspot list and the targets that
optimization work is judged against.

## Method

`python3 benchmarks/harness_perf/run.py [--smoke] [--references all]`

- **Model removed.** `stub_provider.py` answers every request at once from a
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
| steady round overhead, `noop` | 77.6 ms | ≤ 38 ms (codex) |
| steady round overhead, `shell` | 89.3 ms, p95 535 ms | ≤ 48 ms, p95 ≤ 100 ms (pi) |
| tool round trip, `run_command true` | 285 ms | ≤ 21 ms (pi) |
| round overhead at 20 sessions | 2,029 ms | ≤ 1.5x the one-session figure (references: flat) |
| CPU per model request (all Tofu processes) | 166 ms | ≤ 34 ms (pi) |
| settle after last response | 14.3 ms | keep ≤ 31 ms (Tofu already best) |
| round-0 request | 71–77 KB | ≤ 31 KB (opencode, the smallest full coding toolset); pi's 6.5 KB has 4 tools |
| growth per tool round | 313 B | ≤ 285 B (pi) |
| prefix breaks | 0 | stay 0 |
| idle RSS, all processes | 376 MB | ≤ 300 MB (claude-code single process) |
| execution host cold start | 14.4 s | ≤ 2 s (opencode, the slowest reference to first request) |

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
