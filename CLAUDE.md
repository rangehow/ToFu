# CLAUDE.md — Project Intelligence for AI Assistants

> **This file is the single source of truth for all AI-assisted code modifications.**
> Every change MUST comply with these rules. No exceptions.

---

## 1. Project Overview

**Tofu (豆腐)** is a self-hosted AI assistant with a **Quart** (async Flask)
backend and vanilla JS frontend, served via **Hypercorn** (ASGI). All
existing Flask-style sync route handlers run unchanged in a thread pool;
new endpoints can be `async def` for native async I/O.

> **📐 Full architecture panorama** lives at [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
> (drift-checked directory map + Mermaid) and [`docs/architecture.html`](docs/architecture.html)
> (single-file visual diagram in Claude-Code-style layered cards).  Re-scan those
> files whenever you add a new sub-package or Blueprint.

> **👤 Human-facing docs:** [`README.md`](README.md) / [`README_CN.md`](README_CN.md) are the
> USER-facing product docs (features, how to use them). THIS file is the agent-facing
> rules doc. When you ship a user-visible feature, update the README too — and keep the
> Chinese README in sync.

```
bootstrap.py           — Smart launcher: auto-installs missing pip packages via LLM diagnosis
server.py              — App entry (Quart + Hypercorn), Flask→Quart shim, middleware,
                         logging bootstrap, auto-TLS for HTTP/2, VS Code proxy detection
export.py              — Three-level sanitization export (personal / internal / opensource) — see §10
lib/                   — Core business logic
  log.py               — Centralized logging utilities (get_logger, log_exception, audit_log, log_context)
  # ── Shared infrastructure (used everywhere; see §4.6) ──
  api_response.py      — Unified jsonify helpers: api_ok / api_error / api_not_found /
                         @safe_route — replaces 446 ad-hoc `return jsonify(...)` patterns
  request_parser.py    — Typed JSON body extraction: parse_body / require_str / require_int /
                         optional_list / etc. — raises BadRequest auto-converted to 400
  http_client.py       — Sync (requests) + async (httpx) HTTP with auto-applied proxy.
                         Public helpers: http_get / http_post / http_stream /
                         async_http_get / async_http_post / async_http_stream
  json_store.py        — Atomic JSON file I/O with per-path locking and JSONC tolerance:
                         read_json / write_json_atomic / update_json_atomic / write_text_atomic
  agent_verdict/       — Single source of truth for agent-loop decision logic:
                         classify_verdict (STOP/CONTINUE_WORKER/CONTINUE_PLANNER
                         gating + anti-analysis-spiral overrides), detect_stuck,
                         count_state_changing_rounds, STATE_CHANGING_TOOLS,
                         VU_DONE_SENTINEL. Consumed by endpoint_review,
                         orchestration_engine, autopilot (was hand-copied 4×)
  agent_loop.py        — Shared multi-round tool-calling loop + abort seam:
                         AbortSignal (uniformly wraps threading.Event /
                         task['aborted'] flag / abort_check callback behind one
                         .aborted predicate) + run_agent_loop() owning the round
                         loop and the 3 abort checks (before-round / post-stream /
                         between-tools). Adopters: paper report_engine + qa_engine
                         (2026-07; orchestrator/endpoint/swarm/timer adopt later).
                         The main orchestrator still owns its own loop in
                         lib/tasks_pkg/orchestrator/_run.py — cutover blocked on
                         the run_task locals inventory (~30 fields, see pt_03f4cdf1).
  ttl_cache.py         — Generic in-memory TTL cache with LRU eviction +
                         get_or_compute serialization (TTLCache class)
  task_runtime.py      — Compatibility shim → re-exports TaskRuntime from
                         lib/agent_core/task_runtime.py (relocated 2026-06). See §13.
  push.py              — Compatibility shim → re-exports PushHub + push_event() from
                         lib/agent_core/push.py — backs the /api/push WebSocket multiplexer (see §4.7)
  agent_core/          — Reusable agent base, as one browsable package (relocated 2026-06):
                         run loop, dispatch, endpoint loop, compaction, push hub,
                         TaskRuntime, capability profiles, streaming-event contract.
    __init__.py        — Lazy (PEP 562) facade; CORE_MEMBERS maps symbol → defining module
    task_runtime.py    — TaskRuntime implementation (real home; lib/task_runtime.py shims it)
    push.py            — PushHub + push_event() (real home; lib/push.py shims it)
    events.py          — EventType / EventSpec streaming-event contract
    profiles.py        — Agent capability profiles (apply_profile / get_profile)
  llm/                 — LLM API communication (package)
    __init__.py        — Public facade re-exporting all symbols
    body.py            — Model-aware build_body(), image validation/downscaling
    cache.py           — Anthropic prompt-cache breakpoints
    chat.py            — Non-streaming chat() entry point
    stream.py          — SSE streaming + tool-call accumulation
    astream.py         — Async SSE streaming variant
    _sse_core.py       — Shared SSE parsing + tool-call accumulation core
    anthropic_outbound.py — Anthropic-native outbound request builder
    diagnostics.py     — RawSSEDumper (anomaly ring buffer + opt-in transcript)
    _transport.py      — Retry config, headers(), chat_url(), abortable_sleep()
  llm_errors.py        — Exception classes + HTTP error classifier
  llm_sanitize/        — Message sanitization (gateway terms, orphan tool calls, role merging)
  llm_dispatch/        — Dynamic model routing / load balancing (package)
    api.py             — High-level dispatch_chat / dispatch_stream entry points
    config.py          — Model alias + routing tables
    discovery.py       — Model capability discovery
    dispatcher.py      — Core dispatch loop, fallback chains
    factory.py         — Client/provider factory
    slot.py            — Slot / key-pool state management
  model_info/          — Per-model capabilities + _clamp_max_tokens (see §12):
                         _capabilities.py / _family.py / _limits.py / _max_output.py +
                         capability_taxonomy.py (single source of truth for
                         CHAT_EXCLUDED_CAPS + DISPATCHER_NON_CHAT_CAPS — see §3.7-adjacent
                         note; frontend consumes it via static/js/core/model_caps.js)
  tools/               — Tool definitions (package)
    project.py         — list_dir, read_files, grep_search, find_files, write_file, apply_diff, …
    search.py          — web_search
    browser.py         — fetch_url, browser automation
    meta.py            — meta/planning tools (create_plan, etc.)
    human_guidance.py  — ask_user / request_human_input
    image_gen.py       — image generation tool wrappers
    code_exec.py       — sandboxed code execution
    conversation.py    — conversation-control tools
  tasks_pkg/           — Task orchestration, compaction, execution. Most large
                         modules are now facade-preserving PACKAGES (dir/ with a
                         re-exporting __init__.py), not single files:
    orchestrator/      — Main run_task loop (facade __init__.py + _run.py) +
                         extracted slices (pt_03f4cdf1): _vu_startup / _prefetch /
                         _context_inject / _tool_history / _post_loop / _teardown /
                         _finalize (SSE emission + autopilot baton) / _turn
    manager/           — Task registry + lifecycle (_registry / _persist / _recovery /
                         _stream / _sync / _events / _maintenance / _state)
    commit_round/      — Per-round file-history snapshot (daemon-thread
                         make_snapshot + round_committed SSE) +
                         derive_round_modified_files (extracted from orchestrator)
    auto_translate/    — Server-side auto-translate safety net for assistant +
                         endpoint-critic messages (extracted from manager)
    executor/          — Tool execution entry (+ content_ref resolution)
    executor_image/    — Image-gen tool execution path
    streaming_tool_executor.py — Streaming-tool variant (single file)
    tool_dispatch/     — Tool-name → handler routing
    tool_display/      — Tool UX; tool_hooks.py (single file) — before/after hooks
    endpoint/          — Endpoint mode (Planner→Worker→Critic loop; _run / _replan /
                         _sync / _translate)
    endpoint_prompts/  — Prompts; endpoint_review.py (single file) — critic/planner turns
                         (verdict parsing delegates to lib/agent_verdict/)
    compaction/        — Context-window compaction (3-layer; _layer1 / _manual /
                         _reactive/ / _steps / _tokens …)
    cache_tracking/, llm_fallback/, stream_handler/ — Prompt-cache
                         tracking, model-swap fallback, stream classifier
    system_context/, message_builder/, conv_message_builder/,
    server_message_store/ — Context assembly, message transforms, per-turn
                         file injection (system_context/: _inject / _profile /
                         _reminders / _search)
    model_config.py, attachments.py — Per-model config, attachment handling (single files)
    approval.py, human_guidance.py, stdin_handler.py — Write-approval,
                         ask_user, blocking stdin requests (single files)
    handlers/          — Per-tool execution handlers (misc, project, search, browser, mcp, memory, code_exec, _adapter)
    segments/          — Per-turn message segments (thinking / assistant / tool)
                         used by the wire-fingerprint and killed-recovery paths
    system_prompt_cc/  — System-prompt cache-control (Anthropic breakpoint) helpers
    autopilot.py, autopilot_markers.py, autopilot_state.py — VU autopilot loop,
                         baton handoff markers, CAS state store (pt_00459503 target)
    wire_fingerprint.py, wire_messages.py — On-wire message identity + fingerprints
                         (drives resume / recovery / stream-retry adoption)
    killed_recovery.py — Rebuild task['segments'] + toolRounds after hard-kill
    entry.py           — build_chat_config + spawn_task entry points
    floor_retry.py, turn_retry.py — Cache-floor collapse retry + turn-level retry policy
    event_fold.py, event_log.py — SSE fold / dedup + event log persistence
    persist_registry.py, persistence_store.py — Task-result persistence adapters
    chat_mode.py       — Air/Studio 2-tier capability profile (see §3.7)
    write_breakdown.py — Per-round file-write attribution
  project_mod/         — Project file tools (list/read/write/grep/run)
    tools.py           — Tool dispatch facade: execute_tool registry
                         (_EXEC_HANDLERS name→handler) + re-exports
    run_command.py     — run_command subsystem (shell exec, process-tree kill,
                         snapshot/diff, destructive-command guards) — extracted
                         from tools.py
    read_tools.py      — Absolute + project file reading (images/PDF/Office)
    write_tools.py     — write_file / apply_diff / insert_content
    indexer.py, scanner.py, modifications.py, config.py
  swarm/               — Multi-agent orchestration (master, agent, scheduler, planner, review, registry, rate_limiter, artifact_store, synthesis, integration, events, tools, types, …)
  optimizer/           — Nightly self-tuning loop: analyzer → proposer (LLM)
                         → applier → storage. Whitelist auto-apply + ttl_days
                         revert. See routes/api_v1/optimizer.py (REST surface)
                         and lib/optimizer/actions/ for the action registry.
  browser/             — Browser automation (advanced, playwright pool, queue, handlers)
  # NOTE: web search + fetch were EXTRACTED to the standalone `tofu_search`
  # package (orchestrator, engines, rerank, dedup, HTTP/HTML/PDF extraction,
  # content filter). They are NO LONGER in-tree; chatui seams via
  # lib/search_bridge.py + lib/tools/search.py / lib/tools/browser.py. See §11.
  mcp/                 — Model Context Protocol client, registry, config
  memory/              — Memory / stored-notes layer (storage, relevance, injection, tools).
                         MODEL-authored experience notes: flat *.md files at
                         <project>/.tofu/memories/ + <data>/memories/global/,
                         discovered by BM25 prefetch + search_memories.
  skills/              — USER-installed skill packages (Anthropic AgentSkills
                         format) — a DIFFERENT NOUN from memory/ (decoupled
                         2026-07, epic pt_229606ca): registry (enumerate /
                         uninstall), injection (the always-visible
                         <available_skills> index, spliced as its own cache
                         block by system_context/_inject.py), activate
                         (activate_skill progressive-disclosure loader —
                         returns the SKILL.md guide + bundled-file manifest),
                         installer (zip → validated package), catalog
                         (curated store). Packages live at
                         <project>/.tofu/skills/<id>/ + <data>/skills/global/<id>/.
                         The model channel is READ-ONLY (one tool:
                         activate_skill); install/uninstall/toggle are
                         user-only (Settings → Skills / routes/api_v1/skills.py);
                         memory CRUD refuses packages (_guard_not_package) and
                         packages are excluded from the memory corpus
                         (get_eligible_memories include_packages=False).
  conversations/       — Conversation persistence + the Project Brain (cross-conversation
                         coordination): charter, board/epics, activity feed, peer messaging
                         (project_peer.py), path leases, status lane, reconcile. See
                         docs/PROJECT_BRAIN.md + docs/PROJECT_BRAIN_STATUS_LANE.md.
  oauth/               — OAuth subscription login (Claude Pro/Max, ChatGPT Codex):
                         manager, PKCE, token store. outbound.py bridges a logged-in
                         subscription into a managed server_config provider (the slot's
                         `oauth` marker → live token + client-identity spoof headers +
                         Claude identity system block, resolved per request)
  feishu/              — Feishu/Lark bot pipeline, events, messaging
  scheduler/           — Cron + timer + proactive agent scheduler
  pdf_parser/          — PDF parsing (text, images, math, VLM, postprocess)
  cross_dc/            — Cross-DC FUSE latency detection (env-var driven, auto-benchmarks)
  compat/              — Cross-platform shim (_platform.py: Linux/macOS/Windows) +
                         OpenAI/Anthropic API-compat adapters (openai.py, anthropic.py)
  fs_keepalive.py      — Linux-only FUSE/NFS mount keepalive
  billing/             — Wallet / ledger / pricing / per-user cost accounting (payments/ sub-pkg)
  paper/               — Reading-Mode engine extracted from routes/paper.py:
                         report_engine, translate_engine, prompts, images, arxiv, tools
  motion_video/        — Motion-graphics video pipeline (docs/MOTION_VIDEO_DESIGN.md
                         renders; docs/PRODUCTION_PIPELINE_DESIGN.md P4/P5 front half):
    _recipe.py         — topic → scenes.json recipe: research → script → timeline
                         (fact cards must carry a real source URL; the SRT is timed
                         from REAL TTS audio, never a chars/second estimate)
    _scene_author.py   — per-scene composition author (P5): a bounded run_agent_loop
                         with a NARROW toolset (write_composition / composition_check /
                         web_search / fetch_url — no render reachable). Every failure
                         degrades that ONE scene to _template.py, so a bad scene can
                         never fail the film. Caps: max_rounds + per-scene token budget.
                         Default OFF (TOFU_MOTION_SCENE_AUTHOR / per-job scene_author).
    _template.py       — zero-LLM composition floor (always-valid fallback)
    _storyboard.py, _srt.py, _gates.py, _render.py, _concat.py, _audio.py, _env.py
    engine.py          — headless worker: recipe → storyboard → narrate → compose →
                         render (bounded pool) → concat → sidecar → mux. Crash-resume
                         via job.json + the stage checkpoint; already-rendered scenes
                         and already-authored compositions are never redone.
    runtime.py         — TaskRuntime + dedup index for motion jobs
  longform/            — Long-form research report capability (Production
                         Substrate P7 — the THIRD recipe, written to MEASURE the
                         substrate rather than to refactor it):
    recipe.py          — research → outline → sections(×N) → assemble. The stage
                         list is DATA-DEPENDENT (one stage per outline section),
                         which the static video stage list never exercised; it
                         rides the existing checkpoint contract unchanged by
                         running the graph twice against ONE state file.
    engine.py          — worker; publishes the report as a markdown artifact.
    runtime.py         — TaskRuntime + dedup index. NOTE: ~67% byte-identical to
                         motion_video/runtime.py after renaming — this, plus the
                         manifest/rescan pair, is the measured evidence for what
                         P6's ProductionRuntime should absorb.
                         Ships ZERO bespoke poll/abort routes: the generic
                         /api/v1/tasks/* endpoints serve it.
  production/          — Production Substrate (docs/PRODUCTION_PIPELINE_DESIGN.md).
                         The horizontal layer under every "one sentence → finished
                         product" capability; each capability keeps its own thin recipe.
    runtime.py         — ProductionRuntime: a thin layer OVER TaskRuntime holding
                         what every capability hand-rolled on top of it — dedup index
                         (liveness-checked + self-pruning), create-with-field-shape,
                         append+touch, stale sweep keyed on updated_at, id minting.
                         Extracted because the per-capability runtime.py measured
                         67% byte-identical across THREE samples. All three
                         (motion-video / paper-podcast / longform-report) ride it;
                         their legacy _X_runtime names still resolve to the same
                         TaskRuntime the /api/v1/tasks discovery finds.
    jobs.py            — job manifest write/read + crash-resume rescan. One
                         implementation of the scan that re-spawns every job whose
                         manifest still says `running` after a process death.
    stages.py          — stage-graph contract: Stage(name, run, gate, retry, resumable)
                         + a checkpointed runner. A stage's artifact is committed to the
                         state file as soon as its gate passes, so a killed process
                         resumes at the first UNFINISHED stage — crash-resume is a
                         CORRECTNESS contract here, not a cost optimization.
                         P6 slice 1 relocated this VERBATIM from
                         lib/motion_video/_stages.py (which remains a re-export shim).
                         Deliberately capability-agnostic — a guard test AST-asserts it
                         imports no motion_video/tts/llm/paper/audio module.
                         NOT here (deliberate, evidence-based): the binary
                         `deliverable` channel, progress double-projection and the
                         artifacts binary format. The third recipe never needed
                         them, so they are video/podcast commonalities rather than
                         global ones — extracting them would fit the wrong shape.
  # NOTE: the trading subsystem was EXTRACTED to a standalone `tofu-trading`
  # package (2026-06) and is no longer in-tree. It mounts via the
  # `tofu.blueprints` / `tofu.startup` entry-point groups (see routes/plugin_registry.py).
  database/            — Dual-backend DB layer (PostgreSQL primary, SQLite fallback)
    _core.py           — Connection factory, pool, config (PG_* / TOFU_DB_PATH; legacy CHATUI_DB_PATH still honored)
    _bootstrap.py      — Auto-bootstrap local userspace PG; fallback to SQLite
    _core_schema.py    — SINGLE source of every table definition (SQLAlchemy
                         Core). Migration COMPLETE (2026-06): one Table() →
                         byte-equivalent PG + SQLite DDL. Defining/altering a
                         table happens HERE.
    _schema_pg.py      — PG bootstrap: create_if_absent(Core tables) + PG-only
                         extras (indexes, tsvector/GIN/trigger, ALTER migrations)
    _schema_sqlite.py  — SQLite bootstrap: create_if_absent(Core tables) +
                         SQLite-only extras (indexes, FTS5, ALTER migrations)
    _sql_translate.py  — Permanent SQLite→PG dialect bridge at the wrapper layer
                         (? → %s, json_extract, strftime, …; runs on every PG
                         query). NOT deprecated — only its INSERT OR REPLACE
                         upsert branch is superseded by _core_schema.upsert()
                         for migrated tables; in-tree _PK_MAP entries are gone,
                         leaving only external tofu-trading tables.
    _wrappers.py       — Uniform execute() / fetchone() / fetchall() API
routes/                — Quart Blueprints. Top-level: chat (+ chat_helpers /
                         chat_queue / chat_human_io / chat_tool_state / chat_state /
                         chat_side_effects / chat_task_start / chat_poll_abort —
                         the chat_send/chat_stream fat-handler seams), conversations
                         (+ _search / _compaction), common, desktop, oauth, translate,
                         upload, artifacts, browser, paper, push, compat_openai,
                         compat_anthropic, api_docs, metrics, legacy_redirects.
  api_v1/              — Headless `/api/v1/*` surface (the canonical API — see §15):
                         agents, agent_run, auth, billing, capabilities, chat,
                         conversations, daily_report, folders, keys, logs, mcp, memory,
                         skills, oauth, optimizer, orchestrations, paper, project, providers,
                         scheduler, swarm, tasks, translate, update, users, webhooks, …
  __init__.py          — ALL_BLUEPRINTS + register_all(); plugin blueprints mount via
                         routes/plugin_registry.py (entry-point groups — see §4.1)
  plugin_registry.py   — Pluggable Blueprint / startup-hook / TaskRuntime discovery
  _task_routes.py      — register_task_routes() factory: auto-generates /poll + /abort
static/js/             — Frontend (vanilla JS). Unified API client api.js (§3.2.0); large
                         monoliths decomposed (2026-05-28) into subpackages:
                         core/ (folders, conversations, markdown, safe_html,
                         model_caps — capability-taxonomy bridge, health_stream_timer,
                         cross_tab_sync, icons, …),
                         ui/ (chat_render, streaming_render, sse_* handlers + pipeline, …),
                         main/ (send pipeline, conv lifecycle, init, …),
                         settings/ (provider_render, key_stats, mcp, oauth, …).
                         Feature modules: paper-reader, project, memory, skills, orchestration,
                         translation, upload, image-gen, artifacts, branch, myday, optimizer,
                         scheduler, timer, update, relay-admin, compaction-viewer, context-bar,
                         push, idb-cache, export-images, log-clean, i18n. Bundled by
                         lib/js_bundler.py (_BUNDLE_FILES — see §3.2.1).
static/                — CSS (styles.css)
debug/                 — Standalone test/benchmark scripts
tests/                 — pytest-style suites + standalone runners. Suites for the
                         shared infrastructure modules:
                           test_api_response.py, test_json_store.py, test_ttl_cache.py,
                           test_request_parser.py, test_http_client.py,
                           test_task_runtime.py, test_server_async.py,
                           test_agent_loop.py, test_server_config_concurrent_writers.py,
                           test_chat_manager_migration.py, test_paper_migration.py,
                           test_translate_migration.py,
                           test_trading_simulator_migration.py
                         Migration scripts (one-shot, idempotent, dry-run-safe):
                           _migrate_api_response.py, _migrate_request_parser.py,
                           _migrate_http_client.py — see §14 for usage rules
logs/
  app.log              — Business logic only (lib.*, routes.*, server) INFO+, daily rotation, 30 days
  access.log           — HTTP request log (werkzeug), daily rotation, 14 days
  error.log            — WARNING/ERROR/CRITICAL from ALL sources (5 MB × 10)
  vendor.log           — Third-party libraries WARNING+ (5 MB × 3)
  audit.log            — Structured JSON audit trail
```

---

## 2. ⚠️ MANDATORY: Logging Discipline

> **The #1 rule of this project: every code path that can fail MUST leave a trace in the log file.**
> Silent failures are the enemy. If a bug happens in production, `logs/error.log` (for errors) and `logs/app.log` (for business context) must contain enough information to diagnose it without a debugger.

### 2.1 Every Python file MUST have a logger

```python
# At the top of every .py file in lib/ and routes/:
from lib.log import get_logger
logger = get_logger(__name__)
```

**Never use `print()` for diagnostics.** Always use the logger — it provides level, module name, thread info, and routes to the correct log file.

### 2.2 Exception handling — ZERO silent catches

```python
# ❌ FORBIDDEN — silent catch
try:
    result = do_something()
except Exception:
    pass

# ❌ FORBIDDEN — catch-and-return without logging
try:
    result = do_something()
except Exception:
    return None

# ✅ CORRECT — always log, then handle
try:
    result = do_something()
except Exception as e:
    logger.warning('do_something failed: %s', e)
    return None

# ✅ CORRECT — for truly expected/harmless exceptions, use debug level
try:
    count = int(maybe_string)
except (ValueError, TypeError) as e:
    logger.debug('Non-numeric value, defaulting to 0: %s', e)
    count = 0

# ✅ CORRECT — for unexpected errors, use error + traceback
try:
    data = complex_operation()
except Exception as e:
    logger.error('complex_operation failed: %s', e, exc_info=True)
    raise
```

**Rules:**
| Scenario | Level | `exc_info` | Example |
|---|---|---|---|
| Expected / harmless fallback | `debug` | optional | Parse int, optional file |
| Unexpected but recoverable | `warning` | `False` | API timeout, retry |
| Unexpected, degraded behavior | `error` | `True` | Tool execution failure |
| Fatal / unrecoverable | `critical` | `True` | DB corruption |
| Retry loop (each attempt) | `warning` | `False` | Stream retry |
| Retry loop (final failure) | `error` | `True` | All retries exhausted |

### 2.3 Use `log_context` for operations > 1 second

```python
from lib.log import log_context

# Automatically logs start, duration, and any exception
with log_context('rebuild_project_index', logger=logger):
    indexer.rebuild()
```

### 2.4 Use `log_exception` for catch-and-reraise

```python
from lib.log import log_exception

try:
    process(data)
except Exception:
    log_exception(logger, 'Failed to process data for conv=%s', conv_id)
    raise
```

### 2.5 Use `audit_log` for significant state changes

```python
from lib.log import audit_log

audit_log('model_switch', old_model=old, new_model=new, reason='rate_limit')
audit_log('task_complete', task_id=tid, tokens_used=usage)
```

### 2.6 Log content guidelines

- **Include context**: conv_id, task_id, model name, URL, file path — whatever helps grep.
- **Use %-style formatting** (lazy evaluation): `logger.info('Fetched %s in %.1fs', url, elapsed)` — NOT f-strings.
- **Sanitize secrets**: never log API keys, tokens, or full request bodies with credentials.
- **Truncate large data**: `logger.debug('Response preview: %.500s', body)` — don't dump 100 KB into the log.
- **Structured prefix**: Use `[Module]` or `[op:name]` prefix for easy grepping: `logger.info('[LLM] Streaming started model=%s', model)`.

---

## 3. Code Style & Conventions

### 3.1 Python

- **Imports**: stdlib → third-party → `lib.*` → `routes.*`, blank line between groups.
- **Logger init**: Always `from lib.log import get_logger; logger = get_logger(__name__)`, placed right after imports.
- **Type hints**: Encouraged on public functions; optional on internal helpers.
- **Docstrings**: Required on modules and public functions. Use Google-style:
  ~~~python
  def fetch_page(url: str, timeout: int = 15) -> str:
      """Fetch a web page and return its text content.

      Args:
          url: The URL to fetch.
          timeout: Request timeout in seconds.

      Returns:
          Extracted text content, or empty string on failure.
      """
  ~~~
- **Constants**: UPPER_SNAKE_CASE at module level.
- **Private helpers**: Prefix with `_` (e.g., `_parse_sse_line()`).

### 3.2 JavaScript (static/js/)

- Vanilla JS only — no frameworks, no build step.
- Module pattern: each file exposes functions via `window.*` or direct calls in `main.js`.
- Use `console.warn` / `console.error` sparingly for client-side diagnostics.

#### 3.2.0 ⚠️ Unified API Client — `static/js/api.js`

> **Every backend HTTP call from the frontend MUST go through
> `window.Api.<domain>.<method>(...)`.  No JS file other than `api.js`
> may issue a raw `fetch('/api/...')` or `fetch(apiUrl('/api/...'))`.**

This is the single seam between frontend and backend. It exists so:
- migrating an endpoint from legacy → `/api/v1` touches one file;
- cross-cutting concerns (timeout, error shape, auth) live in one place;
- the frontend stays a thin renderer, never re-implementing backend logic
  (see `.tofu/skills/separation-of-concerns-directive.md`).

The rule is enforced by `tests/test_frontend_api_isolation.py`, which
maintains a per-file ratchet (`BASELINE`) of remaining legacy calls.
The count must monotonically decrease — CI fails if any file's count
grows or a new file calls `/api/...` directly.

**Migration playbook**: see [`docs/api_client.md`](docs/api_client.md).
**Public surface**: `Api.request / get / post / put / patch / del / stream`
plus per-domain methods (`Api.folders.*`, growing). Errors throw
`ApiError`; pass `{onError: 'null'}` for best-effort fetches.

**Streaming exceptions** (allowed because they don't fit the JSON
verb model):
- Real-time push events → `pushSubscribe(channel, taskId, fn)` from `push.js`.
- SSE chat stream `/api/chat/stream/<id>` → consumed via
  `Api.chat.streamResponse(taskId, {signal})`, which returns the raw
  Response so the caller (`ui/sse_pipeline.js`, `branch.js`) can pipe
  `.body.getReader()`. No file calls this endpoint with a raw `fetch`.

#### 3.2.1 ⚠️ JS Bundler Allowlist — DO NOT FORGET

> **Every new top-level `static/js/*.js` file MUST be added to `_BUNDLE_FILES`
> in `lib/js_bundler.py`. Otherwise it loads as a silent no-op in production.**

`routes/common.py` rewrites `index.html` on every `GET /` and replaces all
individual `<script defer src="static/js/*.js">` tags with a single
`bundle-<hash>.js` tag. The bundle is built by concatenating only the files
listed in `_BUNDLE_FILES` (in order). Files NOT in that list are still
**stripped** from the served HTML by the `_APP_SCRIPTS_RE` regex
(`routes/common.py:371-405`), but never **added back** by the bundler →
the `<script>` tag silently disappears, no 404, no console error.

**Symptom**: file exists on disk, `<script>` tag in your local
`index.html`, but `typeof window.yourFunction === 'undefined'` in the
browser and `document.getElementById('yourElement')` returns `null`.

**Fix when adding a new top-level JS module:**
1. Add the filename to `_BUNDLE_FILES` in `lib/js_bundler.py` in the
   correct dependency order. Hard rules:
   - `i18n.js` MUST stay first (`t()` is used everywhere).
   - `main.js` MUST stay last (it boots the app).
   - Files that read globals declared in `main.js` (e.g. `conversations`,
     `activeConvId`, `config`) at IIFE-load time go AFTER `main.js`.
   - Files referenced only at runtime (inside function bodies) can go
     anywhere before `main.js`.
2. Keep the `<script defer src="static/js/foo.js?v=...">` tag in
   `index.html` for the dev-mode fallback (when bundling fails the
   original tags are served — see `routes/common.py:380-384`).
3. Restart the server (the bundler is pure-Python, no hot-reload). On
   startup, the log line `[Bundle] Built bundle-XXXXXXXX.js (N files, ...)`
   should show `N` increased by 1.
4. Hard-refresh the browser — the bundle filename changes via content
   hash, so a soft reload would re-use the cached old bundle.

**Audit command** (lists files referenced in `index.html` but missing
from `_BUNDLE_FILES`):
```bash
diff <(grep -oE 'static/js/[a-z_-]+\.js' index.html | sed 's|static/js/||' | sort -u) \
     <(python3 -c "from lib.js_bundler import _BUNDLE_FILES; [print(f) for f in _BUNDLE_FILES]" | sort)
```

### 3.3 HTML/CSS

- `index.html` is a single-page app with inline structure.
- CSS is in `static/styles.css` (main).
- Dark theme with CSS variables at `:root`.

### 3.4 Icons — SVG Only, NO Emoji

> **Emojis are PROHIBITED as icons anywhere in the UI. Every icon MUST be an
> SVG — official brand logos for real products/services, and a suitable SVG
> glyph for generic concepts. No exceptions.**

**Rules:**
- **Brand/product icons**: Use the actual official SVG logo (Feishu/Lark, Google,
  GitHub, Docker, …). Never a generic emoji or a random SVG path.
- **Generic-concept icons** (credentials, workspace, access control, warnings, …):
  Use an SVG glyph too — NOT an emoji like 🔑 / 📂 / 👥 / ⚠️.
- **Search first**: Before adding any brand icon, search the web for the official SVG (check sources like
  [dashboard-icons](https://github.com/homarr-labs/dashboard-icons),
  [Simple Icons](https://simpleicons.org/), or the brand's own asset page).
- **Inline SVG preferred**: Embed the SVG inline in HTML for color control and no external dependency.
- **Size to context**: Use `width`/`height` attributes matching the surrounding text size (e.g. 15×15 for tab buttons, 20×16 for section titles).
- **Never use**: 💬 for Feishu, 🔍 for Google, 🐙 for GitHub, etc. — always the real logo.
- **No unicode glyphs as controls either**: `⤢` / `−` / `+` / `⟳` etc. are
  glyphs with font-dependent metrics that render off-center and font-fallback
  differently across platforms — same prohibition as emoji. Use an inline SVG.

**Alignment — the decision rule (this is a recurring bug class):** an inline
`<svg>` sits on the text baseline and reserves descender space, so it renders
~2–3px low next to text and floats off-center in a fixed box. Two correct
patterns:

- **Standalone affordance** (button, tile, logo+label row, toolbar control) →
  make the PARENT a flex box and give the icon `display:block`. Use the shared
  `.icon-box` utility (`static/styles.css`, base layer):
  `display:inline-flex;align-items:center;justify-content:center` + `>svg{display:block}`.
  Just add `class="icon-box"` to the icon's container.
- **Icon inline within a sentence** → use `vertical-align` (the `Icon()` helper
  in `static/js/core/icons.js` already bakes in `vertical-align:-0.125em`), and
  match the icon `height` to the surrounding `font-size`.

Traps: (1) `vertical-align` does NOTHING on a flex child — use `align-items:center`
instead; (2) a centered flex box still looks low if you forget `svg{display:block}`
(that line, not the parent centering, removes the descender gap); (3) `Icon(name,size)`
already carries `vertical-align`, so don't also fight it — inside a flex parent just
let `.icon-box`'s `display:block` win. The `.icon-box` invariant is guarded by
`tests/test_frontend_icon_box_alignment.py`.

> **Scope note**: this rule governs *icons* (visual UI affordances). It does NOT
> touch the separate structured-protocol tokens (e.g. the `✅`/`❌` critic-verdict
> markers in `lib/tasks_pkg/endpoint_prompts.py` / `endpoint_review.py`), which are
> parsed by code, not rendered as icons — leave those alone.

### 3.5 No Hardcoded Environment-Specific Values

> **This project is open-source. Never hardcode paths, hostnames, datacenter names,
> cluster identifiers, internal domain names, or any environment-specific values directly in code.**

**Rules:**
- **Use environment variables** for anything that varies between deployments (storage paths, IDC names, cluster endpoints).
- **Use config files** (`data/config/*.json`) for values that users should be able to tune without editing code.
- **Provide sensible defaults** that work on a vanilla machine (or gracefully disable the feature if no env var is set).
- **Document the env vars** in docstrings, not in hardcoded examples with real values.
- **Probe / auto-detect** at runtime where possible (e.g., benchmark latency instead of maintaining a list of "known remote datacenters").
- **In docstrings and comments**, use generic placeholder names (`cluster-A`, `/mnt/storage/...`, `datacenter-X`) — never real infrastructure names.

```python
# ❌ FORBIDDEN — hardcoded infrastructure
REMOTE_CLUSTERS = ['sh02-training', 'hldy-training']  # Shanghai, Hailidao
if hostname.startswith('set-zw05'):
    local_dc = 'beijing'

# ✅ CORRECT — environment-driven, auto-detected
_CLUSTER_MOUNTS_ENV = os.environ.get('CROSS_DC_CLUSTER_MOUNTS', '')
_LOCAL_IDC = os.environ.get('CROSS_DC_LOCAL_IDC', '')
# Benchmark latency at startup to classify local vs remote
```

---

### 3.6 Agent-Written Project Artifacts — the `.tofu` prefix convention

> **Any hidden file or directory the assistant writes INTO a user's project
> (not the Tofu install) MUST be named with the `.tofu` prefix, and MUST be
> declared in `lib/agent_artifacts.py`. Never invent a bare/un-prefixed name.**

As the assistant works it deposits runtime state in the user's project:
`.tofu/` (file-history backups + memories + skills), `.tofu_trash/` (recoverable
deletes), `.tofu_sandbox/` (restricted-run shims), `.tofu_env.json` (env
marker). Many independent mechanisms must recognise these as "agent junk, not
source": `.gitignore` generation (`lib/project_mod/indexer.py`), the export
sanitizer (`export.py`), the self-update preserve/skip lists
(`lib/self_update/`), and the MCP vendor-copy excludes (`lib/mcp/client/`).

Historically each kept its OWN hardcoded list, so a new artifact had to be
added in ~5 places — and forgetting one silently leaked it (committed to git,
copied into exports, flagged as a dirty tree blocking updates). The fix is the
single-source-of-truth registry **`lib/agent_artifacts.py`**:

- `ARTIFACT_PREFIX` (`.tofu`) + `is_agent_artifact(name)` — the prefix predicate.
- `GITIGNORE_PATTERN` (`.tofu*`) — one glob covering every present/future artifact.
- `KNOWN_ARTIFACT_NAMES` + per-artifact constants (`TRASH_DIR`, `SANDBOX_DIR`, …).

**Rules when adding a NEW artifact:**
1. Name it `.tofu_<something>` (the reserved prefix is what makes every
   consumer recognise it mechanically).
2. Add the canonical name as a constant in `lib/agent_artifacts.py` and import
   it in the producer — define the name ONCE.
3. Consumers should call `is_agent_artifact()` / use `GITIGNORE_PATTERN`, never
   re-list literal names. If you find a consumer with its own hardcoded `.tofu`
   list, migrate it to the registry rather than extending the list.


### 3.7 App-Personal vs Headless Capabilities — the `personal_scope` registry

> **Tofu is two products sharing one orchestrator: the interactive app (chat
> UI) where the owner's personal state is ON by default, and the headless agent
> runtime (`/api/v1/agent/run`, `/api/v1/chat/completions`, `/chat/stream-direct`,
> the OpenAI/Anthropic compat surfaces, the in-process `tofu.chat` facade) where
> the server is a stateless executor for a BYO caller. Any capability that
> injects the OPERATOR's personal state into the prompt — the memory store, the
> personal preference profile, and anything similar added later — MUST be
> declared in `lib/agent_core/personal_scope.py` and fails CLOSED on every
> headless surface.**

**Why.** Defaults are sticky and invisible. `memoryEnabled` defaults to `True`
for the UI (`lib/conv_config/`), and historically every headless cfg-builder
inherited that default unless it remembered to override it — so a shared/
multi-tenant deployment would splice the operator's memories (and the global
`.tofu_user_profile.md` preference file) into an unrelated API caller's prompt.
That is both a hallucination vector and a privacy/isolation leak. The
prompt-assembly side already suppresses a capability's description when its flag
is off (`system_context/_inject.py` gates `<memory_accumulation>` / `[USER PREFERENCE
PROFILE]`), so the fix is purely about the DEFAULT a headless caller lands on.

**The mechanism (single source of truth):**
- `PERSONAL_CAPABILITIES` in `lib/agent_core/personal_scope.py` — one entry per
  app-personal capability (cfg key + fail-closed `headless_default` + `ui_default`
  + the prompt block it gates).
- `apply_headless_personal_defaults(cfg)` — called ONCE by every headless
  cfg-builder AFTER merging the caller's explicit cfg (`build_chat_config` in
  `lib/tasks_pkg/entry.py`; `_build_cfg` in `routes/api_v1/agent_run.py`;
  `translate_openai_request` / `translate_anthropic_request` in `lib/compat/`).
  It's `setdefault`-based so an explicit caller opt-in (`config.memory=true`,
  `config.preferences=true`, or the raw `memoryEnabled`/`preferencesEnabled`
  keys) ALWAYS wins.
- The UI builder `resolve_conv_config` does NOT call it — the interactive
  product keeps its default-on behaviour, byte-identical.
- The preference profile is its OWN capability (`preferencesEnabled`), decoupled
  from `memoryEnabled` via `resolve_preferences_enabled()` (UI back-compat:
  absent flag → falls back to the memory toggle).

**Rules when adding a NEW capability that injects operator-personal state:**
1. Add ONE `PersonalCapability` entry to `PERSONAL_CAPABILITIES` with
   `headless_default=False`.
2. Gate its prompt injection in the `system_context/` package (`_inject.py`
   for memory, `_profile.py` for the preference profile) on the flag (so an
   un-provided capability is never described to the model).
3. Do NOT add per-surface `setdefault` overrides — the single
   `apply_headless_personal_defaults` call already covers every headless
   builder. The ratchet test `tests/test_personal_scope_headless.py` fails if a
   registered capability isn't honoured fail-closed across all surfaces.

---

## 4. Architecture Patterns

### 4.1 Flask Blueprint registration

All routes live in `routes/*.py` as Blueprints. `routes/__init__.py` → `register_all(app)` wires them.
The authoritative core list is `ALL_BLUEPRINTS` in `routes/__init__.py`. Optional feature bundles
(e.g. the now-external `tofu-trading` package) are NOT imported here — they mount via the
`tofu.blueprints` / `tofu.startup` entry-point groups discovered by `routes/plugin_registry.py`.
The canonical API surface is `/api/v1/*` (`routes/api_v1/`); legacy `/api/*` routes redirect there
(see §15). New endpoints land on `/api/v1/*` first.

```python
# routes/my_feature.py
from flask import Blueprint
my_bp = Blueprint('my_feature', __name__)

@my_bp.route('/api/my-feature', methods=['POST'])
def handle():
    ...
```

### 4.2 Task lifecycle (SSE streaming)

1. Client POSTs to `/api/chat/start` → creates a task dict in memory.
2. Background thread runs `orchestrator.run_task(task)`.
3. Task appends SSE events via `append_event(task, ...)`.
4. Client polls `/api/chat/stream/<id>` for SSE events.
5. On completion, result is persisted to the database (PG if available, else SQLite) via `persist_task_result()`.

**Logging checkpoint**: every stage transition MUST be logged:
```python
logger.info('[Task:%s] status=%s → %s', task_id, old_status, new_status)
```

**Endpoint mode (Planner → Worker → Critic)** — `lib/tasks_pkg/endpoint/`
runs a three-way critic verdict loop:
- **`[VERDICT: STOP]`** → terminate (approved).
- **`[VERDICT: CONTINUE_WORKER]`** → inject critic feedback as a user
  message, back to the Worker with the SAME plan.
- **`[VERDICT: CONTINUE_PLANNER]`** → run a fresh Planner turn, reset
  the Worker context to `[system, user(new plan)]`, then back to the
  Worker. Guarded by `MAX_REPLANS=3`.
- A defense-in-depth guard in `_parse_verdict` (`endpoint_review.py`)
  automatically overrides STOP→CONTINUE_PLANNER if the feedback body
  still contains `❌` / "NOT met" / "still failing" / "unresolved".
- Kill switch: `TOFU_ENDPOINT_REPLAN=0` (legacy `CHATUI_ENDPOINT_REPLAN=0`)
  downgrades CONTINUE_PLANNER to CONTINUE_WORKER and disables the override guard.
- SSE event `endpoint_critic_msg` carries `next_phase` (new) and mirrors
  `should_stop` (legacy). Frontend reads `next_phase` and creates a
  replan Planner placeholder when `'planner'`.

### 4.3 LLM client flow

`lib/llm/body.py::build_body()` constructs model-specific payloads.
`lib/llm/stream.py::stream_chat()` handles SSE streaming with retry logic.
High-level `dispatch_chat` / `dispatch_stream` in `lib/llm_dispatch/api.py`
wrap this with model routing, fallback chains, and per-model clamping.

All consumers import from the package facade:

```python
from lib.llm import build_body, stream_chat, chat, add_cache_breakpoints
from lib.llm import RateLimitError, AbortedError, is_claude
```

**Logging checkpoints:**
- Log model, token count estimate before each API call.
- Log each retry attempt with reason.
- Log final outcome (success/failure, usage stats).

### 4.4 Tool execution

Tools are defined in the `lib/tools/` package — one submodule per tool family
(`project.py`, `search.py`, `browser.py`, `meta.py`, `human_guidance.py`,
`image_gen.py`, `code_exec.py`, `conversation.py`) plus `registry.py` (tool
schema registry). Execution
is split across `lib/tasks_pkg/`:

- `tool_dispatch.py` — name → handler routing
- `executor.py` — main execution entry + `_resolve_content_ref()`
- `streaming_tool_executor.py` — streaming variant
- `executor_image.py` — image-generation path
- `handlers/` sub-package — per-family handlers (`misc.py`, `project.py`,
  `search.py`, `browser.py`, `mcp.py`, `memory.py`, `code_exec.py`, `_adapter.py`)

**Logging checkpoint**: every tool call MUST log:
```python
logger.info('[Tool:%s] called with args=%s', tool_name, truncated_args)
logger.info('[Tool:%s] returned %d chars in %.1fs', tool_name, len(result), elapsed)
# On failure:
logger.error('[Tool:%s] failed: %s', tool_name, error, exc_info=True)
```

### 4.5 Token-saving tool — `content_ref`

**`content_ref` on `write_file`** — Optional parameter (`{tool_round, start?, end?}`) that writes a previous tool result's content to a file, instead of the model re-generating it. Resolved by `_resolve_content_ref()` in executor.py.

### 4.6 Shared infrastructure modules — use these, not raw primitives

> **New code MUST go through the shared modules below for the patterns
> they own. They each replaced 50–500+ ad-hoc copies and exist precisely
> so we never re-grow that duplication.**

| Pattern | Use this | Don't write |
|---|---|---|
| Returning JSON from a route | `lib.api_response.api_ok / api_error / api_not_found / api_bad_request / @safe_route` | `return jsonify({...}), N` for the 4 helper-covered shapes (still fine for genuinely custom multi-key responses — see §4.6.1) |
| Parsing a JSON request body | `lib.request_parser.parse_body() + require_str / require_int / optional_list / …` (raises `BadRequest` → auto 400) | `data = request.get_json(silent=True) or {}` followed by manual `.get()` |
| Outbound HTTP (sync) | `lib.http_client.http_get / http_post / http_stream` — auto-applies proxy + 30s timeout + Tofu UA | `requests.get(url, ..., proxies=proxies_for(url), timeout=...)` |
| Outbound HTTP (async, in `async def` routes) | `await async_http_get / async_http_post / async with async_http_stream(...)` | bare `httpx.AsyncClient` per call |
| Reading/writing JSON files | `lib.json_store.read_json / write_json_atomic / update_json_atomic` | hand-rolled `tempfile.mkstemp + os.replace`, `json.load/dump` |
| Background TTL caches | `lib.ttl_cache.TTLCache(ttl=..., max_size=..., name=...)` | hand-rolled `_cache = {}` + `_cache_lock = threading.Lock()` |
| Background tasks (chat / paper / translate / trading-sim style) | `lib.task_runtime.TaskRuntime` (one instance per task kind) — see §13 | local `_tasks = {}` registry with custom append/poll/abort logic |
| Multi-round tool-calling loop + abort/stop | `lib.agent_loop.run_agent_loop(...)` + `AbortSignal.from_event / from_task_flag / from_callback / never` | hand-rolled `for rnd in range(max+1)` shell with per-engine before/post-stream/between-tools abort checks |
| Server-side push (real-time event channel) | `lib.push.push_event(channel, task_id, event)` (auto-fired from `TaskRuntime.append_event`) — see §4.7 | per-feature WebSocket endpoints |

#### 4.6.1 Carve-outs — modules that intentionally bypass the shared layer

These are **not** sloppiness; they have specific reasons to keep their
own implementations. Don't migrate them without a strong cause.

- `lib/llm/stream.py`, `lib/llm/astream.py` — custom SSE streaming with
  retry / 429 cycling / cache-breakpoint injection. Wrap via
  `lib.llm` package facade, not `http_client`.
- `lib/token_counter/usage_cache.py` — structured `_UsageEntry` dataclass +
  signature-based staleness + model-family validation. Not a plain
  key-value cache; deliberately separate from `TTLCache`.
- 44 multi-key/multi-line `return jsonify({...})` sites in routes/ —
  not boilerplate, they produce distinct response shapes; left as-is.

#### 4.6.2 Logging integration

All shared modules log to the standard `lib.log.get_logger(__name__)`
sink. They do NOT swallow errors silently:

- `json_store` — read failures log `WARNING` and return the default;
  write failures raise.
- `request_parser.parse_body` — unexpected `get_json()` errors log at
  `DEBUG` (outside-context misuse is the expected case) and return `{}`.
- `api_response.api_internal_error` — auto-logs the exception at
  `ERROR` with traceback so 500s never go undocumented.
- `http_client` — does NOT auto-`raise_for_status()`; caller logs and
  decides what to do with non-2xx (matches `requests`'s contract).

### 4.7 Unified push channel — `/api/push` WebSocket

A single global WebSocket multiplexes all real-time events for a client:

```
Frontend                         Backend
pushSubscribe('chat', taskId)    push_event('chat', task_id, event)
pushSubscribe('paper', tid)      push_event('paper', task_id, event)
pushSubscribe('translate', tid)  push_event('translate', task_id, event)
```

- Backend hub: `lib/push.py::PushHub` (singleton `hub`) — thread-safe;
  call `push_event(channel, task_id, dict)` from any thread.
- WebSocket endpoint: `routes/push.py::/api/push` — handles
  subscribe/unsubscribe/abort frames; per-client async queue.
- `TaskRuntime.append_event()` automatically pushes to the configured
  `push_channel` — most consumers don't call `push_event()` directly.
- Frontend client: `static/js/push.js` (`pushSubscribe`,
  `pushUnsubscribe`, `pushSend`) with auto-reconnect + exponential
  backoff. MUST be in `_BUNDLE_FILES` (already is, after `core.js`).

---

## 5. Error Handling Patterns

### 5.1 API / Network calls

```python
try:
    resp = requests.get(url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
except requests.Timeout:
    logger.warning('[Fetch] Timeout after %ds: %s', FETCH_TIMEOUT, url)
    return ''
except requests.RequestException as e:
    logger.warning('[Fetch] Request failed for %s: %s', url, e)
    return ''
```

### 5.2 JSON parsing

```python
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError) as e:
    logger.warning('Invalid JSON (len=%d): %s — preview: %.200s', len(raw), e, raw)
    data = {}
```

### 5.3 Database operations

```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s — sql=%.200s params=%s', e, sql, params, exc_info=True)
    db.rollback()
    raise
```

### 5.4 Background threads

Background threads MUST wrap their entire run loop in try/except to prevent silent death:

```python
def _worker_loop():
    logger.info('[Worker] Started')
    while running:
        try:
            _do_one_cycle()
        except Exception as e:
            logger.error('[Worker] Cycle failed: %s', e, exc_info=True)
            time.sleep(60)  # back off on error
    logger.info('[Worker] Stopped')
```

---

## 6. File Modification Checklist

Before submitting any code change, verify:

- [ ] **Logger present**: File has `from lib.log import get_logger; logger = get_logger(__name__)`.
- [ ] **No silent catches**: Every `except` block logs something (debug at minimum).
- [ ] **Context in logs**: Log messages include relevant IDs (conv_id, task_id, url, model, etc.).
- [ ] **Tracebacks on errors**: `exc_info=True` on `logger.error()` for unexpected exceptions.
- [ ] **`log_context` for slow ops**: Operations that may take > 1s use `with log_context(...)`.
- [ ] **No f-strings in log calls**: Use `logger.info('x=%s', x)` not `logger.info(f'x={x}')`.
- [ ] **Secrets not logged**: API keys, tokens, passwords never appear in log output.
- [ ] **Large data truncated**: Use `%.500s` or `[:500]` to cap logged payloads.
- [ ] **No hardcoded env values**: No hardcoded paths, hostnames, cluster names, or datacenter IDs — use env vars or config files (see §3.5).
- [ ] **Use the shared infrastructure** (see §4.6): new routes use `api_ok` / `api_error` / `parse_body`; new outbound HTTP uses `http_client.http_get / http_post`; new JSON-on-disk uses `json_store`; new background tasks use `TaskRuntime` (§13); new TTL caches use `TTLCache`. Don't re-grow the patterns we just deleted.
- [ ] **Export sync**: If this change adds/modifies secrets, endpoints, credentials, data dirs, or internal identifiers → update `export.py` (see §10.3).

---

## 7. Testing

- Test workflow is **Makefile-driven** (pytest markers under the hood):
  - `make lint` — ruff check (blocks CI); `make lint-fix` to auto-fix; `make typecheck` runs `tsc --checkJs` over the vanilla-JS frontend (no build step).
  - `make test-unit` (`-m unit`) / `make test-api` (`-m api`) / `make test-visual` (Playwright) / `make test-all`.
  - `make ci` = lint + unit + api + healthcheck; `make smoke` for import/syntax validation.
- pytest suites live in `tests/`; ad-hoc/benchmark scripts in `debug/`.
- Frontend contracts are gated by ratchet tests: `tests/test_frontend_api_isolation.py`
  (raw `/api/*` fetch count must monotonically decrease — §3.2.0) and
  `tests/test_frontend_typecheck.py` (tsc error budget).
- The legacy `_fix_silent_catches.py` helper has been retired. To audit for silent
  exception handlers, use `grep_search` on `except` blocks in `lib/` and `routes/`
  and confirm each has a matching `logger.*` call — see §2.2.

---

## 8. Key Files Quick Reference

| Need to… | Look at… |
|---|---|
| Return JSON from a route | `lib/api_response.py` — `api_ok` / `api_error` / `api_not_found` / `@safe_route` (§4.6) |
| Parse a JSON request body with type checks | `lib/request_parser.py` — `parse_body` + `require_str` / `require_int` / `optional_list` … (§4.6) |
| Make an outbound HTTP call (sync or async) | `lib/http_client.py` — `http_get` / `http_post` / `async_http_get` / `async_http_post` / `http_stream` / `async_http_stream` (§4.6) |
| Read/write a JSON file safely | `lib/json_store.py` — `read_json` / `write_json_atomic` / `update_json_atomic` (§4.6) |
| Add an in-memory TTL cache | `lib/ttl_cache.py` — `TTLCache(ttl=, max_size=, name=)` (§4.6) |
| Add a background task (poll/abort/events) | `lib/task_runtime.py` — `TaskRuntime` + `routes/_task_routes.py::register_task_routes` (§13) |
| Run a multi-round tool-calling loop with Stop support | `lib/agent_loop.py` — `run_agent_loop(...)` + `AbortSignal` (wraps Event / task-flag / callback) (§4.6) |
| Push a real-time event to the frontend | `lib/push.py::push_event(channel, task_id, event)` — auto-fired by `TaskRuntime.append_event` (§4.7) |
| Change LLM behavior | `lib/llm/` (package), `lib/llm_dispatch/` (package) |
| Adjust per-model token caps | `lib/model_info/` (`_clamp_max_tokens`) |
| Add a new tool | Define in `lib/tools/` (pick the right submodule or add one) → register routing in `lib/tasks_pkg/tool_dispatch/` → add handler in `lib/tasks_pkg/handlers/` |
| Add a new API endpoint | Land it on `/api/v1/*` first: `routes/api_v1/` (Blueprint) → `routes/api_v1/__init__.py` (`ALL_V1_BLUEPRINTS`); use `api_ok` / `api_error` (§4.6) + `@require_scope` + `@api_meta` (§15) |
| Mount an optional feature bundle (e.g. trading) | `routes/plugin_registry.py` — `tofu.blueprints` / `tofu.startup` / `tofu.task_runtimes` entry-point groups |
| Fix streaming issues | `lib/llm/stream.py` (SSE) → `routes/chat.py` (delivery) |
| Debug task flow | `lib/tasks_pkg/orchestrator/`, `lib/tasks_pkg/manager/` |
| Debug endpoint (Planner/Worker/Critic) | `lib/tasks_pkg/endpoint/`, `endpoint_prompts/`, `endpoint_review.py` |
| Change project file tools | `lib/project_mod/tools.py`, `lib/project_mod/read_tools.py`, `lib/project_mod/write_tools.py` |
| Read local files (images/PDF/Office) | `lib/file_reader/` (core) → `lib/project_mod/read_tools.py` (`_read_absolute_file`) |
| Manage memory / stored notes (legacy "skills") | `lib/memory/storage.py`, `lib/memory/tools.py`, `routes/memory.py`, on-disk `<project>/.tofu/skills/` (project scope); global memories moved to the server store `<data>/memories/global/` (2026-06) |
| Install Anthropic / OpenClaw / AgentSkills `.zip` packages (drag-and-drop) | `lib/memory/installer.py` → `POST /api/v1/memory/install` (multipart). Packages live as `<.tofu/skills>/<name>/SKILL.md` + references/ + scripts/. Treated identically to flat `.md` memories by BM25 / search_memories — frontend marks them with a `SKILL` badge. `install.sh` is **never auto-executed**; surfaced as `install_hints`. |
| Skills store / curated catalog / file browser | `lib/memory/catalog.py` (curated `SkillCatalogEntry` list), `routes/api_v1/memory.py` (`/api/v1/memory/catalog`, `/api/v1/memory/catalog/install`, `/api/v1/memory/<id>/files`), `static/js/skills.js`, Settings → **Skills** tab. App-Store layout mirrors the MCP tab: search + scope tabs (Catalog / Installed) + category pills + grid + drag-drop zone. Catalog one-click installs download a `.zip` over HTTPS (capped at 50 MB) and feed it to `install_skill_package`. |
| Modify trading features | External `tofu-trading` package (extracted 2026-06) — mounts via `tofu.blueprints` entry point; not in this repo |
| Reusable agent base (run loop, dispatch, TaskRuntime, push, profiles) | `lib/agent_core/` (facade `__init__.py`; `task_runtime.py`, `push.py`, `events.py`, `profiles.py`) |
| Per-user billing / wallet / cost ledger | `lib/billing/` (wallet, ledger, pricing, users, payments/), `routes/api_v1/billing.py` |
| Declarative multi-agent orchestration (Studio) | `lib/orchestration/` (schema + validator), `lib/orchestration_engine.py`, `routes/api_v1/orchestrations.py`, `static/js/orchestration.js` |
| Orchestration typed node I/O (Dify-style dataflow) | OPTIONAL `params.io = {inputs:[{name,type,from}], outputs:[{name,type}]}` on role/subflow nodes. Types: `VALID_IO_TYPES` (text/json/artifact/file/number/bool/any). `from` ref = `'<id>'`/`'<id>.<out>'`/`'start'`. Helpers `node_output_names` + `parse_io_ref`; `_validate_node_io` in `lib/orchestration/`. Engine (`lib/orchestration_engine.py`) `_compose_typed_inputs` (a node with declared inputs reads ONLY wired upstream outputs, not the scratchpad) + `_publish_outputs`/`_build_change_manifest` (an `artifact`-typed output is filled with the worker's state-changing tool manifest — how a tool-heavy worker exposes its many ops as ONE typed output vs a pure-NL node's single `text` output). FULLY back-compat: no `io` block ⇒ legacy accumulating scratchpad. Studio: edges are click-to-SELECT (not click-to-delete) + Delete/Backspace key + edge inspector (reverse/bind); I/O editor (`_orchRenderIoEditor`) authors ports. Tests: `tests/test_orchestration_io.py` + Scenario 5 in `tests/orch_nested_roundtrip_harness.js`. |
| Paper / Reading Mode (reports, Q&A, translate) | `lib/paper/` (report_engine, translate_engine, prompts), `routes/paper.py`, `static/js/paper-reader.js` |
| Daily report subsystem | `routes/daily_report.py`, `lib/scheduler/` |
| Scheduled / proactive agents, cron, timers | `lib/scheduler/` (manager, executor, cron, timer, proactive), `routes/scheduler.py` |
| MCP (Model Context Protocol) | `lib/mcp/` (client, registry, config), `routes/mcp.py`, `lib/tasks_pkg/handlers/mcp.py` |
| Use a Claude Pro/Max or ChatGPT subscription as a provider | `lib/oauth/` (PKCE login, token store) → `lib/oauth/outbound.py` bridges a logged-in subscription into a managed provider slot (the slot's `oauth` marker resolves per-request to a live token + client-identity headers). Used via the normal dispatch path — NOT a CLI subprocess. (The former `lib/agent_backends/` CLI-subprocess backend was removed 2026-06-21.) |
| OAuth flows (Claude / Codex) | `lib/oauth/`, `routes/oauth.py` |
| PDF parsing (text, images, math, VLM) | `lib/pdf_parser/` |
| Web fetch / browser automation | `tofu_search.fetch` (external pkg), `lib/browser/`, `routes/browser.py` |
| Web search orchestration | `tofu_search.search` (external pkg — orchestrator, engines, rerank, dedup); chatui seams via `lib/search_bridge.py` |
| Feishu / Lark bot | `lib/feishu/` |
| Swarm (multi-agent) | `lib/swarm/` (master, agent, scheduler, planner, review, registry, rate_limiter, artifact_store, synthesis, integration, events, tools, types), `routes/api_v1/swarm.py` |
| Nightly optimizer (self-tuning) | `lib/optimizer/` (orchestrator, analyzer, proposer, applier, storage, actions/), `routes/api_v1/optimizer.py`, `static/js/optimizer.js` |
| Folder / conversation organization | `routes/api_v1/folders.py`, `routes/conversations.py` |
| Check recent errors | `tail -f logs/error.log` or `grep_search` the `logs/` directory |
| Export / sanitize project | `export.py` (three modes: `--mode personal` / `--mode internal` / `--mode opensource`) — see §10 |
| Cross-platform compat | `lib/compat/_platform.py` (core, re-exported from `lib/compat/__init__.py`) → `debug/test_cross_platform.py` (smoke test) |
| Cross-DC FUSE latency | `lib/cross_dc/` (detection) → `data/config/cross_dc.json` (config) |

### 8.1 Conversation IDs & tracing a conversation

> 🔴 **READ THIS SECTION FIRST whenever the user gives you a bare token that
> could be a conversation ID** (14 lowercase-alphanumeric chars, e.g.
> `mqgfkmxyrlygaa`, often introduced as "this is the conversation ID" or
> "对话 ID"). Do NOT explore from scratch, do NOT `grep_search` the repo for
> the ID, and do NOT `find`/`ls` the filesystem for it — those find nothing
> and waste the turn (conversations are DB rows, not files). The complete,
> authoritative recipe — ID→timestamp decode, the `conversations` schema, and
> the exact `psql` query to fetch content — is right here in §8.1. Use it.

**ID format.** A conversation ID is **14 chars matching `^[a-z0-9]{14}$`**
(lowercase base36), e.g. `mqhv05fyf3y1u3`. The **first 8 chars decode to the
creation time in epoch-ms**: `int(cid[:8], 36)` ≈ the `created_at` column
(also stored as bigint epoch-ms). The two are close but **not byte-exact** —
the ID is minted client-side slightly before the row is persisted, so use it
for an approximate "when was this created" sanity check, not as a precise
timestamp. IDs that don't match the 14-char pattern (`agent-*`, `bench-*`, …)
are swarm/benchmark runs, not real user conversations.

```python
import datetime
cid = "mqhv05fyf3y1u3"
print(datetime.datetime.fromtimestamp(int(cid[:8], 36) / 1000))  # ≈ created_at
```

**Storage.** Single-table — there is **no `messages` table**. Everything lives
in `conversations` (PG-primary; SQLite only when PG is down):

```
conversations: id(text), user_id(int), title(text), messages(jsonb),
               created_at(bigint ms), updated_at(bigint ms),
               settings(jsonb), msg_count(int), search_text, search_tsv
```

`messages` is a JSONB array; each element has `role` / `content` / `thinking` /
`timestamp` / `model` / `usage` / `toolRounds` / `apiRounds` … (PK is
`(id, user_id)`).

**Fetching a conversation by ID — query PostgreSQL DIRECTLY.**

> ⚠️ Conversations are **rows in the `conversations` table, NOT files on
> disk**. To read one by its ID, go straight to the DB. **Do NOT** `find` /
> `ls` the filesystem for the ID, and do NOT `grep_search` the repo for it —
> those will find nothing and waste the turn. There is no per-conversation
> file anywhere.

The live PG instance is on **port 15439, db `tofu`, user `hadoop-aipnlp`**
(NOT the code default 15432; the old `chatui` db name no longer exists).
Always pass an explicit `user=` and set `PGGSSENCMODE=disable` to dodge Kerberos:

```bash
export PGGSSENCMODE=disable
DSN="host=127.0.0.1 port=15439 dbname=tofu user=hadoop-aipnlp"
# header
psql "$DSN" -tAc "SELECT id,title,msg_count,to_timestamp(created_at/1000)
                  FROM conversations WHERE id='<conv_id>';"
# full message array (role + content), in order
psql "$DSN" -tAc "SELECT m->>'role', left(m->>'content',200)
                  FROM conversations, jsonb_array_elements(messages) m
                  WHERE id='<conv_id>';"
```

> Verify the port at runtime: `grep 'listening on IPv4' logs/postgresql.log | tail -1`.
> Don't trust `data/tofu.db` (SQLite) when PG is running — it's an empty shadow.

**Secondary — logs, for runtime *behaviour* only** (not for fetching content):
once you have the conv ID, `grep_search` it across `logs/` to see the LLM
rounds, tool calls, and errors (per §2.6 every code path logs `conv_id`).
Use this to answer "what happened during the run", never to locate the
conversation itself — that's always the DB query above.

---

## 9. Environment

- Python 3.10+
- **ASGI stack**: Quart (async Flask, same Pallets API) + Hypercorn (HTTP/2-capable
  ASGI server). The `flask` import name is shimmed to `quart` at startup
  (`server.py::_install_flask_shim`) so all `from flask import …` works unchanged.
  Key dependencies in `requirements.txt`: `quart`, `hypercorn`, `httpx`,
  `cryptography` (for auto-TLS — see below).
- **Auto-TLS for HTTP/2**: on first run, `cryptography` programmatically
  generates a self-signed cert at `data/certs/tofu.{pem,key}` (no openssl CLI
  required). Browsers see HTTPS + HTTP/2 directly; warning is one-time-click.
  Auto-DISABLED when `VSCODE_PROXY_URI` / `CODESPACES` / `GITPOD_WORKSPACE_URL`
  is set (the proxy already provides HTTPS + HTTP/2). Override with
  `TOFU_TLS=1` (force on) or `TOFU_TLS=0` / `--no-tls` (force off).
- **Critical Quart timeouts** (configured at boot in `server.py`):
  `RESPONSE_TIMEOUT = None` and `BODY_TIMEOUT = None` — Quart's defaults
  of 60s would silently kill long SSE streams (chat tasks routinely run 5+
  minutes). `keep_alive_timeout = 600` on the Hypercorn config.
- **Dual-backend database** (`lib/database/`): tries PostgreSQL 18+ first (better concurrency for 100+ users, JSONB, tsvector); auto-falls-back to SQLite (`data/tofu.db`; legacy `data/chatui.db` is still picked up if present) if PG is unavailable. Force SQLite with `TOFU_DB_BACKEND=sqlite` (legacy `CHATUI_DB_BACKEND=sqlite` still honored). PG is optional — runs as a local userspace process (no `sudo`), auto-bootstrapped via conda when missing.
- Logging: multi-file architecture configured in `server.py`, utilities in `lib/log.py`:
  - `logs/app.log` — Business logic (`TimedRotatingFileHandler`, daily, 30 days)
  - `logs/access.log` — HTTP requests (`TimedRotatingFileHandler`, daily, 14 days)
  - `logs/error.log` — All warnings/errors (`RotatingFileHandler`, 5 MB × 10)
  - `logs/vendor.log` — Third-party libs (`RotatingFileHandler`, 5 MB × 3)
  - `logs/audit.log` — Structured JSON audit trail
- **Per-project config isolation**: All settings (providers, models, features) are stored in
  `data/config/` within the project directory — NOT in `~/.chatui/` (legacy global). This means
  multiple copies on the same machine have fully independent configs, databases, and API keys.
  Config files: `data/config/server_config.json`, `data/config/features.json`, `data/config/daily_reports/`.
  Project-scoped memory / skill notes are stored under `<project>/.tofu/skills/`;
  global memories moved (2026-06) to the server-side store `<data>/memories/global/`
  so they are shared across projects and reachable in a project-less chat (the legacy
  `<project>/.tofu/skills/global/` dir is still read and migrated once, idempotently).
  Auto-migration: on first run, if `data/config/server_config.json` doesn't exist but `~/.chatui/server_config.json` does, it's copied once.
- Key env vars for initial setup:
  - `LLM_API_KEYS` — comma-separated API keys (or configure via Settings UI)
  - `LLM_BASE_URL` — LLM endpoint (default: `https://api.openai.com/v1`)
  - `LLM_MODEL` — default model (default: `gpt-4o`)
  - `TOFU_DB_PATH` (legacy `CHATUI_DB_PATH`) — SQLite database file path (default: `data/tofu.db`; legacy `data/chatui.db` auto-picked up)
  - `TOFU_DB_BACKEND` (legacy `CHATUI_DB_BACKEND`) — force backend choice: `sqlite` or `postgres` (default: auto-detect, prefer PG)
  - `TOFU_PG_HOST` / `TOFU_PG_PORT` / `TOFU_PG_DBNAME` / `TOFU_PG_USER` / `TOFU_PG_PASSWORD` (legacy `CHATUI_PG_*`) — PostgreSQL DSN overrides (defaults: `127.0.0.1:15432/chatui`, no auth — matches the auto-bootstrapped userspace instance; live deployments still use DB name `chatui` for back-compat)
  - `TOFU_DB_MAX_CONNS` / `TOFU_DB_ACQUIRE_TIMEOUT` (legacy `CHATUI_*`) — PG connection pool tuning
  - `PROXY_BYPASS_DOMAINS` — comma-separated domain suffixes for proxy bypass (e.g. `.internal.example.com`)
  - `CROSS_DC_CLUSTER_MOUNTS` — cluster mount map for FUSE latency detection (format: `cluster1:/path/a,cluster2:/path/b`)
  - `CROSS_DC_LOCAL_IDC` — local datacenter identifier for cross-DC classification
  - `TOFU_ENDPOINT_REPLAN` (legacy `CHATUI_ENDPOINT_REPLAN`) — endpoint-mode three-way Critic kill switch (`1` default / `0` to disable). When `0`, the Critic's `[VERDICT: CONTINUE_PLANNER]` is silently downgraded to `[VERDICT: CONTINUE_WORKER]` and the STOP-with-❌ override guard is disabled. Use for hot rollback of the replan redesign without a code change.
  - `TRADING_ENABLED` — gate honored by the external `tofu-trading` plugin (extracted 2026-06); its `register()` returns no Blueprints when unset. No effect on a vanilla core install where the plugin isn't installed.
- Proxy bypass unified: Settings UI bypass domains auto-sync to both `proxies_for()` per-request bypass and `no_proxy` env var (see `lib/proxy.py`)
- Provider templates in Settings UI for one-click provider setup (OpenAI, Anthropic, Meituan, etc.)
- Trading is now an external plugin (`tofu-trading`), not bundled with core; install it + set `TRADING_ENABLED=1` to mount its Blueprints (see `routes/plugin_registry.py`)
- **Cross-platform support** (Linux, macOS, Windows):
  - All platform-specific code is in `lib/compat/` (`_platform.py`, re-exported from `lib/compat/__init__.py`) — use its helpers instead of direct `fcntl`, `select`, `/proc` access.
  - FS keepalive (`lib/fs_keepalive.py`) is Linux-only; graceful no-op on other platforms.
  - Interactive stdin detection in `run_command` is Linux-only (requires `/proc`); degrades to non-interactive on macOS/Windows.
  - `DANGEROUS_PATTERNS` in `lib/project_mod/config.py` include both Unix and Windows equivalents.
  - Smoke test: `python debug/test_cross_platform.py` validates compat layer on any platform.

---

## 10. ⚠️ MANDATORY: Export Sanitization & Sensitive Data Sync

> **Whenever you add, move, or modify ANY sensitive data in the codebase, you MUST
> also update `export.py` to ensure it is properly sanitized on export.**
> Failure to keep `export.py` in sync means secrets will leak to colleagues or the public.

### 10.1 What `export.py` does

`export.py` copies the project to a clean destination with three sanitization levels:

```bash
# Personal: self-use copy, no sanitization, just skip bulky junk
python3 export.py --mode personal --dest ~/tofu-backup

# Company-internal: keeps API keys & endpoints, strips personal data
python3 export.py --mode internal --dest ~/tofu-team

# Open-source: strips ALL secrets, keys, internal domains, paths
python3 export.py --mode opensource --dest ~/tofu-public

# Preview without writing
python3 export.py --mode opensource --dry-run
```

### 10.2 Sanitization matrix

| Data category | `personal` | `internal` | `opensource` | Where defined in export.py |
|---|---|---|---|---|
| Source code & configs | ✓ kept | ✓ kept | ✓ sanitized | `_sanitize_source_opensource()` |
| Skills (`.chatui/`) | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_DIRS` |
| Uploads (`uploads/`) | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_DIRS` |
| Session caches (`.project_sessions/`) | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_DIRS` |
| Data configs (`data/config/`) | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_DIRS` |
| Databases (`*.db`) | ✗ removed | ✗ removed | ✗ removed | `PERSONAL_EXCLUDE_GLOBS_HEAVY`, `ALWAYS_EXCLUDE_DIRS` |
| Logs (`logs/`, `*.log`) | ✗ removed | ✗ removed | ✗ removed | `PERSONAL_EXCLUDE_DIRS`, `ALWAYS_EXCLUDE_DIRS` |
| `__pycache__`, `.git` | ✗ removed | ✗ removed | ✗ removed | All levels |
| Scratch files (`a.md`, `o.md`, etc.) | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_FILES` |
| API keys (hardcoded in `lib/__init__.py`) | ✓ kept | ✓ kept | ✗ → placeholder | `_SECRETS` dict |
| Internal endpoints (`aigc.sankuai.com`) | ✓ kept | ✓ kept | ✗ → `api.openai.com` | `_ENDPOINTS` dict |
| Feishu credentials | ✓ kept | ✓ kept | ✗ → empty | `_SECRETS` dict |
| Internal domains (`.sankuai.com`) | ✓ kept | ✓ kept | ✗ → `.internal.example.com` | `_INTERNAL_DOMAIN_LITERALS` |
| Provider ID (`'sankuai'`) | ✓ kept | ✓ kept | ✗ → `'default'` | `_sanitize_source_opensource()` §10 |
| Absolute paths (`/mnt/dolphinfs/...`) | ✓ kept | ✓ kept | ✗ → `/path/to/your/project` | `_sanitize_source_opensource()` §8 |
| Trading module (`TRADING_ENABLED`) | ✓ kept | ✗ OFF via `.env` | ✗ OFF via `.env` | `_create_skeleton()` writes `.env` |
| Thinking Depth default | ✓ `medium` | ✗ → `off` | ✗ → `off` | `_sanitize_defaults_for_export()` |
| Security audit reports | ✓ kept | ✓ kept | ✗ removed | `OPENSOURCE_EXTRA_EXCLUDE_FILES` |
| `export.py` itself | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_FILES` |
| `CLAUDE.md` | ✓ kept | ✗ removed | ✗ removed | `ALWAYS_EXCLUDE_FILES` |

### 10.3 ⚠️ When to update `export.py` — MANDATORY triggers

You **MUST** update `export.py` when any of the following happens:

| Change | What to update in `export.py` |
|---|---|
| **New API key or credential hardcoded** | Add to `_SECRETS` dict |
| **New internal endpoint/URL added** | Add to `_ENDPOINTS` dict |
| **New internal domain referenced** | Add to `_INTERNAL_DOMAIN_LITERALS` list |
| **New personal/runtime data directory** | Add to `ALWAYS_EXCLUDE_DIRS` |
| **New scratch/temp file pattern** | Add to `ALWAYS_EXCLUDE_FILES` or `ALWAYS_EXCLUDE_GLOBS` |
| **New file with hardcoded secrets** | Add file-specific sanitization in `_sanitize_source_opensource()` |
| **New provider/vendor with internal identity** | Add provider ID replacement in `_sanitize_source_opensource()` |
| **New security-sensitive report** | Add to `OPENSOURCE_EXTRA_EXCLUDE_FILES` |
| **New hardcoded absolute path pattern** | Add regex to path-cleaning block in `_sanitize_source_opensource()` |
| **New `.env` variable with secret default** | Ensure `.env` is excluded (already is) and `.env.example` uses placeholders |
| **New feature flag that should be OFF in exports** | Add to the `.env` written by `_create_skeleton()` in `export.py` |

### 10.4 How to comply

When making a code change that introduces sensitive data:

1. **Make your primary code change** as normal.
2. **Immediately open `export.py`** and add the corresponding sanitization.
3. **Verify** with `python3 export.py --mode opensource --dry-run` — check the file shows as "would sanitize".
4. **Run a full export** periodically and check the post-export secret scan passes (0 leaks).

```python
# Example: you added a new API key in lib/config.py
# Step 1: your code change
NEW_SERVICE_KEY = 'abc123secret'

# Step 2: update export.py
# In _SECRETS dict:
_SECRETS = {
    ...existing...,
    'abc123secret': 'YOUR_NEW_SERVICE_KEY',  # ← add this
}
```

### 10.5 Post-export verification

The `opensource` mode automatically runs `_verify_opensource()` after export, which scans
all text files for known secret patterns. If any leak is detected, it prints file:line
details and a warning. **Do NOT publish until 0 leaks are confirmed.**

To add new patterns to the verifier, update the `leak_patterns` list in `_verify_opensource()`.

---

### 10.6 Database auto-creation on exported copies

Exported projects ship with no database file or PG data directory. On first
`python3 server.py` the dual-backend layer (`lib/database/`) will:

1. Try to bootstrap/connect to a local PostgreSQL 18+ (userspace, no `sudo`).
   If PG binaries are missing, `bootstrap.py` will offer to `conda install -c conda-forge postgresql>=18`.
2. If PG is unavailable or the user sets `TOFU_DB_BACKEND=sqlite`, fall back
   to SQLite — `data/tofu.db` is auto-created (built into Python, zero install).

Either way, colleagues can `cd tofu-team && python3 server.py` with zero manual DB setup.

---

## 11. ⚠️ Process Safety

Unless explicitly requested, do not kill server.py on your own.

---

## 12. ⚠️ MANDATORY: No Artificial `max_tokens` Cap on Long-Form Generation

> **For long-form generation (paper reports, deep analyses, full translations,
> multi-section writeups), NEVER hardcode a small `max_tokens` value.**
> The output must be allowed to run to the model's native ceiling so the
> content is not silently truncated mid-section.

### 12.1 The rule

- **Pass a very large ceiling** (use `128000` as the convention) to
  `dispatch_stream(...)` / `dispatch_chat(...)` / `_stream_llm_sse(...)` for
  any long-form task.
- `_clamp_max_tokens()` in `lib/model_info/` automatically reduces this to
  each model's actual API limit (GPT=32k, Claude=128k, Qwen per-model
  16–64k, Doubao=16k, etc.). This is the correct way to say
  **"use as much as the model allows, no artificial cap"**.
- **Never hardcode `max_tokens=4096`** (or any small value) for user-facing
  long-form output. 4096 tokens ≈ 3k words — it truncates complex reports
  (e.g. Reading Mode's 9-section paper report) mid-Technical-Reference.

### 12.2 Why this exists

- Users reported that Reading Mode reports were silently cut off before the
  last sections (Research Landscape, Technical Reference, Reproducibility
  Checklist) because `_run_report_task` relied on `dispatch_stream`'s
  default `max_tokens=4096`.
- The fix is to pass a large ceiling and let `_clamp_max_tokens` do its job
  per model, rather than picking any magic number.

### 12.3 Where this applies

| Code path | Required behavior |
|---|---|
| `routes/paper.py` → `_run_report_task` (report generation) | `max_tokens=128000` |
| `routes/paper.py` → `_stream_llm_sse` (Q&A, translate) | `max_tokens=128000` default |
| Any future "generate a complete/full/comprehensive X" tool | `max_tokens=128000` |
| Short, bounded outputs (e.g. title summarization, 1-line labels) | Small cap is fine (explain why in a comment) |

### 12.4 How to comply

- When adding a new long-form generation path, grep for `max_tokens=` in
  the existing file and set it to `128000` (or document why a smaller
  value is correct for that specific path).
- **Note on approval**: Bumping `max_tokens` caps for long-form generation
  to follow this rule is NOT a hyperparameter tuning change — it is a
  correctness fix to prevent truncation, and does not require separate
  approval. Reducing it back to a small cap, however, does require
  approval.

---

## 13. Background tasks — `TaskRuntime` and `spawn_task`

`TaskRuntime` is the **single source of truth** for every server-side
background task pattern. Five legacy registries (chat, paper-report,
paper-translate, translate, trading-sim) were unified onto it; new code
must follow suit. It now lives in `lib/agent_core/task_runtime.py`
(relocated 2026-06); `lib/task_runtime.py` is a compatibility shim, so
`from lib.task_runtime import TaskRuntime` still works. New code may
import `from lib.agent_core import TaskRuntime`.

### 13.1 Standard task dict

```python
{
  'id': str, 'kind': str,
  'status': 'pending' | 'running' | 'done' | 'error' | 'aborted',
  'events': [...], 'events_lock': Lock, 'abort_event': threading.Event,
  'result': Any, 'error': dict | None,   # error envelope
  'created_at': float, 'finished_at': float | None,
  'meta': {...},                          # caller-supplied custom fields
}
```

Custom fields go in `meta` (or, for legacy compatibility, augment the
dict directly after `runtime.create()` — chat does this for `convId`,
`messages`, `content_lock`, etc.).

### 13.2 Adding a new task kind

```python
# lib/foo.py
from lib.agent_core import TaskRuntime  # (lib.task_runtime shim also works)
_foo_runtime = TaskRuntime(
    'foo', ttl=3600,
    push_channel='foo',           # auto-broadcasts append_event over WebSocket
    error_source='lib.foo',       # used in error envelopes
)

def start(payload):
    task = _foo_runtime.create(meta={'payload': payload})
    _foo_runtime.spawn(task['id'], _worker, task)
    return task['id']

def _worker(task):
    try:
        for chunk in slow_thing(task['meta']['payload']):
            if task['abort_event'].is_set():
                _foo_runtime.finish(task['id']); return
            _foo_runtime.append_event(task['id'], {'type': 'chunk', 'data': chunk})
        _foo_runtime.finish(task['id'], result={'output': 42})
    except Exception as e:
        _foo_runtime.finish(task['id'], error=e)
```

Then expose poll/abort routes by calling
`routes._task_routes.register_task_routes(bp, _foo_runtime, url_prefix='/api/foo')`
and writing only the start handler by hand.

### 13.3 Spawning rules

- **Use `runtime.spawn(task_id, fn, *args)`** — picks `asyncio.to_thread`
  inside the event loop or a daemon thread outside. NEVER use raw
  `threading.Thread(target=worker)` for new code.
- For chat tasks specifically: use `from lib.tasks_pkg import spawn_task`
  (the chat-orchestrator entry point) — it wires the orchestrator's
  `run_task` correctly. There are 6 historical spawn sites (chat × 3,
  message_queue, agent_backends, autopilot) all routed through
  `spawn_task`.

### 13.4 Aborting

- Workers MUST poll `task['abort_event'].is_set()` at safe checkpoints
  and call `runtime.finish(id)` (no `error=`) when they observe it —
  the runtime then promotes the status to `'aborted'`.
- If a worker calls `finish(error=)` while `abort_event` is set, the
  error wins (matches the legacy behaviour).
- `runtime.finish` is idempotent — second call returns `False`.

### 13.5 Why this matters

The five legacy registries each had subtly different semantics
(cursor-based polling, dedup-by-tuple, per-channel push). Migrating them
revealed real bugs (dropped events on cleanup, missing terminal events,
unbounded TTL growth). Don't re-grow that surface area — `TaskRuntime`
is battle-tested with 28 unit tests + 5 migration test suites.

---

## 14. Migration scripts in `tests/_migrate_*.py`

Three checked-in **one-shot** migration scripts exist in `tests/`:

| Script | Maps |
|---|---|
| `tests/_migrate_api_response.py` | raw `jsonify({...})` → `api_ok / api_error / api_not_found / …` |
| `tests/_migrate_request_parser.py` | `data = request.get_json(silent=True) or {}` → `data = parse_body()` |
| `tests/_migrate_http_client.py` | `requests.X(url, ..., proxies=_proxies_for(url), ...)` → `http_X(url, ...)` |

### 14.1 Common contract

- **Dry-run by default** (`python tests/_migrate_X.py`); pass `--apply`
  to write changes; pass `--file BASENAME` to restrict (api_response
  + request_parser only).
- **Conservative** — only rewrite single-line patterns the regex fully
  understands; skip multi-line dict literals, unknown statuses,
  matches inside triple-quoted docstrings.
- **Idempotent** — running again finds nothing to rewrite.
- **Auto-imports** — when a rewrite needs a helper not yet imported,
  the script extends `from lib.X import …` or inserts a fresh import
  line in the right group.

### 14.2 When to run

These were applied once during the initial migration and shouldn't
need re-running. But if you write new code in the legacy patterns by
mistake, running the dry-run is a quick way to see what would
collapse. **Always inspect the dry-run diff before `--apply`** — the
scripts skip docstrings and unknown statuses but cannot detect
semantic edge cases (e.g. a route that intentionally uses a custom
response shape).

### 14.3 Don't add new scripts in this style

If you're tempted to write a fourth `_migrate_X.py`, the right move is
usually:

1. Build the unified module + tests first.
2. Migrate a single small file by hand to validate the recipe.
3. Only if there are 50+ near-identical sites left, write the regex
   script (and treat it as code: needs a `--apply` gate, docstring
   awareness, and inspection of every dry-run diff before write).

---

## 15. ⚠️ MANDATORY: Headless API + Frontend/Backend Boundary

> **Every UI feature MUST be a `frontend → POST → backend does the
> work → returns result` round trip.** The browser is one client of
> Tofu; CLIs, SDKs, OpenAI/Anthropic-compat callers, webhooks, and
> evaluation harnesses are all peer clients. Anything that lives only
> in `static/js/*` is a leak — every API consumer gets a degraded
> version of the product.

The full guide for headless callers lives in
[`docs/HEADLESS_API.md`](docs/HEADLESS_API.md). The reclamation
backlog (per-file leak audit) is in
[`docs/JS_LEAK_AUDIT.md`](docs/JS_LEAK_AUDIT.md).

### 15.1 Three API surfaces (all wired to the same orchestrator)

| Path prefix     | Purpose                                   | Auth                       |
|-----------------|-------------------------------------------|----------------------------|
| `/api/v1/*`     | Tofu-native, fully-expressive             | Bearer or session cookie   |
| `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` | OpenAI compat | Bearer (also via `Authorization`) |
| `/v1/messages`  | Anthropic Messages compat                 | Bearer or `x-api-key`      |
| `/api/openapi.json`, `/api/docs`, `/api/redoc` | Spec + viewers | Public |

When you add a new feature, it MUST land on `/api/v1/*` first; the UI
becomes a client of that endpoint. Compat adapters
(`lib/compat/openai.py`, `lib/compat/anthropic.py`) translate the
ecosystem shapes onto the same backend.

### 15.2 Single auth model — Bearer everywhere (UI included)

Tofu has **one** authentication system: API keys minted by
`lib/api_keys/`. The UI, the SDK, the OpenAI / Anthropic compat
adapters, and `/metrics` all consult the same `g.auth_ctx` resolved
once per request by `routes/api_v1/auth.py:auth_before_request`. There
is no second auth scheme.

Token transports accepted (priority order):

1. `Authorization: Bearer <token>` — programmatic / SDK clients.
2. `x-api-key: <token>` — Anthropic SDK convention.
3. `tofu_session` cookie — set on first browser visit (`?token=…` →
   redirect + HttpOnly + SameSite=Lax). Subsequent same-origin XHR
   authenticates from the cookie alone.
4. `?token=<token>` query string — first-link convenience for browsers.
5. `X-Tunnel-Token` / `TUNNEL_TOKEN` env — **deprecated** back-compat
   shim. Emits a one-shot warning at boot. New deployments don't set it.

#### First-boot bootstrap

When `data/config/api_keys.json` is empty (and no `TUNNEL_TOKEN` is
set), `lib.api_keys.bootstrap_personal_key()` mints one
`tofu_admin_<32hex>` token at server start, prints it once to stderr,
and writes plaintext (chmod 0600) to `data/config/.first_run_token`.
The boot banner includes a one-shot URL
`http://host:port/?token=<token>` so opening the browser once installs
the cookie. Disable with `TOFU_AUTO_KEY=0` if you want manual control.

#### Default bind: 127.0.0.1

`server.py` defaults `--host` to `127.0.0.1`. Networked exposure is an
explicit choice: pass `--host 0.0.0.0` (or `BIND_HOST=0.0.0.0`).
Personal use stays effortless; accidental LAN/internet exposure stops
being the default.

#### Public allow-list

A short, frozen list in `routes/api_v1/auth.py:_PUBLIC_EXACT` /
`_PUBLIC_PREFIXES` defines the only paths reachable without auth:

- `/static/*`, `/favicon.*`, `/.well-known/*`, `/robots.txt`
- `/api/health`, `/api/openapi.json|yaml`, `/api/docs`, `/api/redoc`
- `/api/v1/capabilities`, `/api/v1/keys/whoami`

Everything else — including the legacy single-user `/api/*` routes —
requires a credential. The UI satisfies this via the cookie set on
first visit; SDK clients via the `Authorization` header.

#### Key model

- Stored in `data/config/api_keys.json` via `lib.json_store` (atomic).
- Issued via `POST /api/v1/keys` (admin scope) or the Settings UI.
- Token shape: `tofu_live_<32hex>` or `tofu_admin_<32hex>`. Only the
  SHA-256 hash is persisted; plaintext shown ONCE at creation.
- Closed scope vocabulary in `lib/api_keys.ALL_SCOPES`. Adding a new
  scope = adding to that frozenset (and using it in
  `@require_scope(...)` on the route).
- Per-key rate limits (RPM + TPD) enforced by `lib/rate_limit_api.py`.
  Standard headers (`X-RateLimit-*`, `Retry-After`) on every response.
- Cookie-authenticated UI requests have admin scope (matches the local
  user's privilege level) and bypass per-key rate limits.

> **⚠️ Security invariants** (verified by
> `tests/test_e2e_headless_api.py`):
>
> 1. Every `/api/*`, `/v1/*`, `/metrics` request that is not in the
>    public allow-list returns 401 without a credential. No env-var
>    state changes this — `TOFU_AUTO_KEY=0` or empty key store just
>    means there is no admin token, not that auth is bypassed.
> 2. The bootstrap path NEVER mints a key when `TUNNEL_TOKEN` is set
>    (operator chose the legacy shim explicitly).
> 3. `?token=<token>` is consumed by the middleware and stripped from
>    the URL before any route sees it; subsequent navigation cannot
>    leak the token via referer / address bar.

### 15.3 OpenAPI 3.1 self-description

Routes annotate themselves via `@api_meta(...)`:

```python
from lib.openapi import api_meta
from routes.api_v1.auth import require_scope

@bp.route('/api/v1/agents/translate', methods=['POST'])
@require_scope('agents:translate')
@api_meta(summary='Start a translation task',
          tags=['agents'], scope='agents:translate',
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'$ref': '#/components/schemas/...'}}}})
def translate_start():
    ...
```

`lib.openapi.build_spec(app)` walks `app.url_map` at request time and
emits the full spec at `/api/openapi.json`. Routes without
`@api_meta` still appear (with auto-derived tags) so the spec stays
drift-free.

### 15.4 Idempotency

Use `@idempotent_post()` on any POST that creates a side-effect
resource. Clients send `Idempotency-Key: <uuid>`; replays within
24h return the cached response with `Idempotency-Replay: true`.

### 15.5 What lives where — the boundary rule

| In `static/js/*`                                     | In `lib/`, `routes/`        |
|------------------------------------------------------|------------------------------|
| Markdown rendering                                   | Tool execution, retry policy |
| DOM updates, focus, scroll, drag-drop                | Conversation index / branches|
| Animation, theme switching, keyboard shortcuts       | Config resolution / defaults |
| IndexedDB caching (cache, NEVER source of truth)     | Token counting, message build|
| Fetch wrappers + render of returned structured data  | Derivation of structured data|
| i18n string lookup                                   | Tool round / turn summary    |

Adding code to a `.js` file?  Run the self-test in §15.6 before you
commit.

### 15.6 Self-test for new JS

Ask:

> Is this function **deciding business policy** — running a tool,
> deciding a retry strategy, building a request body the model sees,
> producing a derivation rule, persisting the canonical version of
> something?

If yes → write a server endpoint instead. The JS becomes a thin
`fetch()` + render call.

If no (display, animation, focus, drag-drop, IDB cache, accessibility,
keyboard shortcut) → JS is the right place.

Every new top-level JS file still has to be added to `_BUNDLE_FILES`
in `lib/js_bundler.py` (see §3.2.1) — that hasn't changed.

### 15.7 PR checklist additions

- [ ] **API parity**: any new UI feature is reachable via `/api/v1/*`
      in the same PR.
- [ ] **Boundary**: no business logic added to `.js` files. Display /
      input handling / IDB cache only.
- [ ] **OpenAPI**: new routes carry `@api_meta(...)`. Re-run
      `curl /api/openapi.json | jq '.paths | keys'` — every new route
      appears.
- [ ] **Scope**: new routes use `@require_scope('...')`. Don't invent
      a new scope without adding it to `lib.api_keys.ALL_SCOPES`.
- [ ] **Audit**: state-changing routes call `audit_log(event, …)`
      (per §2.5).
- [ ] **Compat**: if the new feature surfaces in completions, decide
      whether it should also be exposed via the OpenAI / Anthropic
      adapters (`lib/compat/*`).
- [ ] **Docs**: update `docs/HEADLESS_API.md` for any new public
      endpoint or behavioural change.

---

*Last updated: 2026-07-24 — Continued strangler-fig decomposition:
(1) orchestrator/ run_task loop sliced into 7 seam modules (_vu_startup, _prefetch,
_context_inject, _tool_history, _post_loop, _teardown, _finalize) under pt_03f4cdf1;
(2) routes/chat.py fat-handlers seam-split into chat_helpers / chat_task_start /
chat_poll_abort / chat_state / chat_side_effects (+ existing chat_queue /
chat_human_io / chat_tool_state); (3) new lib/model_info/capability_taxonomy.py is
the single source of truth for CHAT_EXCLUDED_CAPS + DISPATCHER_NON_CHAT_CAPS, exposed
via /api/v1/capabilities + /api/v1/server-config and consumed by frontend
static/js/core/model_caps.js (`isChatModel`); the 6 legacy hardcoded chat filters
are gone. (4) Frontend tool_rounds.js `_renderUnifiedToolLine` decomposed into 16
branch-owned helpers under a byte-identity wire-parity harness
(tests/test_frontend_tool_rounds_wire_parity.py) — a permanent regression guard
for tool-panel UI. (5) CSS dead-selector sweep pass-2 uses branch-reachability
judgement (not "rule-internal all-dead") — 323 rules / 32.6 KB removed with
zero dead-rule bytes remaining per audit script debug/css_style_audit.py.
Prior: 2026-07-01 — Same-interface consolidation arc: (1) all six `server_config.json` read-modify-write sites unified onto `json_store.update_json_atomic` (locked RMW, fixes a lost-update race); (2) new shared agent-loop seam `lib/agent_loop.py` (`AbortSignal` + `run_agent_loop`), first adopters paper `report_engine`/`qa_engine`; (3) paper's 3 abort endpoints migrated to `register_task_routes` (poll deferred — see JOURNAL). Prior: 2026-06-11 — Directory-map refresh after the agent-base relocation + trading extraction. `lib/agent_core/` is now the browsable home of the reusable base (run loop, dispatch, endpoint loop, compaction, push hub, `TaskRuntime`, profiles, streaming-event contract); `lib/task_runtime.py` and `lib/push.py` are compatibility shims re-exporting from it. The trading subsystem was extracted to a standalone `tofu-trading` package and now mounts via the `tofu.blueprints` / `tofu.startup` / `tofu.task_runtimes` entry-point groups (`routes/plugin_registry.py`) — no longer in-tree. New in-tree packages: `lib/paper/` (Reading-Mode engine), `lib/billing/` (per-user wallet/ledger/pricing), `lib/orchestration*.py` (declarative multi-agent Studio). Routes reorganized under `routes/api_v1/` as the canonical surface. Frontend monoliths (core.js / ui.js / main.js / settings.js) decomposed into `core/` `ui/` `main/` `settings/` subpackages, with unified `static/js/api.js` as the single backend seam. Prior wave (2026-05-22): Flask→Quart+Hypercorn ASGI migration; `/api/push` WebSocket multiplexer; six shared infrastructure modules; 5 task registries unified onto `TaskRuntime`. Test workflow is now Makefile-driven (`make lint` / `test-unit` / `test-api` / `ci`).*
