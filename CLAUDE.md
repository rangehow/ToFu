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
> (single-file visual diagram). Re-scan those files whenever you add a new
> sub-package or Blueprint.

> **👤 Human-facing docs:** [`README.md`](README.md) / [`README_CN.md`](README_CN.md) are the
> USER-facing product docs (features, how to use them). THIS file is the agent-facing
> rules doc. When you ship a user-visible feature, update the README too — and keep the
> Chinese README in sync. Evolution history (what changed and why) lives in
> [`JOURNAL.md`](JOURNAL.md) — this file keeps only the current rules, not the archaeology.

```
bootstrap.py           — Smart launcher: auto-installs missing pip packages via LLM diagnosis
server.py              — App entry (Quart + Hypercorn), Flask→Quart shim, middleware,
                         logging bootstrap, auto-TLS for HTTP/2, VS Code proxy detection
export.py              — Three-level sanitization export (personal / internal / opensource) — see §10
lib/                   — Core business logic
  log.py               — Centralized logging utilities (get_logger, log_exception, audit_log, log_context)
  # ── Shared infrastructure (used everywhere; see §4.6) ──
  api_response.py      — Unified jsonify helpers: api_ok / api_error / api_not_found /
                         @safe_route. Binding contract + carve-outs + drift ratchet:
                         docs/API_CONTRACT.md (+ tests/test_api_contract_drift.py)
  request_parser.py    — Typed JSON body extraction: parse_body / require_str / require_int /
                         optional_list / etc. — raises BadRequest auto-converted to 400
  http_client.py       — Sync (requests) + async (httpx) HTTP with auto-applied proxy:
                         http_get / http_post / http_stream + async_* variants
  json_store.py        — Atomic JSON file I/O with per-path locking and JSONC tolerance:
                         read_json / write_json_atomic / update_json_atomic / write_text_atomic
  ttl_cache.py         — In-memory TTL cache, LRU eviction, get_or_compute serialization
  agent_verdict/       — Single source of truth for agent-loop decision logic:
                         classify_verdict (STOP / CONTINUE_WORKER / CONTINUE_PLANNER
                         gating + anti-analysis-spiral overrides), detect_stuck,
                         STATE_CHANGING_TOOLS, VU_DONE_SENTINEL.
  agent_loop.py        — Shared multi-round tool-calling loop + abort seam:
                         AbortSignal (one .aborted predicate over threading.Event /
                         task flag / callback) + run_agent_loop() owning the round
                         loop and the 3 abort checks (before-round / post-stream /
                         between-tools). IRON RULE (charter 2026-07-27): NEW agentic
                         capabilities MUST ride this chassis — docs/AGENT_CAPABILITY_GUIDE.md;
                         new private loops are blocked by tests/test_agent_loop_adoption_guard.py.
                         Grandfathered private loops: endpoint/_run.py, orchestrator/_run.py.
  lifecycle_approval.py — Human-approval gate for server lifecycle actions
                         (restart/shutdown): pending-request store, one-time
                         short-TTL tokens, 15-min restart cooldown, script-gate CLI.
  credentials_vault.py — Encrypted credential store (Fernet): values in
                         data/config/credentials_vault.json (600), key in a SEPARATE
                         .credentials_vault.key (600); values never logged; reveal
                         endpoint is the only plaintext exit (audited). THE place for
                         user credentials — never hardcode, never commit (§6 / §10.3).
                         Model-facing: build_vault_index() → <credential_vault>
                         prompt block (names + $ENV vars + notes ONLY, byte-stable),
                         spliced by system_context/_inject.py; exec_env_overlay()
                         injects values into the run_command child env (skill.*
                         entries stay owned by lib/skills/env.py).
                         UI: Settings → Advanced →「凭证保管库」; REST: api_v1/credentials.py.
  task_runtime.py      — Compatibility shim → re-exports TaskRuntime from
                         lib/agent_core/task_runtime.py. See §13.
  push.py              — Compatibility shim → re-exports PushHub + push_event() from
                         lib/agent_core/push.py — backs the /api/push WebSocket multiplexer (see §4.7)
  agent_core/          — Reusable agent base, as one browsable package (lazy PEP 562
                         facade __init__.py; CORE_MEMBERS maps symbol → module):
                         run loop, dispatch, endpoint loop, compaction, push hub,
                         task_runtime.py (real home; lib/task_runtime.py shims it),
                         push.py (PushHub + push_event; lib/push.py shims it),
                         rev_clock.py (conversation-revision clock; re-exported via
                         lib/conversations/meta_cache), events.py (EventType/EventSpec
                         streaming-event contract), profiles.py (capability profiles)
  llm/                 — LLM API communication (package)
    __init__.py        — Public facade re-exporting all symbols
    body.py            — Model-aware build_body(), image validation/downscaling
    cache.py           — Anthropic prompt-cache breakpoints
    chat.py            — Non-streaming chat() entry point
    stream.py          — SSE streaming + tool-call accumulation
    astream.py         — Async SSE streaming variant
    _sse_core.py       — Shared SSE parsing + tool-call accumulation core
    anthropic_outbound.py — Anthropic-native outbound request builder
    responses_outbound/ — OpenAI Responses API boundary (protocol #3):
                         _to_responses / _sse (ResponsesSSETranslator) /
                         _from_responses / _url. Wire gate is
                         api_protocol=='responses' alone (single gate).
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
                         _capabilities / _family / _limits / _max_output +
                         capability_taxonomy.py (source of truth for CHAT_EXCLUDED_CAPS +
                         DISPATCHER_NON_CHAT_CAPS; frontend: static/js/core/model_caps.js)
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
                         modules are facade-preserving PACKAGES (dir/ with a
                         re-exporting __init__.py), not single files:
    orchestrator/      — Main run_task loop (facade __init__.py + _run.py) +
                         extracted slices: _vu_startup / _prefetch /
                         _context_inject / _tool_history / _post_loop / _teardown /
                         _finalize (SSE emission + autopilot baton) / _turn
    manager/           — Task registry + lifecycle (_registry / _persist / _recovery /
                         _stream / _sync / _events / _maintenance / _state)
    commit_round/      — Per-round file-history snapshot (daemon-thread
                         make_snapshot + round_committed SSE) +
                         derive_round_modified_files
    auto_translate/    — Server-side auto-translate safety net for assistant +
                         endpoint-critic messages
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
                         baton handoff markers, CAS state store
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
                         snapshot/diff, destructive-command guards). Filesystem-grep
                         interception: `grep` with file/dir operands (or -r) is REFUSED
                         with a grep_search translation (FUSE bad windows wedged
                         `grep -rn` for 17min+); stream filtering stays legal.
                         Escape hatch: TOFU_RUN_GREP_GUARD=0.
    read_tools.py      — Absolute + project file reading (images/PDF/Office)
    write_tools.py     — write_file / apply_diff / insert_content
    indexer.py, scanner.py, modifications.py, config.py
  swarm/               — Multi-agent orchestration (master, agent, scheduler, planner, review, registry, rate_limiter, artifact_store, synthesis, integration, events, tools, types, …)
  optimizer/           — Nightly self-tuning loop: analyzer → proposer (LLM)
                         → applier → storage. Whitelist auto-apply + ttl_days
                         revert. See routes/api_v1/optimizer.py (REST surface)
                         and lib/optimizer/actions/ for the action registry.
  browser/             — Browser BRIDGE to the user's real Chrome (the server has
                         NO runtime browser): extension command queue (queue/), agent
                         tool handlers (handlers/), fetch_url_via_browser (fetch.py),
                         cookie_capture → auth_sources. Rides the user's logged-in
                         session via browser_extension/ over POST /api/browser/poll.
                         Login-walled / risk-controlled sites are BROWSER-FIRST
                         (docs/SITE_KNOWLEDGE_LAYER_DESIGN.md).
  # NOTE: web search + fetch were EXTRACTED to the standalone `tofu_search`
  # package (orchestrator, engines, rerank, dedup, HTTP/HTML/PDF extraction,
  # content filter). They are NO LONGER in-tree; chatui seams via
  # lib/search_bridge.py + lib/tools/search.py / lib/tools/browser.py. See §11.
  # Login-walled sites (XHS, sankuai): BROWSER-FIRST via BrowserProvider.scrape /
  # browser fetch (live session beats cookie replay — replay from the server IP is
  # THE risk-control trigger) — unless the registry row says cookies_replay/public.
  auth_sources.py      — The SITE-ACCESS REGISTRY (settings「站点接入」): per-site
                         rows {cookies, access_strategy (browser_first|cookies_replay|public
                         — tofu-search path ORDER is this data), aliases, login_url,
                         fields}. The user's LIVE BROWSER SESSION is a first-class
                         credential: browser_first rows work with ZERO stored cookies;
                         cookie values never leave the browser. Internalize = append a row.
  site_knowledge.py    — Per-site extraction knowledge store (doctor-pinned selector
                         OVERRIDES; built-ins serve when unpinned)
  site_doctor.py       — Selector-drift autofix: drift signal → bounded run_agent_loop
                         re-con → pin only the VERIFIED selector pair (auth wall =
                         give_up). TOFU_SITE_DOCTOR=0 kills; 3h per-site cooldown.
  mcp/                 — Model Context Protocol client, registry, config
  memory/              — Memory / stored-notes layer. MODEL-authored experience
                         notes: flat *.md at <project>/.tofu/memories/ +
                         <data>/memories/global/, BM25 prefetch + search_memories.
  skills/              — USER-installed skill packages (AgentSkills format) — a
                         DIFFERENT NOUN from memory/: registry, injection (the
                         <available_skills> index, spliced by system_context/_inject.py),
                         activate_skill progressive-disclosure loader, installer
                         (zip → validated package), catalog. Packages live at
                         <project>/.tofu/skills/<id>/ + <data>/skills/global/<id>/
                         (installs default GLOBAL — external packs are cross-project;
                         project scope hid them from project-less chat mode).
                         env.py — per-skill env/key bindings backed by
                         credentials_vault (keyed skill.<id>.<env_lower>): declared
                         via OpenClaw metadata.requires.env (nested-YAML parse in
                         memory/storage/_frontmatter.py), configured in Settings →
                         Skills, satisfies the eligibility gate, and merges into
                         every run_command subprocess env (exec_env_overlay).
                         Uninstall clears the skill's vault bindings (no orphan
                         secrets); set_skill_scope moves project↔global.
                         Model channel is READ-ONLY (only activate_skill);
                         install/uninstall/toggle are user-only; memory CRUD
                         refuses packages; excluded from the memory corpus.
  conversations/       — Conversation persistence + the Project Brain (cross-conversation
                         coordination): charter, board/epics, activity feed, peer messaging
                         (project_peer.py), path leases, status lane, reconcile —
                         docs/PROJECT_BRAIN.md + docs/PROJECT_BRAIN_STATUS_LANE.md.
  oauth/               — OAuth subscription login (Claude Pro/Max, ChatGPT Codex):
                         manager, PKCE, token store. outbound.py bridges a logged-in
                         subscription into a managed provider slot (the slot's `oauth`
                         marker → live token + client-identity headers, per request)
  desktop/             — Desktop-agent pairing + bridge: pairing.py (6-digit pairing
                         codes + LAN discovery responder), bridge.py, egress.py,
                         remote.py, adapter.py. See §15.2 for the bind/LAN defaults.
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
  motion_video/        — Motion-graphics video pipeline (docs/MOTION_VIDEO_DESIGN.md):
    _recipe.py         — topic → scenes.json: research → script → timeline (fact cards
                         must carry a real source URL; SRT timed from REAL TTS audio,
                         never a chars/second estimate)
    _scene_author.py   — per-scene composition author: bounded run_agent_loop with a
                         NARROW toolset (no render reachable); any failure degrades
                         that ONE scene to _template.py — a bad scene never fails the
                         film. Default OFF (TOFU_MOTION_SCENE_AUTHOR / per-job flag).
    _template.py       — zero-LLM composition floor (always-valid fallback)
    engine.py          — headless worker: recipe → storyboard → narrate → compose →
                         render → concat → sidecar → mux. Crash-resume via job.json +
                         stage checkpoint; finished scenes/compositions never redone.
    runtime.py         — TaskRuntime + dedup index for motion jobs
  longform/            — Long-form research report capability (recipe.py: research →
                         outline → sections(×N) → assemble — a DATA-DEPENDENT stage
                         list riding the checkpoint contract). Ships ZERO bespoke
                         poll/abort routes: generic /api/v1/tasks/* serves it.
  production/          — Production Substrate (docs/PRODUCTION_PIPELINE_DESIGN.md) —
                         the horizontal layer under every "one sentence → finished
                         product" capability; capabilities keep their own thin recipe.
    runtime.py         — ProductionRuntime: thin layer OVER TaskRuntime — dedup index
                         (liveness-checked + self-pruning), create-with-field-shape,
                         append+touch, stale sweep, id minting. All three capabilities
                         (motion-video / paper-podcast / longform-report) ride it.
    jobs.py            — job manifest write/read + crash-resume rescan: re-spawns
                         every job whose manifest still says `running` after a death.
    stages.py          — stage-graph contract (Stage + checkpointed runner): a stage's
                         artifact is committed as soon as its gate passes, so a killed
                         process resumes at the first UNFINISHED stage — crash-resume
                         is a CORRECTNESS contract, not a cost optimization.
                         Deliberately capability-agnostic (guard test AST-asserts no
                         motion_video/tts/llm/paper/audio imports). NOT here
                         (deliberate): binary `deliverable` channel, progress
                         double-projection, artifacts binary format.
  # NOTE: the trading subsystem was EXTRACTED to a standalone `tofu-trading`
  # package (2026-06) and is no longer in-tree. It mounts via the
  # `tofu.blueprints` / `tofu.startup` entry-point groups (see routes/plugin_registry.py).
  database/            — Dual-backend DB layer (PostgreSQL primary, SQLite fallback)
    _core.py           — Connection factory, pool, config (PG_* / TOFU_DB_PATH; legacy CHATUI_DB_PATH still honored)
    _bootstrap.py      — Auto-bootstrap local userspace PG; fallback to SQLite
    _core_schema.py    — SINGLE source of every table definition (SQLAlchemy
                         Core): one Table() → byte-equivalent PG + SQLite DDL.
                         Defining/altering a table happens HERE.
    _schema_pg.py      — PG bootstrap: create_if_absent(Core tables) + PG-only
                         extras (indexes, tsvector/GIN/trigger, ALTER migrations)
    _schema_sqlite.py  — SQLite bootstrap: create_if_absent(Core tables) +
                         SQLite-only extras (indexes, FTS5, ALTER migrations)
    _sql_translate.py  — Permanent SQLite→PG dialect bridge at the wrapper layer
                         (? → %s, json_extract, strftime, …; runs on every PG query).
    _wrappers.py       — Uniform execute() / fetchone() / fetchall() API
    db_paths.py        — PG data-directory placement (FUSE vs local disk) + warnings
    _pg_seed.py        — One-time 21GB pgdata FUSE→local-disk seed. DEFAULT-ON: a
                         plain `python server.py` migrates automatically when
                         unseeded (see §9 rule); failure quarantines the half-copy,
                         legacy stays authoritative, next boot retries (self-healing,
                         no dead-man markers). TOFU_DB_SEED_LOCAL=0 defers this boot;
                         TOFU_DB_LOCAL_SPLIT=0 is the full rollback. Runbook:
                         docs/PG_LOCAL_SEED_RUNBOOK.md.
routes/                — Quart Blueprints. Top-level: chat (+ chat_helpers /
                         chat_queue / chat_human_io / chat_tool_state / chat_state /
                         chat_side_effects / chat_task_start / chat_poll_abort —
                         the chat_send/chat_stream fat-handler seams), conversations
                         (+ _search / _compaction), common, desktop, oauth, translate,
                         upload, artifacts, browser, paper, push, compat_openai,
                         compat_anthropic, api_docs, metrics, legacy_redirects.
  api_v1/              — Headless `/api/v1/*` surface (the canonical API — see §15):
                         agents, agent_run, auth, billing, capabilities, chat,
                         conversations, credentials, daily_report, folders, keys, logs,
                         mcp, memory, skills, oauth, optimizer, orchestrations, paper,
                         project, providers, scheduler, swarm, tasks, translate,
                         update, users, webhooks, …
  __init__.py          — ALL_BLUEPRINTS + register_all(); plugin blueprints mount via
                         routes/plugin_registry.py (entry-point groups — see §4.1)
  plugin_registry.py   — Pluggable Blueprint / startup-hook / TaskRuntime discovery
  _task_routes.py      — register_task_routes() factory: auto-generates /poll + /abort
static/js/             — Frontend (vanilla JS). Unified API client api.js (§3.2.0); large
                         monoliths decomposed into subpackages:
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
scripts/               — Repo tooling: test_select.py (make test-affected reverse
                         index, §7), ratchet_audit.py (guard-ratchet funeral audit
                         → docs/RATCHET_AUDIT.md, §7). NOTE: scripts/ is gitignored
                         by default (`/scripts/*` + whitelist) — new scripts must be
                         whitelisted in .gitignore AND export.py _OPENSOURCE_KEEP_FILES
                         (the gitignore↔export sync pin watches this).
debug/                 — Standalone test/benchmark scripts
tests/                 — pytest-style suites + standalone runners. See §7 for the
                         workflow (Makefile-driven), the jsdom harness contract
                         (expect_pass), and the ratchet-discipline gates.
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
- **Docstrings**: Required on modules and public functions. Use Google-style.
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

This is the single seam between frontend and backend: endpoint migrations
touch one file, cross-cutting concerns (timeout, error shape, auth) live in
one place, and the frontend stays a thin renderer.
Enforced by `tests/test_frontend_api_isolation.py` — a per-file ratchet of
remaining legacy calls that must monotonically decrease.
**Migration playbook**: [`docs/api_client.md`](docs/api_client.md).
**Public surface**: `Api.request / get / post / put / patch / del / stream`
+ per-domain methods; errors throw `ApiError`; `{onError: 'null'}` for
best-effort fetches.

**Streaming exceptions** (allowed because they don't fit the JSON verb model):
- Real-time push events → `pushSubscribe(channel, taskId, fn)` from `push.js`.
- SSE chat stream `/api/chat/stream/<id>` → consumed via
  `Api.chat.streamResponse(taskId, {signal})`, which returns the raw
  Response so the caller (`static/js/ui/sse_pipeline.js`, `static/js/branch.js`) can pipe
  `.body.getReader()`. No file calls this endpoint with a raw `fetch`.

#### 3.2.1 ⚠️ JS Bundler Allowlist — DO NOT FORGET

> **Every new top-level `static/js/*.js` file MUST be added to `_BUNDLE_FILES`
> in `lib/js_bundler.py`. Otherwise it loads as a silent no-op in production.**
>
> **Manifest freshness contract:** `build_bundle()` re-reads `_BUNDLE_FILES` /
> `_DEFERRED_FILES` / `_DEFERRED_ENTRY_POINTS` / `_CRITICAL_FILES` from DISK on
> every build (`_refresh_manifest()`), so a long-running server picks up edits
> WITHOUT a restart. The four assignments MUST stay plain module-level literals
> (no concat / comprehension / conditional) — `_extract_manifest_from_source()`
> parses them with `ast.literal_eval` and anything clever fails LOUDLY.
> Guarded by `tests/test_bundle_manifest_freshness.py`.

`routes/common.py` rewrites `index.html` on every `GET /`, replacing the
individual `<script defer>` tags with one `bundle-<hash>.js` tag. Files NOT
in `_BUNDLE_FILES` are still **stripped** from the served HTML but never
**added back** → the tag silently disappears, no 404, no console error.
**Symptom**: file on disk + tag in `index.html`, yet
`typeof window.yourFunction === 'undefined'` in the browser.

**Fix when adding a new top-level JS module:**
1. Add the filename to `_BUNDLE_FILES` in the correct dependency order:
   `i18n.js` first (`t()` is used everywhere); `main.js` last (it boots the
   app); files that read `main.js` globals at IIFE-load time go AFTER
   `main.js`; files referenced only at runtime can go anywhere before it.
2. Keep the `<script defer src="static/js/foo.js?v=...">` tag in `index.html`
   for the dev-mode fallback (served when bundling fails).
3. Restart the server (the bundler is pure-Python, no hot-reload) — the
   `[Bundle] Built bundle-XXXXXXXX.js (N files, ...)` log line should show `N`
   increased by 1.
4. Hard-refresh the browser — the bundle filename changes via content hash.

**Audit command** (files referenced in `index.html` but missing from `_BUNDLE_FILES`):
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
- **Search first**: find the official SVG ([dashboard-icons](https://github.com/homarr-labs/dashboard-icons),
  [Simple Icons](https://simpleicons.org/), or the brand's asset page).
- **Inline SVG preferred**, sized to context (e.g. 15×15 tab buttons, 20×16 section titles).
- **No unicode glyphs as controls either**: `⤢` / `−` / `+` / `⟳` etc. are
  glyphs with font-dependent metrics that render off-center and font-fallback
  differently across platforms — same prohibition as emoji. Use an inline SVG.

**Alignment — the decision rule (a recurring bug class):** an inline `<svg>`
sits on the text baseline and reserves descender space, so it renders ~2–3px
low next to text and floats off-center in a fixed box. Two correct patterns:

- **Standalone affordance** (button, tile, logo+label row, toolbar control) →
  make the PARENT a flex box and give the icon `display:block`. Use the shared
  `.icon-box` utility (`static/styles.css`, base layer):
  `display:inline-flex;align-items:center;justify-content:center` + `>svg{display:block}`.
- **Icon inline within a sentence** → use `vertical-align` (the `Icon()` helper
  in `static/js/core/icons.js` already bakes in `vertical-align:-0.125em`), and
  match the icon `height` to the surrounding `font-size`.

Traps: (1) `vertical-align` does NOTHING on a flex child — use `align-items:center`;
(2) a centered flex box still looks low without `svg{display:block}` (that line,
not the parent centering, removes the descender gap); (3) `Icon(name,size)`
already carries `vertical-align` — inside a flex parent let `.icon-box`'s
`display:block` win. Guarded by `tests/test_frontend_icon_box_alignment.py`.

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
REMOTE_CLUSTERS = ['sh02-training', 'hldy-training']
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
`.tofu/` (file-history backups + memories + skills), `.tofu_trash/`, `.tofu_sandbox/`,
`.tofu_env.json`. Many mechanisms must recognise these as "agent junk, not source"
(`.gitignore` generation, export sanitizer, self-update skip lists, MCP vendor-copy
excludes). The single-source-of-truth registry is **`lib/agent_artifacts.py`**:

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
for the UI, and every headless cfg-builder historically inherited it unless it
remembered to override — splicing the operator's memories (and global preference
file) into an unrelated API caller's prompt: a hallucination vector AND a
privacy/isolation leak.

**The mechanism (single source of truth):**
- `PERSONAL_CAPABILITIES` in `lib/agent_core/personal_scope.py` — one entry per
  app-personal capability (cfg key + fail-closed `headless_default` + `ui_default`
  + the prompt block it gates).
- `apply_headless_personal_defaults(cfg)` — called ONCE by every headless
  cfg-builder AFTER merging the caller's explicit cfg (`build_chat_config` in
  `lib/tasks_pkg/entry.py`; `_build_cfg` in `routes/api_v1/agent_run.py`;
  `translate_openai_request` / `translate_anthropic_request` in `lib/compat/`).
  It's `setdefault`-based so an explicit caller opt-in ALWAYS wins.
- The UI builder `resolve_conv_config` does NOT call it — the interactive
  product keeps its default-on behaviour, byte-identical.
- The preference profile is its OWN capability (`preferencesEnabled`), decoupled
  from `memoryEnabled` via `resolve_preferences_enabled()` (UI back-compat:
  absent flag → falls back to the memory toggle).

**Rules when adding a NEW capability that injects operator-personal state:**
1. Add ONE `PersonalCapability` entry to `PERSONAL_CAPABILITIES` with
   `headless_default=False`.
2. Gate its prompt injection in the `system_context/` package (`_inject.py`
   for memory, `_profile.py` for the preference profile) on the flag.
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
schema registry). Execution is split across `lib/tasks_pkg/`:

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
| Background tasks (chat / paper / translate style) | `lib.task_runtime.TaskRuntime` (one instance per task kind) — see §13 | local `_tasks = {}` registry with custom append/poll/abort logic |
| Multi-round tool-calling loop + abort/stop | `lib.agent_loop.run_agent_loop(...)` + `AbortSignal.from_event / from_task_flag / from_callback / never` | hand-rolled `for rnd in range(max+1)` shell with per-engine abort checks |
| Server-side push (real-time event channel) | `lib.push.push_event(channel, task_id, event)` (auto-fired from `TaskRuntime.append_event`) — see §4.7 | per-feature WebSocket endpoints |
| Storing a credential (PAT, API key, token) | `lib.credentials_vault` (Fernet, separate key file; REST `routes/api_v1/credentials.py`) | hardcoded literals, committed `.secrets` files, config JSON in git |

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

All shared modules log to the standard `lib.log.get_logger(__name__)` sink and
do NOT swallow errors silently: `json_store` read failures log `WARNING` and
return the default (writes raise); `parse_body` logs unexpected `get_json()`
errors at `DEBUG` and returns `{}`; `api_internal_error` auto-logs at `ERROR`
with traceback (500s never go undocumented); `http_client` does NOT auto-
`raise_for_status()` — the caller logs and decides (matches `requests`).

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

Apply the §2.2 level table to the common shapes:

- **5.1 API / network calls** — timeouts and `RequestException` are `warning` +
  return the empty value; include url + timeout in the message.
- **5.2 JSON parsing** — catch `(json.JSONDecodeError, TypeError)`, log `warning`
  with length + a `%.200s` preview of the raw payload, fall back to `{}`.
- **5.3 DB operations** — `logger.error(..., exc_info=True)` with the SQL
  (truncated) + params, `db.rollback()`, then re-raise.
- **5.4 Background threads** — MUST wrap the entire run loop in try/except so the
  thread can never die silently; log `error` + `exc_info`, back off (sleep),
  and log start/stop:

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
- [ ] **Credentials in the vault**: any new credential a feature needs lives in `lib/credentials_vault` — never hardcoded, never committed (see §4.6 / §10.3).
- [ ] **Startup operations default-on**: a migration/one-time maintenance gated behind a launch-time env var will never fire — make plain `python server.py` trigger it; env only as `=0` opt-out (see §9 rule).
- [ ] **Use the shared infrastructure** (see §4.6): new routes use `api_ok` / `api_error` / `parse_body`; new outbound HTTP uses `http_client.http_get / http_post`; new JSON-on-disk uses `json_store`; new background tasks use `TaskRuntime` (§13); new TTL caches use `TTLCache`. Don't re-grow the patterns we just deleted.
- [ ] **Export sync**: If this change adds/modifies secrets, endpoints, credentials, data dirs, or internal identifiers → update `export.py` (see §10.3).

---

## 7. Testing

> **North star: [`docs/TESTING_STRATEGY.md`](docs/TESTING_STRATEGY.md)** (owner-approved
> 2026-08-04) — layer taxonomy, the anti-negative-optimization discipline
> (failing-first / NEUTER / no coverage-chasing), and the flake SLA. Read it
> before adding a new test suite.

- Test workflow is **Makefile-driven** (pytest markers under the hood):
  - `make lint` — ruff check (blocks CI); `make lint-fix` to auto-fix; `make typecheck` runs `tsc --checkJs` over the vanilla-JS frontend (no build step).
  - `make test-unit` (`-m unit`, full tier ~20 min on this host) / `make test-api` (`-m api`) / `make test-frontend` (jsdom + tsc ratchet; needs `npm install`) / `make test-visual` (Playwright) / `make test-e2e` (hermetic journeys: real app + real Chromium + stub LLM — 12 main-path journeys, patch-miss is red) / `make test-all`.
  - **`make test-affected`** — iteration inner loop: `scripts/test_select.py`'s static reverse index (test file → AST imports + literal path refs) picks only the suites that can see your changes, plus the always-on guard core; >40% selection falls back to the full tier. The FULL tier stays the CI/pre-push gate.
  - `make ci` = lint + unit + api + suite-health + healthcheck; `make smoke` for import/syntax validation.
  - After each batch of test edits, run a `--collect-only` gate.
- **pytest plugin shields are load-bearing**: `PYTEST_BASE = -p no:napari -p no:timeout`
  (Makefile) and `addopts … -p no:timeout -p pytest_timeout` (pyproject). A stray
  napari plugin's import chain and pytest-timeout's entry-point/name double
  registration both crash collection at startup. Bare `pytest` runs should set
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.
- **Frontend (jsdom) contract**: 467+ suites drive real production JS through
  node+jsdom via `tests/_jsdom.py::run_harness`.
  - Skips are silent by default (contributor machines without npm don't break)
    but MUST be loud where frontend execution is expected: `make test-frontend`
    and the CI frontend job set `TOFU_REQUIRE_FRONTEND=1`, turning
    node/jsdom/npm/tsc skips into hard failures (plus a session-end sentinel).
  - New jsdom suites MUST declare `run_harness(..., expect_pass=N)` (N = real
    assertion count; structured `__JSDOM_RESULT__` tail line, not substring
    counting). Undeclared call sites are a ratchet (`test_frontend_harness_expect_ratchet`) — only decrease.
- **Guard ratchets (drift/parity/contract/invariant suites)**: ~150 suites pin
  "only decrease" baselines. New guard suites MUST carry an anchor (NEUTER
  bite-proof or an incident reference — pt_/commit/JOURNAL) or
  `tests/test_ratchet_incident_link.py` rejects them. The quarterly funeral
  audit (`scripts/ratchet_audit.py --write-docs` → `docs/RATCHET_AUDIT.md`)
  reclassifies anchored vs candidate suites; candidates are never auto-deleted
  (deletion needs human judgement).
- **Flakes / pre-existing reds**: three-state triage (introduced-by-this-batch /
  pre-existing / sibling churn) → board ticket with clean-HEAD repro evidence →
  7-day SLA to root-fix or direction-align → expired SLA defaults to deleting
  the test (an ignorable test is worse than none).
- Frontend contracts are also gated by `tests/test_frontend_api_isolation.py`
  (raw `/api/*` fetch count only decreases — §3.2.0) and
  `tests/test_frontend_typecheck.py` (tsc error budget).
- To audit for silent exception handlers, `grep_search` `except` blocks in
  `lib/` and `routes/` and confirm each has a matching `logger.*` call — see §2.2
  (guards: `tests/test_no_silent_except*.py` family).

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
| Run a multi-round tool-calling loop with Stop support | `lib/agent_loop.py` — `run_agent_loop(...)` + `AbortSignal` (§4.6) |
| Push a real-time event to the frontend | `lib/push.py::push_event(channel, task_id, event)` — auto-fired by `TaskRuntime.append_event` (§4.7) |
| Store / reveal a user credential | `lib/credentials_vault.py` (Fernet; key in separate 600 file) + `routes/api_v1/credentials.py` (envelope contract; list masked, reveal is the only plaintext exit, audited). Settings → Advanced →「凭证保管库」 |
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
| Manage memory / stored notes | `lib/memory/storage/`, `lib/memory/tools.py`, `routes/api_v1/memory.py`, on-disk `<project>/.tofu/memories/` (project scope) + `<data>/memories/global/` (global) |
| Install Anthropic / AgentSkills `.zip` packages (drag-and-drop) | `lib/skills/installer.py` → `POST /api/v1/memory/install` (multipart). Packages live as `<.tofu/skills>/<name>/SKILL.md` + references/ + scripts/. `install.sh` is **never auto-executed**; surfaced as `install_hints`. |
| Skills store / curated catalog | `lib/skills/catalog.py`, `routes/api_v1/skills.py`, `static/js/skills.js`, Settings → **Skills** tab |
| Modify trading features | External `tofu-trading` package (extracted 2026-06) — mounts via `tofu.blueprints` entry point; not in this repo |
| Reusable agent base (run loop, dispatch, TaskRuntime, push, profiles) | `lib/agent_core/` (facade `__init__.py`; `task_runtime.py`, `push.py`, `rev_clock.py`, `events.py`, `profiles.py`) |
| Per-user billing / wallet / cost ledger | `lib/billing/` (wallet, ledger, pricing, users, payments/), `routes/api_v1/billing.py` |
| Declarative multi-agent orchestration (Studio) | `lib/orchestration/` (schema + validator), `lib/orchestration_engine.py`, `routes/api_v1/orchestrations.py`, `static/js/orchestration.js` |
| Orchestration typed node I/O (Dify-style dataflow) | OPTIONAL `params.io = {inputs:[{name,type,from}], outputs:[{name,type}]}` on role/subflow nodes (`VALID_IO_TYPES`: text/json/artifact/file/number/bool/any; `from` = `'<id>'`/`'<id>.<out>'`/`'start'`). A node with declared inputs reads ONLY wired upstream outputs (not the scratchpad); an `artifact`-typed output is filled with the worker's state-changing tool manifest. Fully back-compat: no `io` block ⇒ legacy accumulating scratchpad. Tests: `tests/test_orchestration_io.py`. |
| Paper / Reading Mode (reports, Q&A, translate) | `lib/paper/` (report_engine, translate_engine, prompts), `routes/paper.py`, `static/js/paper-reader.js` |
| Daily report subsystem | `routes/api_v1/daily_report.py`, `lib/daily_report/`, `lib/scheduler/` |
| Scheduled / proactive agents, cron, timers | `lib/scheduler/` (manager, executor, cron, timer, proactive), `routes/api_v1/scheduler.py` |
| MCP (Model Context Protocol) | `lib/mcp/` (client, registry, config), `routes/api_v1/mcp.py`, `lib/tasks_pkg/handlers/mcp.py` |
| Use a Claude Pro/Max or ChatGPT subscription as a provider | `lib/oauth/` (PKCE login, token store) → `lib/oauth/outbound.py` bridges a logged-in subscription into a managed provider slot (the slot's `oauth` marker resolves per-request to a live token + client-identity headers). Used via the normal dispatch path — NOT a CLI subprocess. |
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
| Cross-platform compat | `lib/compat/_platform.py` (core, re-exported from `lib/compat/__init__.py`) → `tests/test_cross_platform.py` (smoke test) |
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

> **⚠️ Owner rule (2026-08-05): startup operations are DEFAULT-ON — no activation
> env switches.** A migration / one-time maintenance / mode flip gated behind an
> env var that must be set AT LAUNCH will never fire in the owner's real
> workflow (UI restarts via `os.execv` don't inherit shell env; shell restarts
> require remembering a variable name — the PG seed epic burned 6 human-gated
> retries proving this). Plain `python server.py` MUST trigger it. An env var
> may survive ONLY as an opt-out escape hatch (`=0` defers), and safety must
> come from failure semantics (quarantine + idempotent retry next boot), never
> from an opt-in gate. Reference: `lib/database/_pg_seed.py`. Before adding ANY
> `TOFU_*` env gate to a startup path, ask "will the owner ever type this?" —
> if no, default-on.

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
- **Dual-backend database** (`lib/database/`): tries PostgreSQL 18+ first (better concurrency for 100+ users, JSONB, tsvector); auto-falls-back to SQLite (`data/tofu.db`; legacy `data/chatui.db` is still picked up if present) if PG is unavailable. Force SQLite with `TOFU_DB_BACKEND=sqlite` (legacy `CHATUI_DB_BACKEND=sqlite` still honored). PG is optional — runs as a local userspace process (no `sudo`), auto-bootstrapped via conda when missing. On FUSE mounts the pgdata directory is seeded to local disk automatically on first boot after the split (`lib/database/_pg_seed.py`, default-on — see the rule above).
- Logging: multi-file architecture (rotation/retention per file — see the §1
  `logs/` block) configured in `server.py`, utilities in `lib/log.py`.
- **Per-project config isolation**: All settings (providers, models, features) are stored in
  `data/config/` within the project directory — NOT in `~/.chatui/` (legacy global). This means
  multiple copies on the same machine have fully independent configs, databases, and API keys.
  Config files: `data/config/server_config.json`, `data/config/features.json`, `data/config/daily_reports/`.
  Project-scoped memory notes are stored under `<project>/.tofu/memories/`;
  global memories live in the server-side store `<data>/memories/global/`
  (shared across projects and reachable in a project-less chat; the legacy
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
  - `TOFU_DB_SEED_LOCAL=0` — opt-out only: defer THIS boot's PG FUSE→local-disk seed (the seed itself is default-on; see the rule above)
  - `TOFU_DB_LOCAL_SPLIT=0` — full rollback of the local-disk split (legacy FUSE pgdata stays authoritative)
  - `TOFU_GH_TOKEN` — GitHub PAT for export/publish flows (fallback chain: env → credentials vault → `.secrets/github_token`)
  - `PROXY_BYPASS_DOMAINS` — comma-separated domain suffixes for proxy bypass (e.g. `.internal.example.com`)
  - `CROSS_DC_CLUSTER_MOUNTS` / `CROSS_DC_LOCAL_IDC` — cluster mount map (`cluster1:/path/a,…`) + local DC identifier for FUSE latency detection
  - `TOFU_ENDPOINT_REPLAN` (legacy `CHATUI_ENDPOINT_REPLAN`) — endpoint-mode three-way Critic kill switch (`1` default / `0` to disable). When `0`, the Critic's `[VERDICT: CONTINUE_PLANNER]` is silently downgraded to `[VERDICT: CONTINUE_WORKER]` and the STOP-with-❌ override guard is disabled. Use for hot rollback of the replan redesign without a code change.
  - `TRADING_ENABLED` — gate honored by the external `tofu-trading` plugin (extracted 2026-06); its `register()` returns no Blueprints when unset. No effect on a vanilla core install where the plugin isn't installed.
- Proxy bypass unified: Settings UI bypass domains auto-sync to both `proxies_for()` per-request bypass and `no_proxy` env var (see `lib/proxy.py`)
- Provider templates in Settings UI for one-click provider setup (OpenAI, Anthropic, Meituan, etc.)
- Trading is now an external plugin (`tofu-trading`), not bundled with core; install it + set `TRADING_ENABLED=1` to mount its Blueprints (see `routes/plugin_registry.py`)
- **Cross-platform support** (Linux, macOS, Windows): all platform-specific code
  lives in `lib/compat/` (`_platform.py`) — use its helpers instead of direct
  `fcntl`, `select`, `/proc` access. FS keepalive and `run_command` interactive
  stdin detection are Linux-only (graceful degradation elsewhere).
  `DANGEROUS_PATTERNS` covers Unix + Windows equivalents. Smoke test:
  `tests/test_cross_platform.py`.


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

**The export set is git-anchored (2026-08-05).** Untracked *files* inside
tracked directories are EXCLUDED from internal/opensource exports (a WIP test
or NC temp copy must never reach the public repo), and the exclusion is
printed as a loud list (first 8 names + count) teaching the operator to
`git add` — "worth publishing = worth tracking". `--dry-run` consults the
SAME set as the real copy (preview/copy divergence is what hid this bug
class). `personal` mode is unchanged (full self-backup semantics). Mirror
note: the tar overlay never deletes, so *removing* a file from the public
tree requires a `git rm` in the destination repo, not just stopping the copy.

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
| API keys (hardcoded) | ✓ kept | ✓ kept | ✗ → placeholder | `_SECRETS` dict |
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
| **New API key or credential hardcoded** | Add to `_SECRETS` dict — better: don't hardcode at all, store it in the credentials vault (`lib/credentials_vault.py`, §4.6) |
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
| **New script under `scripts/`** | Whitelist it in `.gitignore` (`/scripts/*` + whitelist) AND in `_OPENSOURCE_KEEP_FILES` — the gitignore↔export sync pin (`test_gitignore_covers_export_excludes`) watches both |

### 10.4 How to comply

When making a code change that introduces sensitive data:

1. **Make your primary code change** as normal.
2. **Immediately open `export.py`** and add the corresponding sanitization
   (e.g. a new hardcoded key → one line in the `_SECRETS` dict).
3. **Verify** with `python3 export.py --mode opensource --dry-run` — check the file shows as "would sanitize".
4. **Run a full export** periodically and check the post-export secret scan passes (0 leaks).

### 10.5 Post-export verification

The `opensource` mode automatically runs `_verify_opensource()` after export, which scans
all text files for known secret patterns (including a full-tree `ast.parse` syntax gate —
a replacement that corrupts identifiers breaks the exported tests loudly, not silently).
If any leak is detected, it prints file:line details and a warning.
**Do NOT publish until 0 leaks are confirmed.**

To add new patterns to the verifier, update the `leak_patterns` list in `_verify_opensource()`.

---

### 10.6 Database auto-creation on exported copies

Exported projects ship with no database file or PG data directory. On first
`python3 server.py` the dual-backend layer (`lib/database/`) bootstraps a
userspace PostgreSQL 18+ (offering `conda install postgresql` when binaries
are missing) or, if PG is unavailable / `TOFU_DB_BACKEND=sqlite`, auto-creates
`data/tofu.db`. Colleagues can `cd tofu-team && python3 server.py` with zero
manual DB setup.

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

- **Pass `max_tokens=128000`** (the convention) to `dispatch_stream(...)` /
  `dispatch_chat(...)` / `_stream_llm_sse(...)` for any long-form task.
- `_clamp_max_tokens()` in `lib/model_info/` then reduces it to each model's
  actual API limit (GPT=32k, Claude=128k, Qwen 16–64k, Doubao=16k, …) — the
  correct way to say **"use as much as the model allows, no artificial cap"**.
- **Never hardcode `max_tokens=4096`** for user-facing long-form output.
  4096 tokens ≈ 3k words — it silently truncates complex reports
  (this exact bug cut Reading Mode's 9-section report off mid-Technical-Reference).

### 12.2 Where this applies

| Code path | Required behavior |
|---|---|
| `routes/paper.py` → `_run_report_task` (report generation) | `max_tokens=128000` |
| `routes/paper.py` → `_stream_llm_sse` (Q&A, translate) | `max_tokens=128000` default |
| Any future "generate a complete/full/comprehensive X" tool | `max_tokens=128000` |
| Short, bounded outputs (e.g. title summarization, 1-line labels) | Small cap is fine (explain why in a comment) |

### 12.3 How to comply

- New long-form path → set `max_tokens=128000`, or document why a smaller
  value is correct for that specific path.
- **Approval note**: RAISING a cap per this rule is a correctness fix (no
  approval needed); LOWERING back to a small cap requires approval.

---

## 13. Background tasks — `TaskRuntime` and `spawn_task`

`TaskRuntime` is the **single source of truth** for every server-side
background task pattern. Five legacy registries (chat, paper-report,
paper-translate, translate, trading-sim) were unified onto it; new code
must follow suit. It lives in `lib/agent_core/task_runtime.py`;
`lib/task_runtime.py` is a compatibility shim, so
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
  `run_task` correctly. All historical spawn sites route through
  `spawn_task`.

### 13.4 Aborting

- Workers MUST poll `task['abort_event'].is_set()` at safe checkpoints
  and call `runtime.finish(id)` (no `error=`) when they observe it —
  the runtime then promotes the status to `'aborted'`.
- If a worker calls `finish(error=)` while `abort_event` is set, the
  error wins (matches the legacy behaviour).
- `runtime.finish` is idempotent — second call returns `False`.

### 13.5 Why this matters

The five legacy registries each had subtly different semantics, and migrating
them revealed real bugs (dropped events on cleanup, missing terminal events,
unbounded TTL growth). Don't re-grow that surface area.

---

## 14. Migration scripts in `tests/_migrate_*.py`

Three checked-in **one-shot** migration scripts exist (already applied;
shouldn't need re-running): `_migrate_api_response.py` (raw `jsonify({...})`
→ `api_ok/api_error/…`), `_migrate_request_parser.py` (`get_json(silent=True)`
→ `parse_body()`), `_migrate_http_client.py` (`requests.X(...proxies...)` →
`http_X`). Contract: **dry-run by default** (`--apply` writes), conservative
(single-line patterns only; skips docstrings/unknown statuses), idempotent,
auto-imports helpers. **Always inspect the dry-run diff before `--apply`.**

Don't add a fourth script in this style: build the unified module + tests
first, migrate one small file by hand to validate the recipe, and only
consider a regex script when 50+ near-identical sites remain.

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

Token transports accepted (priority order): `Authorization: Bearer` (SDK
clients); `x-api-key` (Anthropic convention); `tofu_session` cookie (set on
first browser visit via `?token=…` → redirect + HttpOnly + SameSite=Lax —
subsequent same-origin XHR authenticates from it); `?token=` query string
(first-link convenience); `X-Tunnel-Token` / `TUNNEL_TOKEN` env —
**deprecated** back-compat shim (one-shot boot warning; don't set it).

#### First-boot bootstrap

When `data/config/api_keys.json` is empty (and no `TUNNEL_TOKEN` is
set), `lib.api_keys.bootstrap_personal_key()` mints one
`tofu_admin_<32hex>` token at server start, prints it once to stderr,
and writes plaintext (chmod 0600) to `data/config/.first_run_token`.
The boot banner includes a one-shot URL
`http://host:port/?token=<token>` so opening the browser once installs
the cookie. Disable with `TOFU_AUTO_KEY=0` if you want manual control.

#### Default bind: 0.0.0.0 (all interfaces)

`server.py` defaults `--host` to `0.0.0.0` (owner 2026-08-04 —
bootstrap.py / Docker / install.sh already did; the desktop-agent LAN
pairing flow needs off-loopback reachability). Loopback is the explicit
opt-in: `--host 127.0.0.1` (or `BIND_HOST=127.0.0.1`). The packaged
desktop app pins loopback for itself (`desktop/launcher.py`). The boot
banner still warns loudly on open-auth + non-loopback binds, and the
LAN discovery responder (`lib/desktop/pairing.py`) is ON by default
(`TOFU_DESKTOP_LAN_DISCOVERY=0` disables) but stays silent on a
loopback bind. Drift-pinned by `tests/test_bind_lan_default.py`.

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

- Only the SHA-256 hash is persisted (`data/config/api_keys.json` via
  `lib.json_store`); tokens are `tofu_live_<32hex>` / `tofu_admin_<32hex>`,
  plaintext shown ONCE at creation; issued via `POST /api/v1/keys` (admin)
  or the Settings UI.
- Closed scope vocabulary `lib/api_keys.ALL_SCOPES` — adding a scope = adding
  to that frozenset + using it in `@require_scope(...)` on the route.
- Per-key rate limits (RPM + TPD) via `lib/rate_limit_api.py` with standard
  `X-RateLimit-*` / `Retry-After` headers; cookie-authenticated UI requests
  have admin scope and bypass per-key limits.

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

*Last updated: 2026-08-05 — (1) credentials vault (`lib/credentials_vault.py` +
Settings「凭证保管库」) is now THE place for user credentials; export's own token
chain is env `TOFU_GH_TOKEN` → vault → `.secrets/`. (2) Owner rule: startup
operations are DEFAULT-ON (§9) — plain `python server.py` must trigger
migrations (reference: `lib/database/_pg_seed.py` PG FUSE→local-disk seed);
env vars survive only as `=0` opt-outs. (3) Testing rewired per
docs/TESTING_STRATEGY.md: `make test-affected` inner loop, `make test-frontend`
+ `TOFU_REQUIRE_FRONTEND=1` loud-skip sentinel, mandatory `expect_pass=` on new
jsdom suites, ratchet funeral audit + incident-link gate, 7-day flake SLA,
12-journey hermetic e2e lane, `-p no:napari -p no:timeout` plugin shields.
(4) Export set is git-anchored: untracked files inside tracked dirs are
excluded with a loud list (§10.1). (5) Directory map de-archaeologized —
migration history lives in JOURNAL.md; new map entries: credentials_vault,
agent_core/rev_clock, database/db_paths+_pg_seed, desktop/, scripts/.*
