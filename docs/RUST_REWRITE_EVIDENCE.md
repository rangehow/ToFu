# Rust rewrite: evidence and target architecture

Status: the owner decided on 2026-11-18 to rewrite Tofu fully in Rust, reached
incrementally behind the contracts (A2 below), each phase shippable and
parity-gated by the existing tests. Phase 0 is delivered and the generator
part of Phase 7 has started (see "Progress" below). Phase 2 (manager and
lifecycle) is implemented behind a cutover that keeps the Python manager by
default; see [Phase 2 progress](#phase-2-progress-manager-and-lifecycle).
[Phase 3a](#phase-3a-rust-storagev2-client), the Rust storage.v2 client, is
implemented and differentially verified but not yet wired in; two Phase 5
leaf owners, the MCP client and the channel stdio host, have complete
parity-tested ports, also not yet wired in
([Phase 5 progress](#phase-5-progress-leaf-owners)). The independent Phase 5
leaf [tofu-search](#phase-5-leaf-tofu-search) has its first two steps in
(native text kernels; URL, authority, citation and HTML-extraction layers).
Phase 1, the rest of Phase 3, Phases 4–5 beyond those leaf owners and the rest
of Phase 7 are not delivered here. Phase 6 has all node commands ported, but distribution
still ships the Python node. The one other Rust round that exists is the
evidence-only prototype in [Core loop prototype](#core-loop-prototype).
[Where time goes](#where-time-goes) measures real Turns. The rest of this
document is the design evidence for that decision and answers four questions: what
exists, what it depends on, what the Rust system looks like, and how to reach it
without losing behaviour.

Machine-readable inventory: `output/rust_rewrite/inventory.json`. `output/` is
gitignored and is a separate workspace root, so the generator
(`output/rust_rewrite/measure_inventory.py`) and the hand-authored dependency
classification it validates (`output/rust_rewrite/dependency_classification.json`)
sit next to it. Measured at commit `235644fae3` with tokei 15.0.0 and Python 3.12.13.

## Inventory

### How it was measured

| Quantity | Method (also recorded under `commands` in inventory.json) |
|---|---|
| File set | `git ls-files -z`, tracked files only. An owner is `lib/<child>`, `packages/<child>`, `frontend/src`, `routes`, `serverctl_pkg`, `scripts` or `tests`. |
| Lines of code | `tokei --output json -- <owner files>` (code lines, excluding comments and blanks). A second, tool-independent count of non-blank lines is also stored. |
| Modules | Tracked `.py/.rs/.ts/.tsx/.js/.mjs/.cjs` files per owner. |
| Tests | Regex count over test files: Python `def test_*(`, Rust `#[test]` and `#[tokio::test]`, TS/JS `it(` and `test(` at line start. Tests in `tests/<x>/` are attributed to `lib/<x>` when that owner exists. |
| Contracts implemented | The owner's sources mention the contract's file name (`contracts/*.json|yaml|h`, `docs/contracts/*.md`). This undercounts: an owner that reaches a contract only through generated code is not credited. |
| Dependencies | `tomllib` over every tracked `pyproject.toml` (dependencies plus all extras), and `ast` over every tracked `.py` file (absolute imports, minus the standard library, first-party roots and script-local modules). |

### Size by top-level owner

| Owner | Owners | Modules | Code lines (tokei) | Languages | Tests defined | Contracts referenced |
|---|---:|---:|---:|---|---:|---:|
| `lib/*` | 108 | 2,643 | 667,260 | Python 666,031 | 28,560 in `tests/<owner>/` | 136 contract files across all owners; per owner in JSON |
| `routes` | 1 | 138 | 37,894 | Python | (in `tests/`) | 18 |
| `serverctl_pkg` | 1 | 25 | 10,127 | Python | (in `tests/`) | 5 |
| `scripts` | 1 | 230 | 56,711 | Python 51,551, JS 4,233 | (in `tests/`) | 67 |
| `tests` | 1 | 5,241 | 1,033,448 | Python 1,017,666, JS 14,858 | 42,864 | 81 |
| `frontend/src` | 1 | 1,345 | 311,411 | TS 186,596, JS 90,670, CSS 33,333 | 2,063 (vitest) | 26 |
| `packages/*` | 9 | 941 | 764,089 | Rust 723,979, Python 39,140 | 4,633 | see below |

`packages/*` by package:

| Package | Modules | Code lines | Language | Tests | Role |
|---|---:|---:|---|---:|---|
| `tofu-db` | 421 | 357,446 | Rust | 2,788 | Native storage engine (daemon, cdylib, rlib) |
| `tofu-runtime` | 200 | 349,363 | Rust | 547 | Native agent runtime: `tofu-native` binary and C ABIs |
| `tofu-search` | 196 | 31,370 | Python | 955 | Search and fetch pipeline, published to PyPI |
| `vendor` (reqwest 0.12.28) | 41 | 13,543 | Rust | 98 | Vendored HTTP client |
| `tofu-mtsso-auth` | 14 | 3,373 | Python | 116 | Company SSO adapter |
| `tofu-repo-kernel` | 23 | 2,862 | Rust and Python | 15 | PageRank C ABI, gix discovery, tree-sitter extraction |
| `tofu-workspace` | 9 | 2,494 | Rust | 32 | `tofu-git` workspace and revision prototype (git2) |
| `tofu-environments` | 24 | 2,335 | Python | 82 | Environment provider SDK and its MCP server |
| `tofu-overleaf-mcp` | 13 | 1,303 | Python | 0 | Overleaf MCP server |

The largest `lib` owners (full list of all 108 in JSON):

| Owner | Modules | Non-blank lines | Tests in `tests/<owner>/` | Contracts referenced (first four) |
|---|---:|---:|---:|---|
| `lib/tasks_pkg` | 419 | 114,231 | 4,409 | channels_v1, command_control_v1, compaction_continuity_v1, compaction_handoff_v1 … (20) |
| `lib/storage_authority_sql` | 171 | 64,655 | 566 | conversation_search_v2, conversation_sync_v3, project_dispatch_storage_v2, storage_v2 … |
| `lib/(root modules)` | 110 | 48,605 | — | api_v1_model_routing, api_v4, conversation_sync_v3, model_routing_v2 … |
| `lib/project_mod` | 94 | 48,218 | 3,109 | project_file_receipts_v1, project_undo_*, project_write_intent_v1 … |
| `lib/paper` | 128 | 24,738 | 1,200 | paper_build_v1 |
| `lib/tools` | 71 | 20,756 | 1,851 | conversation_sync_v3, hope_queue_policy_v1, project_messages_v1, rich_response_v1 … |
| `lib/llm` | 62 | 19,866 | 1,329 | (reaches contracts through generated code) |
| `lib/orchestration` | 161 | 18,630 | 934 | EXECUTION_ACCOUNT_CONTINUITY |
| `lib/agent_core` | 55 | 15,755 | 966 | native_agent_stdio_v1, native_agent_checkpoint_v1, device_routing_v1 … |
| `lib/swarm` | 43 | 15,186 | 819 | swarm_messages_v1, tofudb_ir_v1, TOOL_PLUGINS |
| `lib/llm_dispatch` | 51 | 14,294 | 647 | — |
| `lib/browser` | 51 | 12,411 | 709 | device_routing_v1 |
| `lib/slides` | 30 | 12,351 | 315 | — |
| `lib/motion_video` | 32 | 12,293 | 586 | — |
| `lib/conversation_sync` | 30 | 12,018 | 75 | conversation_sync_v3, conversation_settings_v1, conversation_execution_identity_v1 … |
| `lib/model_routing` | 39 | 11,606 | 716 | model_routing_v2 |
| `lib/conversations` | 35 | 11,164 | (in `tests/conversation/`) | conversation_settings_v1, project_coordination_v1, project_messages_v1 |
| `lib/research` | 55 | 11,064 | 845 | research_* |
| `lib/mcp` | 28 | 10,533 | (in `tests/mcp_protocol/`) | — |
| `lib/storage` | 45 | 10,245 | 1,491 | storage_v2, storage_operations_v1, native_storage_connection_v2, config_knobs_v1 |
| `lib/channels` | 47 | 9,077 | 311 | channel_protocol_v1, channels_v1 |
| `lib/router` | 39 | 7,535 | 477 | router_v1, compute_patrol_v1, EVENTS |

Test attribution by directory name is approximate: 36 `lib` owners have no
same-named test directory (their tests sit under another name, such as
`tests/conversation/` or `tests/mcp_protocol/`), so 28,560 of the 42,864 tests are
attributed and the rest stay with `tests` as a whole.

Outside the named owners, still part of a full rewrite (tokei code lines): the
repo-root Python entry points (`server.py`, `server_manager.py`, `supervisor.py` …)
12,426; `bootstrap_pkg` 3,669; `tofu_agent` 3,372; `tofu_db` (Python bindings)
1,814; `tofu_workers` 1,423; `export_pkg` 6,678; `desktop` 3,311; `bundles` 15,478;
`plugins` 36,775. Not rewritten: `browser_extension` (JS, 5,869), `android` (Kotlin,
25,178), `ios` (Swift, 6,321), `evaluations` (39,438) and `benchmarks` (49,488).

**What a full rewrite ports.** About 0.79 million lines of Tofu-authored Python
(`lib` 666k, `routes` 38k, `scripts` 52k, `serverctl_pkg` 10k, root and packages
about 25k, `tofu-search` 31k), against about 0.72 million lines of Rust that
already exist (723,979 in `packages/*`, almost all in `tofu-db` and
`tofu-runtime`). The frontend (311k lines) does not
move. The test suite (1.03 million lines, 42,864 tests) is the parity oracle; it is
not translated line by line.

### Contracts and their implementers

All 136 contract files and the owners that reference each one are in
`inventory.json` under `contracts`. Five `docs/contracts/` documents have no code
reference (`COMPAT_ANTHROPIC.md`, `COMPAT_OPENAI.md`, `CONVERSATION_DIAGNOSTICS.md`,
`OPENAI_RESPONSES_FEATURES.md`, `VERSIONING.md`). The four contracts named in the
assignment:

| Contract | Form | Size | Implemented by | Rust generated today |
|---|---|---|---|---|
| `contracts/conversation_sync_v3.yaml` | OpenAPI 3.1 | 22 paths, 139 schemas | `lib/conversation_sync`, `lib/storage_authority_sql`, `lib/tools`, root modules, `packages/tofu-db`, `frontend/src` | Yes: `generated_conversation_change_page.rs`, `generated_system_note_vocabulary.rs` (`scripts/gen_conversation_sync_contract.py`) |
| `contracts/tofudb_ir_v1.json` | JSON | 6,187 lines | `packages/tofu-db`, `lib/storage_authority_sql`, `lib/tasks_pkg`, `lib/swarm`, `serverctl_pkg` | Yes: `generated_tofudb_ir.rs` and others (`scripts/gen_tofudb_ir_contract.py`) |
| `docs/contracts/API_CONTRACT.md` | Prose | 1,695 lines | `routes`, `lib/openapi`, `lib/tasks_pkg`, `frontend/src` | No. Machine authorities are `contracts/api_v4.yaml` (2 paths) and `contracts/api_endpoint_index_v1.json` (764 endpoints, projected from handler `@api_meta` declarations) |
| `docs/contracts/EVENTS.md` | Prose | 1,187 lines | `lib/agent_core`, `lib/router`, `lib/tasks_pkg`, `frontend/src` | No. The authority is the Python registry in `lib/agent_core/events.py` and `PUSH_FRAME_SPECS`; `scripts/gen_event_contract.py` derives TypeScript from it |

About 26 tracked `.rs` files are already generated from contracts or Python
registries (16 in `tofu-db`, 8 in `tofu-runtime`, 2 in `tofu-repo-kernel`). Several
generators read a **Python registry** as the source (for example the storage
operation catalog from `lib/storage_authority_sql/operation_domains`), so the Python
code is still the oracle the Rust is checked against.

### Dependency map

123 third-party distributions: the 78 declared in tracked `pyproject.toml` files,
plus 45 imported but declared nowhere or only transitively. Every one is
classified; the measurement script fails coverage if one is missing (current
result: 0 unclassified declared, 0 unclassified imported). Verdicts: **74 mature, 22
partial, 13 none, 14 not needed in Rust**. Planned dispositions: 61 port to Rust,
19 stay as a sidecar, 26 stay as black-box test or eval harnesses, 17 dropped. The
full per-package table (declared-in, imported-by, crate, reason) is
`inventory.json` → `dependencies`. Crate maturity was checked on 2026-11-18
against crates.io and project READMEs (sources:
[axum](https://crates.io/api/v1/crates/axum),
[reqwest](https://crates.io/api/v1/crates/reqwest),
[rmcp / MCP Rust SDK](https://github.com/modelcontextprotocol/rust-sdk),
[async-openai](https://github.com/64bit/async-openai),
[Anthropic client SDK list](https://docs.anthropic.com/en/api/client-sdks),
[ppt-rs](https://docs.rs/ppt-rs/latest/ppt_rs/),
[PyO3 free-threading](https://pyo3.rs/latest/free-threading.html)).

Findings on the areas the assignment singled out:

| Area | Python today | Rust | Verdict | Disposition |
|---|---|---|---|---|
| LLM providers and streaming | **No provider SDK is imported by the product.** `lib/llm` and `lib/llm_dispatch` are first-party HTTP clients; `openai`/`anthropic` appear only in `tests/sdk` compatibility tests. | Already first-party in `tofu-runtime` (`sse.rs`, `provider.rs`: Chat, Responses, Anthropic). async-openai exists; no official Anthropic or Gemini Rust SDK. | mature (own code) | rust |
| Responses WebSocket transport | `websockets` | tokio-tungstenite | mature | rust |
| MCP client and server | `mcp` 2.x (stdio and SSE clients; servers are subprocesses) | rmcp 3.11 (official): client and server, stdio and Streamable HTTP; the old HTTP+SSE transport is not implemented | mature | rust, plus a small SSE client shim while configured servers still use SSE |
| Slides and Office | python-pptx, python-docx, openpyxl, xlrd, olefile, fontTools | ppt-rs (young), docx-rs (writer), calamine and rust_xlsxwriter, cfb, fontations and allsorts | pptx/docx partial; xlsx/xls/ole mature; fonts partial | pptx/docx sidecar; spreadsheets, OLE and fonts rust |
| PDF | pypdf, pymupdf, pymupdf4llm, pymupdf-layout, docling-core | lopdf (structure), pdfium-render or mupdf (C library); nothing like pymupdf4llm or docling | partial / none | sidecar (document worker) |
| LaTeX | `latexmk`/`xelatex`/`pdflatex` subprocesses | Same binaries; tectonic optional | n/a | external binary |
| Browser and CDP | The user's browser through Tofu's own extension protocol (`/api/browser/poll`); Playwright for headless probes, tofu-search fallback and the test tier | Extension protocol is plain HTTP, so Rust-native; chromiumoxide/fantoccini are partial; the `playwright` crate is dead | partial | extension path rust; headless Playwright sidecar; e2e tier black-box |
| Search (tofu-search) | First-party package (31k lines, 955 tests) built on httpx, bs4, lxml, trafilatura, pypdf, Playwright | reqwest, scraper, dom_smoothie (no trafilatura-level metadata) | partial | port to Rust with the 955 tests as the oracle; trafilatura stays a sidecar until extraction parity |
| Tokenizers | tiktoken, deepseek_tokenizer, transformers (counting) | tiktoken-rs (already in `tofu-runtime`), HF tokenizers | mature | rust |
| Git | `git` CLI through subprocesses (39 files); GitPython only in the Overleaf MCP server | git2 (already in `tofu-workspace`), gix, or keep the CLI | mature | rust (keep the CLI for porcelain parity) |
| SQL Sidecar and Postgres | psycopg 3 (and legacy psycopg2), sqlite3 | tokio-postgres or sqlx; rusqlite already in `tofu-db` | mature | rust |
| Channels | Feishu through `lark_oapi` (undeclared); WeCom, QQ, WeChat and GitHub are first-party HTTP adapters; 大象 (DX) is an out-of-tree bundle adapter with a Node bridge (`bundles/yourprovider/tofu_yourprovider/dx/bridge.mjs`) | open-lark is unofficial and stale; WeCom/QQ/WeChat have only hobby crates; octocrab for GitHub | Feishu partial, IM none, GitHub mature | rust over reqwest; bundle adapters keep running behind the existing stdio JSON-RPC channel protocol (`lib/channels/protocol/stdio.py`) |
| Evaluation harnesses | harbor, swebench, pptagent, slidedeckai, llm_mcp, ddgs, pssdk | n/a | none | black-box: they drive Tofu over its API and stay Python |
| HTTP server | Quart, Hypercorn, h2, wsproto, werkzeug | axum, hyper, tower-http | mature | rust |
| Validation | pydantic, jsonschema | serde, schemars, jsonschema | mature | rust |
| JS tool runtime | wasmtime (Python bindings) | wasmtime (the reference implementation) | mature | rust |

Adjacent findings: `pathspec` is declared in core dependencies but nothing
imports it. Several runtime imports are undeclared in any `pyproject.toml`:
`bcrypt`, `lark_oapi`, `onnxruntime`, `docling_core`, `fast_langdetect`,
`pyautogui`, `pyperclip`, `pystray`, `imageio_ffmpeg`, `numpy`, `matplotlib`,
`redis` (declared only under `scale`) and `akshare` in `plugins/tofu-trading`.
This document does not fix them.

### Where user Python runs

These surfaces execute Python that the user, a skill or a plugin wrote, so a
managed Python runtime must stay available even when no Tofu code is Python:

| Surface | Mechanism | Evidence |
|---|---|---|
| Skills (`.tofu/skills/*/scripts`, `bundles/yourprovider/skills`) | The model runs bundled scripts through `run_command` with the application interpreter; scripts import packages such as paramiko | `pyproject.toml` `app` extra (paramiko comment); `lib/skills/` only injects guide text; `lib/skills/automation/mtskills_cli.py:248` spawns Node |
| `run_command` and background commands | Arbitrary shell; the interpreter directory comes first on PATH | `lib/project_mod/run_command.py`; `lib/tools/process_environment.py:161` |
| Scheduler `python` tasks | `[sys.executable, "-c", command]` | `lib/scheduler/manager.py:93,795-798` |
| Hope/MLP queue-policy shims | Helper launched with `sys.executable` | `lib/tools/hope_queue_policy_launcher.py:158-163` |
| In-process plugins | Entry-point groups `tofu.tools`, app plugins, `tofu.channel_adapters`, environment and compute providers, experiments, features | `lib/tools/registry/_plugins.py:48,76`; `lib/app/plugin_registry.py`; `bundles/yourprovider/pyproject.toml`; `plugins/tofu-trading` |
| Out-of-process channel adapters | stdio JSON-RPC (DX and future adapters) | `lib/channels/protocol/stdio.py`, `docs/CHANNEL_PROTOCOL.md` |
| MCP servers | npx, uvx or python subprocesses | `lib/mcp/client/_bridge.py:1482-1556` |
| Linear Git checkpoint gate | Runs `ruff` against the user's repository | `pyproject.toml` `test` extra (ruff comment) |
| Document worker capabilities | python-pptx, pymupdf4llm, python-docx, docling, matplotlib | dependency map, sidecar rows |

## Target architecture

### Endpoint

Every line of Tofu-authored server, manager, storage, node and CLI code is Rust,
shipped as one `tofu` binary. The browser frontend (TypeScript), the browser
extension, and the Android and iOS shells are unchanged. Python survives only
where Python is the product's input rather than its implementation:

1. a uv-managed Python runtime for user code (the table above);
2. a plugin host for third-party Python plugins (`tofu.tools`, channel adapters,
   environment providers, `tofu-trading`);
3. a document worker for the capabilities whose Rust verdict is `none` or
   fidelity-limited `partial` (pymupdf4llm and pymupdf-layout, docling,
   python-pptx and python-docx editing fidelity, trafilatura extraction,
   matplotlib). Each worker capability has an exit criterion: it is ported when a
   Rust implementation passes the same golden-file corpus.

Black-box evaluation harnesses stay Python because they talk to Tofu over its
public API.

### Process model

**Problem.** Today a personal install runs `server_manager.py`, the Quart API host
(`server.py`), the resident execution host (`serverctl_pkg/execution_host.py`),
on-demand execution shards from a fork server (`serverctl_pkg/execution_shards.py`),
one storage authority process (the `tofu-db serve` daemon or the Python SQL
sidecar), `supervisor.py` for Android, and MCP and channel children. The shards
exist only because of the GIL: `execution_shards.py:3-10` records 2.0 s per round
at 20 sessions against 0.09 s for one. The API/execution split forces a private
control RPC (`contracts/command_control_v1.json`) for state that is process-local.

**Decision.** One binary, roles as subcommands, and three resident processes in
personal mode:

| Process | Subcommand | Owns |
|---|---|---|
| Manager | `tofu manager` | The lifecycle state machine now in `server_manager.py`: launch lease, pipe-EOF ownership, restart with one commit point, `logs/server-manager.log`, restart preparation (`contracts/restart_preparation_v1.json`), engine certification (`serverctl_pkg/native_engine.py`). Small and rarely restarted. |
| Application | `tofu serve --role all` | HTTP, SSE and WebSocket front door; the execution engine (agent loop, tools, swarm, scheduler, recovery); MCP client; channel hosts. Roles `api`, `execution` and `scheduler` (from `bootstrap_pkg/process_roles.py`) stay selectable for distributed deployments. |
| Storage authority | `tofu db serve` | The existing `tofu-db` engine, unchanged. A SQL authority (SQLite or Postgres, ported from `lib/storage_authority_sql`) is a mode of the same subcommand, so storage stays swappable (AGENTS.md design principle 1). |

Children are started on demand and are never part of the core: MCP servers,
channel adapters, the Python plugin host, the document worker, headless Chromium,
and user commands.

When the API and execution roles share a process, `command_control_v1` becomes an
in-process channel with the same message types. When they are split, it stays a
loopback RPC. Execution shards and the fork server are deleted rather than ported:
without a GIL there is nothing for them to fix.

**Alternatives considered.**
- *Embed `tofu-db` in the application process* (the rlib and `embed.rs` already
  exist). This was rejected as the default because it breaks two current
  guarantees. Application restarts would depend on the engine build, which
  `docs/NATIVE_STORAGE_LIFECYCLE.md` ("Application restarts never depend on the
  storage engine build") forbids. And a fail-closed authority would share a crash
  domain with the web process (`docs/ARCHITECTURE.md`: the web process never
  opens the database). Embedding stays available for `tofu agent run` and for
  tests.
- *Keep separate API and execution processes in personal mode.* This was
  rejected: the split exists for the GIL and for restart isolation. Restart
  isolation is already provided by restart pause and resume (`docs/RUN_CONTINUITY.md`).
  Distributed mode keeps the split.
- *Fold the manager into the application.* This was rejected because a restart's
  owner cannot be the process being restarted (`docs/modules/infra_runtime.md`,
  "A restart that stopped its old server always ends serving").

### Async runtime

**Decision.** tokio's multi-threaded runtime in every process, with axum, hyper
and tower for the front door, reqwest through the shared network authority
(`docs/NETWORK.md`) for outbound traffic, and tokio-tungstenite for the Responses
WebSocket. `tofu-runtime` today enables only tokio's current-thread features; that
is widened. `tofu-db` stays synchronous internally with its own threads behind its
wire protocol.

Rules that keep the hardware policy (`docs/RESOURCE_CONTROL.md`, "Hardware never
limits work") intact:
- Every accepted unit of work is spawned at once. Admission has no semaphore,
  bounded channel or bounded pool.
- tokio's blocking pool defaults to a cap (`max_blocking_threads` = 512) that
  queues work beyond it. Blocking filesystem calls, which matter on the FUSE mount
  (`server.py:829-888` already offloads static files for that reason), use an
  uncapped blocking pool or dedicated threads. They never run on async workers.
- tokio sizes its worker-thread count from the CPU count. That count is
  scheduling parallelism, not admission, because no task is refused or queued
  behind a capacity check. It still must be declared in
  `contracts/resource_policy_v1.json`. **Open question for the owner:** whether
  a CPU-derived scheduler width is acceptable under that policy, or must be a
  fixed constant. `tests/meta/test_hardware_never_limits_work.py` changes only by
  owner decision.
- Cancellation is explicit: `CancellationToken` plus joined shutdown. Dropping a
  future never ends a tool effect silently (see the behaviour list).

**Alternatives considered.** async-std and smol: smaller ecosystem, and `tofu-runtime`
and reqwest are already on tokio. Thread-per-turn without async: it reproduces
today's DemandExecutor thread carriers but cannot scale fan-out without pools,
and pools are what the hardware policy rules out.

### Contracts as the single source of Rust types

**Problem.** The machine contracts are canonical (`contracts/AGENTS.md`), but
three gaps remain. Several generators read a Python registry as their source. The
764-endpoint HTTP surface has metadata (`api_endpoint_index_v1.json`) but no
request or response schemas outside conversation sync and v4. `EVENTS.md` is prose
over a Python registry.

**Decision.** Invert every Python-registry generator so that the YAML or JSON
contract is the source, then generate Rust, TypeScript and (for the transition)
Python from it:

| Contract | Generated Rust | Generated TypeScript | Notes |
|---|---|---|---|
| `conversation_sync_v3.yaml`, `api_v4.yaml` | serde types for all 139 schemas, route trait stubs, SSE frame enums | `conversation-sync.generated.ts` and `api-v4.generated.ts` (as today) | Extend `scripts/gen_conversation_sync_contract.py`, which already emits Rust |
| New `contracts/api_v1.yaml` (OpenAPI 3.1) | Request, response and error types for the 764 endpoints | Replaces hand-written DTOs in `frontend/src/api/endpoint-registry` gradually | Bootstrapped from `@api_meta`, handler schemas and recorded traffic from the Python server; Python routes validate against it before any Rust route exists |
| New `contracts/events_v1.json` | Event and push-frame enums with field specs | `event-contract.generated.ts` (today generated from Python) | Exported from `lib/agent_core/events.py` and `PUSH_FRAME_SPECS`, then made authoritative; `EVENTS.md` becomes reference prose |
| `tofudb_ir_v1.json`, `storage_v2.json`, `storage_operations_v1.json`, `worker_control_v1.json`, `tofusql_catalog_v1.json` | Already generated | — | The storage operation catalog becomes authored JSON instead of a projection of `lib/storage_authority_sql/operation_domains` |
| `native_*_abi_v1` | Become in-process Rust APIs; the C headers retire with their Python callers | — | |
| JSON Schemas (`*_v1.schema.json`) | typify-style serde types plus schemars round-trip checks | Existing `gen_*_contract.py` TypeScript outputs | |

The generators stay in-repo, with check mode in CI. They move from Python
scripts to the Rust `xtask` `packages/tofu-xtask` (Phase 7, started early
because it needs no owner to move first; see "Progress"), which removes Python
from the build.
Third-party OpenAPI generators were considered: progenitor generates clients,
not servers, and openapi-generator's Rust server output does not cover Tofu's SSE
framing or its canonical-JSON byte rules. Keeping one in-repo generator that
emits Rust and TypeScript from the same pass is what keeps the frontend in step.

### Plugin and tool boundary

**Decision.** Capabilities that stay in Python run in **subprocesses that speak a
versioned JSON-RPC protocol over stdio**. This is the pattern Tofu already uses for
channel adapters (`lib/channels/protocol/stdio.py`: own process group, credentials
through the environment and never argv, stdin close as the stop signal, process
group reaped on close) and for MCP. There are three hosts:
- `tofu-pyhost plugins`: loads entry-point plugins and exposes their tools,
  channel adapters and providers through the same tool contract the Rust registry
  uses (`docs/contracts/TOOL_PLUGINS.md` semantics: process-global, model-visible
  only when allow-listed).
- `tofu-pyhost documents`: pptx, pdf, docx, docling and matplotlib operations
  with file-in/file-out arguments.
- MCP servers, unchanged.

The first two hosts are started lazily on first use, so they add nothing to
startup or idle. Tofu publishes a thin Python SDK wheel (`clients/python`,
optionally built with maturin) as the plugin authoring API. Sandboxed extensions
that need no OS access use WASM through wasmtime's component model, which
extends today's JavaScript-in-wasmtime tool runtime.

**Alternatives considered.**
- *Embed CPython in the core through PyO3.* Rejected. A segfault in a C extension
  (PyMuPDF, lxml) would take down the server. The GIL would return to the process
  whose GIL cost motivated the rewrite. Every core binary would be tied to one
  CPython ABI, which breaks single-binary distribution (no abi3 for free-threaded
  builds). Plugins would also share memory with authority code. PyO3 remains fine
  inside the optional SDK wheel.
- *WASM for everything.* Rejected. Python plugins need native wheels, the
  network and the filesystem, and WASI cannot host lxml or PyMuPDF.
- *Per-call `python -c` subprocesses.* Rejected: they pay interpreter start and
  import cost on every call and keep no warm state.

### Single-binary distribution and multi-device nodes

**Decision.** A single `tofu` executable per platform, built with cargo,
cargo-zigbuild for Linux glibc and musl on x86_64 and aarch64, and macOS and
Windows targets. Releases use cargo-dist or the existing desktop installers
(NSIS, deb). The binary contains:
- the manager, application, storage engine, CLI (`tofu agent run|doctor|serve`)
  and migrations;
- the node program `tofu node`, which replaces `lib/desktop_agent`: input through
  enigo, screenshots through xcap, clipboard through arboard, tray through
  tray-icon. It keeps the same outbound `/api/desktop/poll` protocol
  (`contracts/desktop_node_v1.schema.json`, `desktop_bridge_v3.schema.json`);
- the Vite build output, embedded with rust-embed at the same `static/` paths and
  cache headers.

**Multi-device nodes.** Every install is a peer node, as today. The server's
download origin (`lib/desktop_dist`) serves this same binary per platform instead
of a PyInstaller bundle (`tofu.spec`, about 153 MB). A node never needs Python
unless it runs user Python. On Android the local kernel (`local_kernel.py` under
proot) becomes `tofu serve --role all --bind 127.0.0.1`, built for
`aarch64-linux-android`.

**Downloaded on first use, never at startup:** the uv-managed Python runtime,
Chromium for headless work, ffmpeg and a TeX distribution. Each is verified by
digest, as the sealed `tofu-native` archive already is
(`contracts/native_runtime_artifact_v1.json`).

### Frontend stays unchanged

The browser keeps the same client. The Rust server must reproduce, byte for byte
where the browser depends on it:
- URL paths and methods for all 764 indexed endpoints;
- the error envelope (`lib/error_envelope`);
- cookie, auth and identity headers (`contracts/identity_v1.yaml`);
- SSE framing (event names, `id`, `retry`, heartbeat and resume semantics of
  `/api/v4/conversation-events`);
- the `WS /api/push` multiplex and `control_rpc_v1` JSON-RPC limits and error codes;
- static asset paths, `Cache-Control` and the service worker.

Parity is proven in three ways:
1. contract tests generated from the OpenAPI documents;
2. wire-fixture replay (the `scripts/gen_wire_fixtures.py` and
   `tests/_tool_rounds_wire_parity_*` pattern), recorded from the Python server
   and replayed against the Rust one;
3. the e2e and visual tier (`tests/e2e`, `tests/visual`) run unchanged against
   either server URL.

### Defeated alternatives

**A1. Big-bang rewrite.**
- *Problem:* rebuild everything, then switch over once.
- *Decision:* rejected in favour of A2. The owner's indifference to cost does not
  rescue it, because the problem is verification, not effort. About 0.79 million
  lines of Python carry behaviours encoded in 42,864 tests and dozens of incident
  fixes (list below). A big-bang can measure parity only at the end, has no
  shippable midpoint, and leaves the running product on frozen Python code for
  the whole duration. The absence of new features makes a freeze cheaper but does
  not make parity checkable earlier.
  This agrees with the existing decision in `docs/MONOREPO.md` ("Rust migration
  quality"), which rejects a wholesale rewrite and any permanent Python/Rust
  mirror. This plan keeps that rule and adds the explicit full-Rust endpoint.
- *Alternatives considered:* A2 strangler; A5 hot paths only.

**A2. Incremental replacement behind contracts (strangler) to full Rust (chosen).**
- *Problem:* reach the full-Rust endpoint without losing behaviour.
- *Decision:* make the contracts authoritative first, put a Rust front door in
  front of Python, then move one owner at a time. Each owner moves only when its
  black-box parity tests pass against Rust, and the Python owner is deleted in the
  same phase.
- *Alternatives considered:* A1 big-bang, which cannot be verified until the end;
  A5, which never reaches the endpoint.

**A3. Rust core with a permanent in-process Python plugin layer (PyO3).**
- *Problem:* keep Python's document and plugin ecosystem without sidecars.
- *Decision:* rejected for the core (see "Plugin and tool boundary"). Accepted in
  reduced form: Python stays out of process for the three named hosts, each with
  an exit criterion.
- *Alternatives considered:* subprocess hosts (chosen); WASM-only; per-call
  interpreters.

**A4. Go instead of Rust.**
- *Problem:* a compiled, concurrent, single-binary server with a simpler
  language.
- *Decision:* rejected. About 0.72 million lines of production Rust already exist
  (`tofu-db` 357k, `tofu-runtime` 349k, generated from the same contracts). Go
  would either keep a cgo boundary to the storage engine and agent runtime (two
  core languages and an FFI on the hottest path) or rewrite both. Go does have
  two real advantages: official Anthropic and Lark/Feishu SDKs and an official MCP
  SDK. They are outweighed because provider and IM clients are thin HTTP code that
  Tofu already writes itself, and rmcp is official on the Rust side.
- *Alternatives considered:* Rust (chosen); Python with hot paths (A5).

**A5. Stay in Python and move hot paths to Rust (the current direction).**
- *Problem:* this is what `docs/ARCHITECTURE.md` "Native runtime foundation"
  pursues: native kernels (`tofu-native` over stdio, cffi ABIs for token counting,
  HTML projection and checkpoints) with Python as the composition owner.
- *Decision:* rejected as an endpoint, kept as the shape of the early phases. It
  leaves every behaviour implemented twice (a Python oracle plus Rust parity
  fixtures), keeps boundary crossings (stdio JSON, cffi) on hot paths, and keeps
  GIL-driven machinery (execution shards, the fork server, the FD-passing broker).
  It also keeps a Python-plus-native install that cannot be a single binary.
- *Alternatives considered:* A2, which uses the same kernels but finishes the
  move and deletes the oracle.

### Phased plan

Every phase ships to the owner's running install. A phase is done when its
parity gate passes against the Rust implementation and the replaced Python is
deleted. The gates come from the existing suite: tests that exercise HTTP, SSE,
storage wire or process behaviour are run **black-box** against a server URL, and
white-box Python tests for a moved owner are rewritten as Rust tests from the
same cases. Before Phase 1, the 42,864 tests are classified into these two kinds
and recorded in a manifest.

| Phase | Ships | Parity gate (existing tests and contracts) |
|---|---|---|
| **0. Contracts first** | `api_v1.yaml` for the 764 endpoints; `events_v1.json`; storage operation catalog authored as JSON; every generator emits Rust, TS and Python with `--check`; black-box harness that can target any server URL; test classification manifest. No product behaviour changes. | All existing generator `--check` runs; `tests/meta`; frontend generated-file freshness; `tests/tofudb/test_tofudb_pre_authority.py`; the Python server passes its own black-box harness |
| **1. Rust front door** | axum serves static assets, auth and identity middleware, conversation SSE, `WS /api/push` and `control_rpc`, and reverse-proxies every other route to Python | `tests/sse`, `tests/websocket`, `tests/static_serving`, `tests/auth`, `tests/identity`, `tests/e2e`, `tests/visual`, frontend vitest unchanged |
| **2. Manager and lifecycle** | `tofu manager` replaces `server_manager.py`, `serverctl`, `supervisor.py` and resident-service ownership; restart preparation; engine certification | `tests/server` (`test_restart_always_serves.py`, `test_server_reexec.py`, `test_execution_host_signal_stop.py`), `tests/lifecycle_approval`, `tests/chaos/test_chaos_nodes.py`, `contracts/restart_preparation_v1.json` |
| **3. Storage clients and SQL authority** | Rust storage.v2 client inside the application; `lib/storage_authority_sql` ported as the SQL mode of `tofu db serve` (rusqlite, tokio-postgres) | `tests/storage`, `tests/storage_authority_sql`, `tests/tofudb` (differential suite), `tests/json_store`, `storage_v2.json` and `tofudb_ir_v1.json` checks |
| **4. Execution engine** | Agent loop, provider streaming, tool loop, settlement, compaction and tool-effect recovery on `tofu-runtime` (the parallel prototype's output); the Python execution host and shards deleted; Python tools reachable through `tofu-pyhost plugins` | `tests/tasks_pkg`, `tests/turn_lifecycle`, `tests/compaction`, `tests/llm`, `tests/llm_dispatch`, `tests/swarm`, `tests/chaos/test_chaos_faults.py`, `contracts/task_event_delivery_v1.json`, `native_agent_*` schemas |
| **5. Domains, one owner at a time** | Order follows dependencies: conversations and sync; `project_mod` (with `run_command`); model routing and registration; MCP (rmcp); channels (the stdio protocol keeps bundle adapters running); router, orchestration and swarm; scheduler and automations; memory, knowledge and research; paper and slides orchestration (rendering through the document host); browser and desktop server side; tofu-search | Per owner, in this dependency order: conversations and sync `tests/conversation`, `tests/conversation_sync`; `project_mod` with `run_command` `tests/project_mod`; model routing and registration `tests/model_routing`; MCP (rmcp) `tests/mcp_protocol`; channels `tests/channels`, `tests/feishu`; router, orchestration and swarm `tests/router`, `tests/orchestration`, `tests/swarm`; scheduler and automations `tests/scheduler`, `tests/automations`; memory, knowledge and research `tests/memory`, `tests/knowledge`, `tests/research`; paper and slides orchestration `tests/paper`, `tests/slides`; browser and desktop server side `tests/browser`, `tests/desktop`; tofu-search `tests/search`, `packages/tofu-search/tests`; owner contracts from `inventory.json` |
| **6. Node and distribution** | `tofu node` replaces `lib/desktop_agent`; one binary per platform served by `lib/desktop_dist`'s successor; Android kernel without proot Python; PyInstaller and `install.sh` Python bootstrap retired | `tests/desktop`, `tests/desktop_dist`, `tests/installer`, `tests/mobile`, `desktop_node_v1` and `desktop_bridge_v3` schemas |
| **7. Endpoint** | Generators as a Rust `xtask`; Python absent from the core install; uv-managed runtime provisioned lazily; document-worker capabilities ported as their Rust golden-file parity passes | Golden-file corpora for pptx, docx and PDF extraction; `tests/slides`, `tests/pdf_parser`, `tests/doc_parser`; full e2e |

### Progress

**The phase gate is executable, and every phase is measurable.**
`scripts/rust_phase_gate.py` (with `make rust-gate` and
`tests/meta/test_rust_phase_gate.py`) turns the table above into numbers: it
reads `scripts/gen_test_classification.py`'s manifest and reports, per phase,
the declared directories, files, tests, black-box versus white-box counts and
per-surface counts. `--check` fails on a declared path that does not exist or a
phase with no black-box file. The Phase 5 row now names one concrete directory
set per owner in its dependency order; `packages/tofu-search/tests` is measured
by the same classification rules even though the generator scans `tests/`
only, so every phase is measurable.

| Phase | Files | Tests | Black-box | White-box |
|---|---:|---:|---|---|
| 1 Rust front door | 259 | 864 | 133f/375t | 126f/489t |
| 2 Manager and lifecycle | 176 | 1,647 | 43f/606t | 133f/1,041t |
| 3 Storage clients and SQL authority | 324 | 3,052 | 50f/712t | 274f/2,340t |
| 4 Execution engine | 940 | 8,965 | 33f/315t | 907f/8,650t |
| 5 Domains, one owner at a time | 1,582 | 15,061 | 157f/1,784t | 1,425f/13,277t |
| 6 Node and distribution | 108 | 1,233 | 25f/317t | 83f/916t |
| 7 Endpoint | 45 | 416 | 9f/78t | 36f/338t |

"Black-box" here is the manifest's signal classification. An audit of the six
Phase-1 directories shows the executable subset is smaller still: 56 test
files, 777 collected tests, of which 25 files (134 functions) ever speak HTTP
and 31 files (70% of functions) never touch a server — `tests/sse` is nine unit
files over the SSE parser, `tests/websocket` parses source with `ast`, and
tests in `static_serving`, `auth` and `identity` assert on `lib.*` objects.
Only `tests/e2e` is genuinely black-box (19 files over `live_server`).
So Phase 1's real gate is a mix: a URL-targeted subset plus white-box cases that
must be rewritten as Rust tests from the same cases, per the phased plan.

**Retargeting seam: `live_server` retargets, `flask_client` never proxies.** The
largest genuinely-HTTP pool is the 87 files / 251 tests that take
`live_server`, 65 of them free of `lib`/`routes`/`server` imports. A proxy behind
`flask_client` (110 files nominally) is rejected: the code under test would run
in another process than the test's monkeypatches, so 21 monkeypatching files
would pass while asserting nothing.

The seam is landed on the existing selector: with `TOFU_BLACKBOX_URL` set,
`live_server` yields that URL and nothing local is constructed — `flask_app` is
a stub that refuses attribute access (a test reading in-process state fails by
name instead of measuring this checkout), the checkout frontend-graph
prerequisite no longer applies, and the `page` teardown refuses a non-loopback
target because it DELETEs the conversations the test created there. Verified:
`tests/api/test_api_v4_contract.py` and
`tests/server_boot/test_instance_lock_reclaim.py` pass identically with and
without the selector (42 each), and the boundary is pinned structurally in
`tests/meta/test_live_server_external_target.py`.

**Three defects fixed while measuring this.**
- A checkout's `.env` names `PORT=15000`; the app's boot loads it after
  `tests/conftest.py` scrubs the identity, so the port returned and the
  production guard refused every child the booted server started. The isolated
  storage-engine manifest parser then failed with
  `EnginePreparationError ... (ProductionConnectionRefused)` and the documented
  in-process `pytest tests/blackbox` path could not run in the owner's own
  checkout. The dotenv load now restores the scrubbed lifecycle names
  (`tests/support/production_api_guard.py`, spec in
  `tests/meta/test_dotenv_lifecycle_scrub.py`); with a `.env` naming the port,
  29/29 black-box HTTP cases pass where 28 errored before.
- `scripts/blackbox_run.py` kept the `--` separator in pytest's argv, so
  `--server-url U -- -q` was read as a path and collected 0 tests.
- The push inbound bound was enforced only by the transport configuration:
  `build_hypercorn_config` set the 64 KiB limit, but a runner that builds its
  own config (including the `live_server` fixture) inherited Hypercorn's 16 MiB
  default and answered an oversized frame with a pong. The case passed only
  against a production-config server. `PUSH_MAX_MESSAGE_BYTES` now lives in
  `lib/agent_core/push.py` (already mirrored by
  `packages/tofu-front-rs/push/src/socket.rs`), the transport still configures
  from it, and `routes/push.py` refuses above it before parsing or dispatch, so
  every runner gets the contracted 1009 close. Same lane, same command: 11
  passed with the change, 1 failed / 10 passed with it stashed.

Baseline for the gate: `tests/blackbox` compares every one of the 16 declared
public static reads in `contracts/api_endpoint_index_v1.json` byte for byte
(plus the refusal and undeclared probes and the Vite asset closure), and
`tests/meta/test_blackbox_matrix_coverage.py` keeps that set complete as the
contract grows. Against a scratch server it passes 42 and skips 1 (the push
differential without a distinct reference).
`lib/project_mod/landing_verification.py` forwards the two target selectors
into the verification child, so
`TOFU_BLACKBOX_URL=... TOFU_BLACKBOX_REFERENCE_URL=... make test TESTS=tests/blackbox`
runs the gate through the normal path with no engine skips instead of falling
back to an in-process server.

**Phase 3b (SQL authority in Rust): SQLite candidate, not selected.**
`packages/tofu-sql-authority` serves `storage.v1` over the same SQLite
schema as `lib/storage_authority_sql`. No launch selects it: the configured
backend and the production child (`python -m lib.storage_authority_sql`)
are unchanged, and the binary runs only under a test project root
(`StorageSupervisor(sql_authority_command=...)`). The test fixtures alone
read `TOFU_TEST_SQL_AUTHORITY_BIN` and pass an explicit command. Product
supervisor and maintenance launches ignore that inherited variable;
`tests/storage/test_storage_sql_authority_seam.py` covers normal maintenance
with a deliberately nonexistent inherited candidate.

All 387 catalog operations are registered, including the latest Continue
admission/context and dispatch Turn-witness changes; the Rust catalog test requires
complete registration, not semantic equivalence. Registration and skipped
cases never certify operation parity. The retained differential corpus compares
each exercised step and every durable table
against the Python authority, with isolated SQLite directories. Build and
oracle execute together on a Hope CPU node through
`scripts/remote_cargo.py --after-build`; no local compiler or live authority
is involved.

**Audit correction:** prior candidate `3a5393ea5b2e` let the supervisor
replace the Python oracle's absent explicit command with the inherited Rust
candidate. Historical differential counts through Hope `69462936` are
**not Python-vs-Rust equivalence evidence**. The replacement gate explicitly
selects both commands and checks live `/proc` executable paths and command
lines. Hope `69467910` retained PID/start and executable SHA-256 observations;
it did not assert equality to the post-build SHA or enforce generation
continuity. Negative specs reject collapsed identities and falsified configured
arguments.

Fresh exact-source Hope run `69467910` (after reconciling shared WAL initialization fix):

- oracle/wire/replay/launch/seam and harness regressions: **254 passed,
  0 failed, 0 skipped, 0 errors**. Of these, 201 differential scripts have
  started/executed pair receipts and exercise **385 distinct catalog operations**.
  `tofusql.query` and `turn.resume.plan.read` are not in those script sequences;
  registration does not fill that gap or establish all-input equivalence;
- contract files: **321 passed, 0 failed, 104 skipped, 0 errors**. Only
  **19 passes** carry an explicitly selected fresh-Rust child receipt; remaining
  Python/direct tests and all skipped tests are not Rust parity evidence;
- original large-turn reader case: **24 passed, 0 failed, 0 skipped, 0 errors**,
  each with a fresh-Rust child receipt, at eight-worker concurrency. The unchanged
  five-second watchdog did not fire; the old debug timeout's cause is unproven.

The runner emits JUnit properties as 703 per-case JSON receipts into the retained
remote log. Published evidence artifact `6c31c5f3-eb78-4444-86b0-68f537ba7d93`
(`p3b-audit-69467910.txt`, SHA-256
`64bf6b6619cb4b6b193c35229a64b82119def0a4517dc1941882b8268fb38a93`)
contains the exact-source/build identity and every pass/skip outcome. Normal
maintenance and process-boundary regression files separately passed **47/47**.

Two test sequencing defects were reproduced and repaired without removing
assertions or changing product deadlines. The test-only torn-tail amendment
remains its separate commit `4dd620439b`: writes now follow initial
snapshot publication, and the differential waits for both transactional search
projection outboxes to drain before shutdown and full-table comparison. No
outbox table is excluded, and the existing pytest timeout bounds stalled drain.

The wider `run_rust_sql_domain.sh` lane (`69450785`) is **not green**:
**924 passed, 2 failed, 502 skipped, 121 setup errors**. The remote image's
old Git rejects `git init -b main`, reproduced in checkout/integration
fixture setup. Other failing blocks were lost in the producer's truncated
large output; they are not classified as environmental without evidence.
The runner now requests short tracebacks and appends a compact JUnit
non-pass summary so future runs retain failing identities. Native-engine
and PostgreSQL prerequisites explain many skips, not a Rust SQL pass.

Historical `69467910` release executable SHA-256:
`2af16bb017cc73b98c2cc952b911ecd68237aba67fe1167e9ac3bcf66ca1a825`.
Source snapshot SHA-256 for the identity-checked run:
`6ebb6d3eee2cd0034df1b98e39fe03f0264354d5e807ad0c787b260943b845f7`.
These are snapshot/build evidence, not deployment or live-runtime claims.

**Bounded follow-up after the `818ed811e2` review:** Hope `69470953` passed
**58/58**, with no skips or errors, on clean commit
`ccd9b140a2f01096b170587ae3d9ab57b60612d8`, tree
`9eaa66d403a6c858a21c6c2c053a04c58a0c6bf8`, archive SHA-256
`be0a37702965551f32b512831dda6dcb174c8d13673765521b419773c5e16eea`,
and fresh release executable SHA-256
`2af16bb017cc73b98c2cc952b911ecd68237aba67fe1167e9ac3bcf66ca1a825`.
The [changed-slice runner](../tests/storage_authority_sql/run_rust_sql_changed.sh)
requires that provenance and binds the live Rust executable bytes to the
post-build digest. Its retained log (`7baaaf43-0ddc-437a-87b2-3780c32b1d52`,
SHA-256 `c2428479f8b25fe86fa0bb36a42cf46cca26ef24f461477c42274f5e986ca598`)
contains all 58 case outcomes, including 40 paired child receipts. Ordinary
operations hold both generations. In the SIGKILL witness only Python changes
first, then only Rust; both generations and executable identities hold through
each replay. Seven fault specs failed before enforcement and passed after:
wrong digest, unexpected PID/start changes (including after replay), absent
intended restart, and same-path byte substitution after replay.

The two new raw-wire witnesses check exact SQL `database_unavailable` for
native-only `tofusql.query` and owned/stale/unavailable/invalid
`turn.resume.plan.read` responses. Exact SQLite logical snapshots stay unchanged
within each authority. These are two bounded operation witnesses, not a rerun
of the historical 385-operation corpus on this tree or a full P3b pass. The
wider domain failures, PostgreSQL implementation/parity, migration and runtime
promotion remain outside this evidence. Python still owns ordinary SQL.

Locked remote Cargo test (`69462285`): **126 passed, 0 failed, 0 ignored**;
locked all-target clippy with `-D warnings` (`69462490`) passed. Formatting
is checked locally. The implementation has no Python authority fallback.
The conversation search projection (`src/search/`) runs as Python's does: a
separate reconstructible SQLite under `data/projections/`, drained by one
background worker that is joined on close. The process-local turn
projection cache and the attempt-event write metrics are ported too; the
cache only accelerates reconstructible data.

| Plan item | Delivered as | Gate |
|---|---|---|
| Wire and process contract | `src/server.rs`, `src/main.rs`: 4-byte framing, inline auth, deadlines, keep-alive, half-close then wait for EOF, the classified error envelope, worker/conversation commit watches, `native_checkpoint.*` admission (capability reported unavailable, as Python does when it is not configured), project lease, stdout `storage.ready`/`storage.error`, stdin/PPID ownership | `tests/storage_authority_sql/test_rust_sql_authority_wire.py`: 27 frame cases, 4 malformed/oversized frames, a torn frame, keep-alive, unauthenticated keep-alive, receipt replay and command-id reuse, and SIGKILL after an acknowledged commit. Every response is compared as a whole JSON value against the Python authority |
| Transactions and receipts | `src/backend.rs`, `src/receipt.rs`: one ordered writer in `BEGIN IMMEDIATE`, a per-transaction watchdog, rollback before classification, the bounded busy retry, receipts written in the same transaction (v2 key, legacy lookup, zlib above 64 KiB through system zlib `compress2`, byte-identical to CPython) | Wire gate above; `zlib::tests` pins the CPython digest |
| Schema | `contracts/sql_authority_schema_v1.json`, rendered from `schema.py` by `scripts/gen_sql_authority_schema.py --check`; `src/schema.rs` replays `initialize_schema`, migrations included | The differential gate compares the full `sqlite_master` text of both authorities |
| Filesystem preflight | `src/mount.rs::filesystem_preflight` ports `run_filesystem_preflight`: it refuses startup without free space, fsync, atomic replace or enforced `flock` exclusion, and reports latency without refusing on it. Fastpath admission uses an explicit measured decision; required mode refuses when its admission cannot be proved | `test_health_preflight_and_project_local_files` |
| Backup and baseline | `src/backup.rs` and `backup/policy.rs`: online SQLite copy, integrity verification, SHA-256 and durable manifest publication, deadline cleanup, retention, stale-job reclamation and recovery-copy accounting. Fastpath backups pin an immutable checkpointed generation and publish a hardlink; budget rotation never deletes an old verified point before new publication | Retained Python maintenance CLI/restore tests and `test_backup_is_verified_and_kept_inside_project`; Rust fault and retention specifications |
| Opt-in fastpath | `src/fastpath/`: real front selection, sole checkpoint owner, durable immutable snapshot generations, frame-aligned incremental WAL shipping, lineage-fenced surviving-front and local-loss recovery, notified shipper and joined close | Both retained supervisor fastpath cases; typed EIO injection checks every armed fault is consumed and the previous recovery point survives |
| Logical outbox | `src/logical_outbox/`: semantic SQL mutation capture and encrypted outbox insert in the existing transaction, authenticated Fernet envelope, durable shadow framing, fsync-before-ACK publication and ordered/idempotent ACKs. The publisher is joined before backend close | Python `LogicalCommitShadow`, `decode_logical_payload` and `BackendReplayTarget` read/decrypt/replay Rust output; Rust tests pin exact Python cryptography token bytes, rollback, corrupt/torn tails and exclusive sink ownership |
| Operations | One `src/ops/<domain>.rs` per `operation_domains/<domain>.py`; SQL is verbatim. `catalog_tests` pins every registration's kind, receipt and timeout to `storage_operations_v1.json`; `tofu-sql-authority unported` lists what is missing | `tests/storage_authority_sql/test_rust_sql_authority_differential.py` runs every script of the Tofu-DB differential corpus against both SQL authorities in isolated data directories, with deterministic command IDs. Per step it requires an equal value, or an equal error code and message. Afterwards it requires equal durable state: every table, with authority-clock values and generated identities tokenized per side |

Decisions:

- **A separate crate, not a module of `tofu-db`.** `packages/tofu-db/AGENTS.md`
  forbids linking SQLite outside the read-only TofuSQL front, and any edit
  to `tofu-db` changes the engine fingerprint the running install certifies.
  The plan's "SQL mode of `tofu db serve`" becomes a subcommand when the
  single `tofu` binary is composed.
  Alternatives considered: a `sql` module inside `tofu-db`, rejected for both
  reasons above; a Rust frontend over Python handlers, rejected because it
  keeps Python as the authority.
- **Shared protocol types with P3a.** `packages/tofu-storage-client` (P3a)
  and this crate share the error taxonomy, the `StorageError::to_payload`
  wire shape and the authored catalog. `test_rust_sql_authority_taxonomy.py`
  pins `error.rs::ERROR_CODES` to `lib/agent_core/storage_errors.py`, the
  owner both sides mirror. Problem: depending on `tofu-storage-client` would
  link the whole `tofu-db` engine, blake3 and the Fernet stack into the SQL
  authority. Its `StorageError` also carries client-only state (transient
  classification, an unsigned retry hint). storage.v1 framing differs from
  the `tofu_db::protocol` framing, so no frame type is shared.
  Alternatives considered: a dependency on the client, rejected for the build
  weight above; a new tiny crate holding the taxonomy and catalog loader,
  deferred until the single `tofu` binary composes both.
- **orjson byte compatibility.** Request digests, receipts and stored
  documents are orjson bytes, so `src/json.rs` reproduces orjson's float
  notation and sorted-key layout instead of using serde_json's default text.

Remaining scope limits (not certified by catalog parity):

- The PostgreSQL adapter and connection-file/co-container launch mode remain
  unported. The supervisor's Rust test override applies only to SQLite;
  PostgreSQL fixture results cannot be counted as Rust evidence.
- Configured native-checkpoint control remains unported: pinned image/scope,
  atomic private checkpoint transactions and joined checkpoint barriers are
  not certified. Unconfigured capability envelopes match Python; an explicit
  `TOFU_STORAGE_NATIVE_CHECKPOINT` now refuses startup instead of being
  silently ignored. `test_rust_sql_authority_launch_limits.py` proves only
  fail-closed safety, not parity for these Python-supported launch APIs.
- Huge fastpath seed/image copies lack Python's resumable checkpoints,
  startup progress/heartbeat and zero-copy classic seeding. Immutable image
  copying currently holds the writer lock. Cross-implementation crash-time
  offline retirement has not been certified.
- The WAL-coalescing VFS and multi-job writer batching remain unported;
  individual command durability and atomicity are preserved. The logical
  publisher currently supports a POSIX filesystem sink only; a kernel-stalled
  fsync cannot be forcibly completed by joined thread shutdown.

No backend configuration, live data or live process changed. Python remains
in service; this change neither promotes the candidate nor retires Python.

**Phase 0 (contracts first): delivered 2026-11-18.** No product behaviour
changed; the Python server still serves every route and emits every event.

| Plan item | Delivered as | Gate |
|---|---|---|
| `api_v1.yaml` for the 764 endpoints | `contracts/api_v1.yaml`: OpenAPI 3.1, 757 published operations exactly as `GET /api/openapi.json` serves them, plus 7 registered routes outside that document (`x-tofu-unpublished-routes`); each carries an `x-tofu-route` record (framework rule, transport, owner, handler, scope/public metadata). Extracted from route assembly, `@api_meta`, blueprint response contracts and the native v4 / Conversation Sync documents. `api_endpoint_index_v1.json` is now derived from it. | `scripts/gen_api_v1_contract.py --check` fails, printing the fragment the code implies, when a route/method/record/operation exists on one side only; `tests/api/test_api_v1_contract.py` (includes "served document == contract minus route records"), `tests/api/test_api_endpoint_index.py` |
| `events_v1.json` | `contracts/events_v1.json`: 71 events (all field-contracted), 26 phases, 7 push frames, envelopes, kind vocabulary, read policies. `lib/agent_core/events.py` stays the emission gate and is checked against it. | `scripts/gen_event_contract.py --check` (both directions, field level); `tests/events/test_events_v1_contract.py` |
| Storage operation catalog authored as JSON | `contracts/storage_operations_v1.json` is no longer written by its generator; the sidecar registry is checked against it (`operationsSha256` and `operationCount` verified); Rust and Python clients render from it. Its key `generatedBy` is kept because the file is an external input of the Tofu-DB engine fingerprint. | `scripts/gen_storage_operation_catalog.py --check`; `tests/tofudb/test_storage_operation_catalog_authority.py`, `tests/tofudb/test_tofudb_pre_authority.py` |
| Generators emit Rust, TS (and Python) with `--check` | New crate `packages/tofu-contracts` (standalone, no runtime consumer): `generated_api_v1.rs` (operation table for all 764 operations; serde types for request, response and error bodies; mapping rules in `scripts/api_v1_rust.py`) and `generated_events_v1.rs` (tagged event and push-frame enums, metadata). The TypeScript event mirror renders from `events_v1.json`. All in `make contracts-check`; the crate is in `check_static_gates.py` `RUST_GATED_PACKAGES`. | `cargo test` (fixture round trips for every event, push frame and Conversation Sync body; operation table == endpoint index), `cargo clippy --all-targets -D warnings`, `cargo fmt --check` with `resolve_cargo` |
| Black-box harness for any server URL | `tests/blackbox/` (`blackbox_url`: `TOFU_BLACKBOX_URL` or the in-repo `live_server`), `scripts/blackbox_run.py --server-url URL`; standard-library HTTP/WebSocket only, contract files only | Passes against the in-process Python server and against a separately started scratch server by URL |
| Test classification manifest | Derived on demand by `scripts/gen_test_classification.py` (rules in its header; stdout, `--output PATH`, or `build_manifest()`), never committed (see "The test classification manifest is derived on demand" below). At delivery: 4,895 files, about 42,940 tests; black-box 577 files / 5,683 tests, white-box 4,318 / 37,257. `parityCases` maps every row of the table below to the tests that pin it (`scripts/test_classification_parity_cases.json`); four rows record a partial-coverage `gap` | `make contracts-check` runs `gen_test_classification.py --check`; `tests/meta/test_test_classification_manifest.py`; a table row without a mapping fails generation |

Not changed in Phase 0, on purpose: `@api_meta` and `lib/agent_core/events.py`
still carry the Python copies of the metadata (validated, not deleted; they go
with their owners in Phases 1–5), and no CPU-sized pool was introduced
anywhere (open question under "Async runtime").

**Phase 3a (Rust storage.v2 client): implemented 2026-11-18, not yet wired
in.** The owner's model-routing keys are now read in Rust (read-only port)
and `command()` fails closed without a redactor.
`packages/tofu-storage-client` is the one client for P1c/P1d, P4 and P5;
coverage, differential evidence and decisions are in
[Phase 3a: Rust storage.v2 client](#phase-3a-rust-storagev2-client).

**Phase 3b (Rust SQL authority): candidate evidence only; not on this tree.**
The SQL `storage.v1` supervisor and online maintenance still launch the
Python authority. The scratch launch regression checks that an inherited
`TOFU_TEST_SQL_AUTHORITY_BIN` cannot select a different executable, including
checking the live Python executable and command line on Linux. This tree has
no explicit replacement-command seam. Keep candidate selection in the held
parity fixtures, with an explicit command per oracle and live executable,
command-line, digest and process-generation checks; do not infer identity from
configuration. Earlier differential counts through Hope `69462936` are not
Python-vs-Rust evidence: inherited selection could launch Rust on both sides.

The corrected candidate record (`14a3988974`, Hope `69467910`, retained receipt
artifact `6c31c5f3-eb78-4444-86b0-68f537ba7d93`) reports **385 distinct paired
operations** across 201 differential scripts, not parity for all 387 registered
operations. Neither `tofusql.query` nor `turn.resume.plan.read` was in the paired
scripts; an independent resume test or registration does not supply paired resume
evidence. The recorded source snapshot SHA-256 identifies the tested archive,
but the retained record does not attest that its bytes match an archive of the
exact proposed Git tree. This is historical candidate evidence, not verification
of this checkpoint, `818ed811e2`, or any deployment or backend cutover.

**Phase 1 next (Rust front door).** Needs: an axum binary consuming
`tofu-contracts` (operation table for routing and reverse-proxy decisions,
generated SSE/push types); the black-box harness and the `tests/sse`,
`tests/websocket`, `tests/static_serving`, `tests/auth`, `tests/identity`
subsets run with `TOFU_BLACKBOX_URL` against it (the manifest's
`fixture:flask_client` signals mark the black-box files that still drive the
app in-process and must first be pointed at a URL). **Blocker:** the tokio
runtime's worker count. Its default is CPU-derived; whether that is compatible
with "Hardware never limits work" is the owner's open question, and until he
answers no CPU-sized pool may enter product code, so the front door cannot be
merged into the running product before that decision (a fixed constant
declared in `contracts/resource_policy_v1.json` is the alternative). Also
open: the parity gaps recorded in the manifest.

**Phase 7, generator part: started 2026-11-18.** `packages/tofu-xtask`
(standalone crate, binary `tofu-xtask`; `serde_json`, `serde_yaml`) renders
every artefact the three Phase 0 contracts derive. `make contracts` writes,
`make contracts-check` (CI lint job) fails on a stale output:

| Contract | Rust | TypeScript | Python / JSON |
|---|---|---|---|
| `contracts/api_v1.yaml` | `packages/tofu-contracts/src/generated_api_v1.rs` | — | `contracts/api_endpoint_index_v1.json` |
| `contracts/events_v1.json` | `packages/tofu-contracts/src/generated_events_v1.rs` | `frontend/src/api/event-contract.generated.ts` | — |
| `contracts/storage_operations_v1.json` | `packages/tofu-db/src/generated_storage_operations.rs` | — | `scripts/generated_storage_operations.py`, `tofu_db/generated_contract.py` |

The port was accepted by byte identity: on the committed contracts the Rust
renderers reproduced all seven Python-rendered outputs exactly except their
"generated by" headers, and again on a contract extended with synthetic
recursive, nullable, `anyOf`/`allOf`, enum, keyword-named and pointer-referenced
schemas. The Python renderers (`scripts/api_v1_rust.py`,
`scripts/tofu_contracts_rust.py`, the render halves of `gen_api_v1_contract.py`,
`gen_event_contract.py`, `gen_storage_operation_catalog.py`) are deleted; those
scripts now only check the Python owners still implementing each contract
(route assembly, event registry, sidecar registry) and go when their owners move
(Phases 1–5). The storage outputs keep their frozen header naming
`gen_storage_operation_catalog.py`: their bytes are part of the storage
contract identity the engine shares (`storage_contract_sha256`), so a header
edit would demand a new engine build. One latent defect was fixed in the port:
a field whose struct type reaches itself is boxed once, where the Python
renderer nested `Box<Box<…>>` (no cycle exists in the committed contract, so no
output changed). Still in Python: the other contract generators
(`gen_conversation_sync_contract.py`, `gen_api_v4_contract.py`,
`gen_api_v1_model_routing_contract.py`, `gen_tofudb_ir_contract.py`,
`gen_storage_v2_contract.py`, the `native_*` ABI generators, …); each moves
the same way, output for output.

**The test classification manifest is derived on demand.** *Problem:* the
committed `tests/parity/test_classification_v1.json` was a function of every
test file (counts, signals, the file list), so any test change made it stale and
broke `tests/meta` for every later writer; it was stale again on the branch
when this was measured. *Decision:* nothing is committed;
`gen_test_classification.py` builds the manifest whenever it is read (about
18 s on this host), and `make contracts-check` plus
`tests/meta/test_test_classification_manifest.py` fail when it cannot be built
(unmapped or edited parity row, missing referenced test, unparsable file).
*Alternatives considered:* a `git_land` regeneration family keeps a committed
copy fresh but rebuilds on nearly every landing (most touch `tests/`), adding
about 11 s to read `tests/` from the shared volume plus the 18 s build, and the
generator's owner rule also reads `lib/` directories outside such a family's
snapshot; a CI-only staleness check (the old `tests/meta` freshness test) did
not prevent the drift, because landings run no tests.

**Phase 1e (`WS /api/push` in Rust): standalone socket library; runtime
qualification remains separate.** The current tracked `packages/tofu-front-rs/`
contains only `push/`, not the parent front-door host/manifest. The historical
checkpoint `283b1a0b39fb` and the gate descriptions below do not establish
current canonical routing or executable provenance. No approved front-door
executable path, digest and exact-source attestation is recorded here;
`/tmp/tofu-front-rs-target` is explicitly owner-unprovable in
[`RUST_BUILDS.md`](RUST_BUILDS.md#lifecycle-of-local-targets-that-remain), not
an admissible runtime artifact. Qualification requires a source-linked approved
host binary and actual paired execution; neither source review nor the Python
bridge fixture qualifies Rust.

The in-memory paired case
`tests/agent_core/test_push_front_door.py::test_notify_reconnect_requires_resubscription_without_replay`
compares a local `PushClient` with the existing synthetic bridge session:
`folders_changed` publications while disconnected or before resubscription are
not replayed; fresh publications after the mirror acknowledgement retain their
order and exact local-socket bytes. A FIFO sender acknowledgement after the
fresh frames fences the bridge output before checking for trailing notifications;
the local client must also have no active or queued event. This is Python-side
fixture support only:
no Rust socket, host process, TCP reconnect, restart or durable replay is
exercised. It does not replace the Rust socket or URL differential gates.

**Current tracked ownership.** `routes/push.py::push_ws` constructs the Python
`PushClient` and control-RPC session for `/api/push`; the bridge endpoint delegates
admission to the same Python policy. The standalone library
`packages/tofu-front-rs/push` (`tofu-front-push`) provides a socket implementation,
not a tracked executable host. This inventory does not establish deployed traffic
ownership. A cached `/tmp/tofu-front-rs-target/debug/tofu-front` executable was
reported by independent review, but its approval, digest-to-source correspondence
and applicable paired execution remain unknown; its existence is not qualification.

**Historical, unqualified P1e record (`283b1a0b39fb`).** The following describes
the reported scratch-host integration, not current canonical ownership or a
current pass. That host reportedly depended on `push/` by path and excluded it
from its parent workspace. The reported socket behavior was `PushHub`
semantics: `(channel, taskId)` and `taskId: "*"` subscriptions narrowed to
the frame owner, control lane (pong) before the RPC lane before the FIFO
event backlog, per-stream latest-value replacement fenced by any other frame
of the stream, a full backlog (`TOFU_PUSH_EVENT_QUEUE_CAPACITY` envelope)
disconnecting the socket, the 30 s keepalive ping with lease heartbeat, the
180 s receive-idle release, 1001 for every server-side release (backlog,
idle, malformed JSON, lost bridge, shutdown), 1009 above Hypercorn's 64 KiB
message limit, and Python's exact bytes for Rust-built frames
(`python_json.rs`: Quart `send_json` sorting, ASCII escapes and number
round trip). Python owners keep publishing through `hub.push_event`: the
front door holds one bridge WebSocket to `GET /api/push/front-door-bridge`
(`tofu.push-bridge/v1`, HMAC of the Flask secret, loopback only;
`lib/agent_core/push_front_door.py`). Over it Python decides each handshake
with the one policy `routes/push.py::_decide_push_handshake` shared with its
own `push_ws`, holds a mirror subscriber per owner in `PushHub` (so
`has_subscriber`, the lease registry and latest-value hand-off run unchanged)
and forwards each selected publication as the exact text a local socket
sends; a first subscription is acknowledged before the socket reads its next
frame, keeping Python's "subscribed before the next reply" ordering. Aborts,
build probes and JSON-RPC frames go to Python too: the RPC lane is a trait
(`rpc.rs`) whose default relays to a per-socket `ControlRpcSession` until
P1f's Rust session replaces it. The link is demand-driven (no reconnect
timer): a lost link releases its sockets with 1001 and the next browser
handshake reopens it; no Python answers 502, a closing one 503. Runs on the
front door's runtime (`TOFU_FRONT_DOOR_SCHEDULER_THREADS`, `fixed`) and
sizes no pool.

The host runtime and routing descriptions above are historical claims. No
current Rust pass is established without an approved executable path, SHA-256,
exact source identity and applicable paired execution receipt.

| Historical gate | Reported evidence (unqualified for the current tree) |
|---|---|
| Rust behaviour | Reported `cargo test` in the crate: hub, lanes, Python `json.dumps` bytes, and socket cases against a scripted bridge over real TCP (admission statuses, routing, owner filter, pong bytes, RPC priority, keepalive, idle 1001, 1009, bridge loss, shutdown 503, mirror ordering, contracted push-frame fixtures) |
| Python bridge | `tests/agent_core/test_push_front_door.py` (current Python-only fixture coverage is distinguished above) |
| Black box | `tests/blackbox/test_blackbox_push.py` (exact pong/RPC bytes, inert frames, published `folders_changed`, close codes, subprotocol, cross-site 401, keepalive) reportedly passed against Python and a scratch front door by URL; no executable/source pair is attested here |
| Differential | Historical `scripts/blackbox_run.py --server-url <front door> --reference-url <python>` reportedly ran `test_blackbox_push_differential.py`: one scripted session against both, every frame, status and close code compared byte for byte, no normalisation |

The historical integration deliberately retained Python auth, abort, build-id
refresh and control-RPC execution over the bridge. Its reported difference: Python's `json.loads` accepts `NaN`/`Infinity`
literals in an inbound frame, the Rust socket releases such a frame as
malformed (no browser sends them).

**Phase 7, golden-document corpora: Python baseline delivered 2026-11-18**
(independent of Phase 0). Frozen input-to-output corpora cover PDF parsing,
DOCX/PPTX extraction, PPTX import, attachment preview, package QA, PPTD export
and HTML/PNG rendering. The reported 60 Python results, including private
cases, certify neither Rust execution nor a Rust document-worker port. The
implementation-neutral command protocol and unchanged tolerance authority
are described in [`GOLDEN_DOCUMENTS.md`](GOLDEN_DOCUMENTS.md). The baseline
checkpoint is `9b12bb9e2bcb6b64b0480dc9870e1332c6f39817`, delivered in
`c4f1fa0299a7dda81b648a226cddbfd272f0ddd5`; the runner, frozen fixtures,
tolerances and original golden tests remain unchanged from both revisions.

#### Bounded document-worker source map and local comparison bridge

| Boundary inspected | Concrete owner / gap |
|---|---|
| Chosen public synthetic operation | `synthetic-pptx-import-fidelity` / `slides.import_pptx`, parameters `{}`; project-owned committed input and expected tree in `tests/fixtures/golden_documents/manifest.json` |
| Python parse/serialization | `scripts/golden_documents.py::py_slides_import` calls `lib/slides/import_pptx.py::import_pptx` (python-pptx); `_deck_tree` parses the emitted PPTD YAML, retains media as byte blobs, and `run_python_operation` writes `result.json` |
| Matching Rust document owner | Not identified in the bounded `packages/**/*.rs` search for `import_pptx`, `pptd` and `pptx`. Matches are generated translation-route metadata and knowledge extension classification, not a PPTX-to-PPTD importer. No source-matched runnable producer for this operation is established. |
| Nearby Rust, not substitutes | `packages/tofu-node/src/commands/paper.rs` builds LaTeX papers and serializes build manifests; `packages/tofu-search/rust/src/lib.rs` exposes HTML/text/search kernels; `packages/tofu-xtask/src/main.rs` generates API/event/storage contracts. None implements this golden operation. |
| Existing implementation bridge | `scripts/golden_documents.py::run_operation` accepts `{op} {input} {out} {params}` and expects `<out>/result.json` plus referenced blobs. An absent command selects Python, not Rust. The corpus CLI selects cases, not an individual operation. |
| Narrow inert checker | `tests/golden_documents/check_rust_document_candidate.py` accepts only an explicitly supplied, already-produced output directory for the fixed operation. It verifies the selected public input/expected digests and reuses the original normalization/comparator/tolerances; it never runs a producer, loads private documents, or falls back to Python. |

**Missing ownership and bridge:** a Rust PPTX-to-PPTD importer matching the
Python import report, theme, page elements and copied media, followed by the
operation's JSON/blob serialization adapter and an approved executable/source
identity. A storage JSON serializer, contract generator, Python subprocess or
reference-copying stub is not that producer.

The inert checker rejects absent/malformed/non-finite output, reference-tree
aliases, escaping blob paths and extra blob metadata. Its executable
specification is `tests/golden_documents/test_rust_document_candidate.py`:
exact frozen-output copies are explicitly comparator self-tests, one-ULP
rounding of an existing point coordinate uses only the documented `1e-9`
number rule, and changed text/structure/types/layout/media or missing output
must fail. Fixture text is compared as inert data; no HTML, links, fonts or
browser are executed or fetched. No tolerance or reference is rewritten.

Evidence levels remain separate:

- **Syntax/build:** local Python checker syntax and unit contracts can be
  checked; no Rust compilation or Rust syntax/build result is asserted.
- **Synthetic same-implementation/comparator contract:** fixture comparisons
  establish checker acceptance/rejection only. They are not a fresh document
  parse, independently produced Rust output, or corpus parity run.
- **True cross-implementation match:** blocked on the missing Rust owner and
  attested producer above; zero such matches are established by this bridge.

The complete worker-port gate still requires actual Rust production for its
operations and the reviewed full corpus. Private/full-corpus qualification,
remote jobs, cargo, runtime selection and release readiness are outside this
local comparison bridge. The separate generator `xtask` is not a document
worker port.

#### Native read-only import tranche: source-only admission boundary

The accepted comparator checkpoint is
`9e3aa3f18b81338f9a9e97aa1b2015d76aa212bf`. It supplies a full-output
checker, not a native importer. The following is a concrete next-step scope,
not an implemented capability, build receipt or parity result. SQL and UI
work do not supply evidence for this tranche.

**Authoritative producer and types.** `lib/slides/import_pptx.py` owns
`ImportReport(dict)`, `_pt`, `_extract_theme` and `import_pptx`.
`scripts/golden_documents.py::py_slides_import` serializes
`{"report": {"pages", "theme", "skipped"}, "deck": ...}`: `_deck_tree`
reads emitted YAML with `safe_load` and represents copied media as byte blobs.
It does not construct `lib/slides/pptd.py::Deck` or `Page`. Those dataclasses
remain the authoring/parser authority; no parallel Rust PPTD schema is needed
for the proposed metadata projection.

**Proposed owner and exact limit.** Once the parser and build gates below are
met, a single private read-only import-metadata module in `tofu-runtime`
would own this native kernel; its name/API is not established yet. It reads
PPTX package bytes, resolves presentation-order slide relationships and the
producer's reachable `theme1.xml`, then projects only the following existing
JSON paths. It does not create a PPTD directory or claim `slides.import_pptx`
support. A test adapter, not a new public operation, owns serialization.

| Selected field | Required type and mapping | Exact synthetic value |
|---|---|---|
| `/report/pages` | integer selected-slide count | `3`; `1` for `[2]` |
| `/report/theme` | object, same mapped theme as manifest | identical to manifest theme |
| `/deck/deck.pptd/pages` | ordered string array, original 1-based numbering | `pages/01.page`, `pages/02.page`, `pages/03.page`; only `pages/02.page` for `[2]` |
| `/deck/deck.pptd/size` | two integers: `int(round(EMU/12700, 2))` | `[959, 540]` |
| `/deck/deck.pptd/title` | explicit nonempty title, otherwise input basename without extension | `pptx_import_fidelity` |
| `/deck/deck.pptd/version` | string | `v2` |
| `/deck/deck.pptd/theme` | existing colors/textStyles mapping, not raw OOXML theme | see producer manifest construction |

The exact fixture is `synthetic-pptx-import-fidelity`, committed input
`tests/fixtures/golden_documents/inputs/synthetic/pptx_import_fidelity.pptx`.
Use both manifest operation keys `slides.import_pptx` with `{}` and
`slides.import_pptx-slide-2` with `{"slide_numbers":[2]}`. Manifest SHA-256s:

- Input (44,529 bytes): `e3603612fa025a9d9649d6087f4771fb5c4bade4aa2b6e6c3a12837dc23284c9`.
- Full-import expected tree: `c33a9fff0a54d4c4fad43036dc808cb8a42a4d016b59968d9cc82fffcc6f38a7`.
- Slide-2 expected tree: `2562ff4126c2de3e9f203bee53c1f72dfec5869c5b33f5a02d4c9715a21b2d65`.

These identify frozen inputs/reference trees, never a native source fingerprint.
The synthetic builder is provenance, not a regeneration instruction.
Static archive inspection confirms `cx=12191695`, `cy=6858000` EMU and
three ordered slide references. Ignore the stale `type="screen4x3"` attribute.
`_pt` yields `[959.98, 540.0]`; manifest truncation yields `[959, 540]`, not
960-by-540. Both major/minor Latin families are `Calibri`, direct EA families
are empty; use the existing EA-then-Latin fallback, not script-specific fonts.
Mapped colors are `bg=#FFFFFF`, `ink=#000000`, `primary=#4F81BD`,
`accent=#C0504D`, with converter constants `muted=#8A93A3` and
`hairline=#D8D5CE`. Preserve the exact four `textStyles` from the producer,
including absent caption/bignum fontFamily fields. Raw palette extraction uses
`srgbClr/@val` or `sysClr/@lastClr`, not general OOXML color resolution.

**Ordering, omissions and determinism.** Selection follows
`{int(n) for n in slide_numbers}` and then presentation order, not caller,
relationship-map, ZIP-entry or lexical filename order. Duplicates collapse;
`[3,1,3]` selects original pages 1 and 3; empty/out-of-range selections yield
zero selected pages. Invalid integer conversion raises in Python. The golden
adapter writes sorted-key UTF-8 JSON with a final newline and unchanged strings;
its default `json.dumps` does not enforce finite numbers. Finite output is an
explicit candidate/gate requirement, not an existing serializer guarantee. No timestamps, absolute output paths or archive iteration
order may affect the metadata result. Repeated parses must be byte-identical.

Exclude page elements, notes, tables, charts, media, rich text/hyperlinks,
rendering, fonts/downloads and `/report/skipped` from this tranche. In
particular, never invent `skipped: []` to imply shapes were inspected.
The actual importer records unsupported charts, MEDIA/GROUP and caught shape
errors as `{"slide": integer, "shapes": [strings]}` and page notes prefixed
`未导入元素: `. Theme extraction failures are logger warnings, not skipped
entries. Its header promises broader diagnostics than the implemented branches;
there is no comprehensive catch-all for every unhandled shape kind. Preserve
these distinctions rather than asserting the header is implemented. This
fixture has only empty skipped lists, so warning equivalence is untested.
Unimplemented package/theme forms must remain explicitly unsupported by the
candidate, not silently become a successful full import. Malformed-package
handling must be checked against the Python boundary before widening support.

**Supported dependencies and parser gap.** Root `Cargo.toml` owns the runtime
workspace (edition 2021, MSRV 1.85); `packages/tofu-runtime/src/lib.rs` has no
PPTX/PPTD/OPC reader. Its direct output dependencies are `serde =1.0.229`
(`derive`) and `serde_json =1.0.151` (`float_roundtrip`, `raw_value`; default
runtime feature adds `preserve_order`). Root `Cargo.lock` resolves transitive
`flate2 1.1.10` through HTTP decompression, not a ZIP package reader.
No supported ZIP/XML crate boundary is established by the tracked manifests,
locks and Rust source search (`zip`, `quick-xml`, `roxmltree`, PPTX/PPTD and
ZIP APIs). This is bounded source evidence, not proof that every possible
parser mechanism is absent or that any dependency is cached offline.

The private `tofu-node::commands::adapter::extract_zip` is an executable
installer, selecting/staging a binary rather than returning arbitrary package
parts. Node has its own lock and Rust 1.89 requirement; copying or repurposing
it is not a supported read-only document boundary. The HTML tokenizer discards
attribute values and lowercases tags: it is not an XML parser. Python's existing
`lib/slides/pptx_qa.py::inspect_pptx_package` uses `zipfile` and `defusedxml`,
with relationship validation and no external-target fetch; it is QA, not a
native import implementation. Python declarations (`python-pptx>=0.6.21`,
`lxml>=5.3`, `PyYAML>=6.0`, `defusedxml>=0.7.1`) are lower bounds, not evidence
of exact installed versions. **Stop before executable Rust until a supported
read-only package/XML boundary and its offline dependency availability exist.**

**No-job availability inspection.** Source inspection of
`scripts/remote_cargo.py::main`, `run` and `post_build_steps`, and
[`RUST_BUILDS.md`](RUST_BUILDS.md), establishes a documented off-production
mechanism, not its current availability. There is no supported availability-only,
`--preflight` or `--dry-run` interface: unknown flags become Cargo arguments,
and normal execution prepares/submits a job. Do not invoke a guessed probe.
`--clean-source` plus `--after-build` can bind immutable commit/tree, verified
archive SHA-256 and freshly built executable SHA-256 during an authorized run;
that is not a pre-existing source-matched importer receipt. No wrapper,
compiler, account/queue/network probe or job was run for this investigation.
Sanctioned independent execution, offline compiler/dependency artifacts, oracle
runtime and exact source/binary identity remain **unknown**. No executable
candidate is admitted, and no Rust build/test/pass is claimed.

**Differential command and acceptance gap.** A future authorized isolated gate
can obtain each Python reference through the existing command (example for the
main operation; use `'{"slide_numbers":[2]}'` and a separate output for slide 2):

```sh
PYTHONPATH=$PWD .venv/bin/python3 scripts/golden_documents.py python-impl \
  slides.import_pptx \
  tests/fixtures/golden_documents/inputs/synthetic/pptx_import_fidelity.pptx \
  "$PYTHON_OUT" '{}'
```

Python producer recomputation is **not verified**: this command was not executed.
The normal corpus pytest setup is not isolated before producer imports: it
imports runtime/extension owners, and its network guard protects production
ports rather than denying all networking. The bounded gate therefore uses only
the exact tracked golden operations and temporary bad-output controls; it does
not open actual/private inputs or run an importer. The native command cannot be
named honestly until its owner/executable exists. Its future test adapter must
accept the same operation/input/out/params protocol and explicitly qualify
**only the selected projection**, never pad it with reference JSON. The missing
projection differential must first verify both manifest input/expected-tree
digests with `availability` for exactly these two operations, then select the
paths in the table from fresh Python output and frozen output, validate native
field types/key sets, and compare native-to-Python and Python-to-frozen via
`golden_documents.compare_value` with unchanged `slides.import_pptx` tolerance
(`1e-9` numeric; exact strings/keys/arrays/booleans). Assert separately that
page count equals selected page-path count and the two theme objects agree.
The native process must not read expected/reference trees. Missing native
output, unknown fields, malformed/nonfinite values and unsupported input must
not be reported as a matching subset. The test-only adapter now exists as
`tests/golden_documents/pptx_metadata_projection.py::check_metadata`.
It accepts only these two operation keys and already-produced local directories,
verifies both frozen expected-tree digests and the input digest before reading
candidate JSON, and rejects missing/unknown projected fields, wrong exact types,
duplicate JSON keys and nonfinite values before using the unchanged comparator
and tolerance list. The candidate must contain exactly the projected structure;
full imports are projected only on the reference/Python side. The optional
`python_dir` is independently checked against frozen output before candidate
comparison; supplying a directory is not evidence of producer execution.
There is no new runtime operation or producer CLI.

The following is the supported isolated frozen-control invocation. It runs no
Python/Rust producer and proves no source identity. Its temporary projected
reference copies are checker controls, not native output fixtures or receipts.

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m unittest tests.golden_documents.test_pptx_metadata_projection
```

The controls cover both operations' pinned original page selection, dimensions,
counts and metadata; missing/unknown fields, wrong order/renumbering, partial or
bogus output, exact int/float/bool distinctions and nonfinite values fail. A
rounding control retains the existing numeric tolerance. A separate control
shows the unchanged full-import checker still refuses an honest metadata subset.
Excluded slide features and `/report/skipped` are deliberately not compared;
this is not empty diagnostics or warning parity. No tracked fixture is duplicated
or regenerated. **Trusted native producer receipts and comparative native
validations remain zero.** These controls do not replace source-attested
production, negative-package tests or repeated-producer determinism checks.

Before qualification, the gate also needs repeated-run byte determinism,
selection order/duplicates/empty selection, explicit nonempty title override
and empty-title basename fallback, fractional/tie rounding, EA/Latin fallback
and missing/malformed theme tests using benign synthetic fixtures.
Warning-producing GROUP/MEDIA/unsupported-chart and malformed relationship
fixtures are required before any warning/full-import claim; the two frozen
operations alone do not cover them. Exact int/float types need explicit checks:
the shared comparator deliberately compares numeric values across those types.

The existing inert command remains:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m tests.golden_documents.check_rust_document_candidate \
  --candidate-dir "$FULL_IMPORT_OUT"
```

It compares the **complete** main import tree, including three pages, native
tables/charts and `media/slide03_03.png`; slide 2 also contains text, table and
chart. Neither fixture is text-only. A metadata result must fail the full
checker, whose match still leaves Rust execution/source identity unverified.
The general corpus `--case` selector would run six operations, including
HTML/preview; it is not a narrow operation gate. No checker tests are repeated,
no references/tolerances are changed, and no full-import parity follows from
this source-only specification.

**Phase 4 (execution engine): implementation candidate; not accepted or
routed.** `packages/tofu-core-rs/src/engine/` extends the prototype. Its
library/replay-only scope excludes the resident scheduler, launch defaults and
`serverctl_pkg/execution_host.py`.
No live Turn runs on it, and no production restart is part of verification.

| Plan item | Delivered as | Gate |
|---|---|---|
| Agent loop, tool dispatch, settlement | `packages/tofu-core-rs/src/engine/`: a sans-IO loop (`session.rs`) over explicit ports (`ports.rs`: provider dispatch, tools, `DurableStore`, event sink, settlement redactor) composing pure policy modules ported from the Python owners: `abort` (one `ABORT_CAUSES` table), `settlement` (monotonic terminal, commit-then-publish, retained-terminal replay, one successor), `tool_effect` (intent with exact ack before entry, outcome classes, failure projection, restart classes, lost-executor receipts, new-write admission), `fallback` (content-filter fallback, return to the primary, whole-turn retry budget), `intent_stall` (the nudge), `steer`, `checkpoint` (protocol-valid boundary, rewind-and-resend, self-drain checkpoint before a paused settlement), `compaction` (admission refuses an overfull prompt, never head-truncates; verified-complete summaries only), `redaction`. Every emitted event is admitted through the generated `tofu_contracts::generated_events_v1::ContractedEvent`. | `cargo test` in `packages/tofu-core-rs` (module tests name the easy-to-lose row and the Python test they mirror; `tests/engine_loop.rs` pins the loop-level rows) |
| Parity with the Python engine | Boundary `engine_session`: `tests/rust_parity_engine/` (Python reference driving the real root agent loop, `_finalize`, dispatch, effect intents and persistence with only the LLM transport and tool handlers scripted; `cases.jsonl`: 8 recorded `tests/fixtures/llm_replay` fixtures + 18 fault cases: content-filter fallback, return to the primary after the refused tool loop, no-fallback refusal, 429, 5xx pre- and mid-stream, upstream exhaustion, dropped stream, owner cancel before/after text, lost/refused/raising tools, parallel tools, durable-event write failure, crash after a request and after a tool round with resume). Rust side: `tofu-engine parity` (`src/bin/tofu-engine.rs`, `engine/replay.rs`). | `python3 -m tests.rust_parity_engine.shared_harness --rust <tofu-engine>` uses the adopted shared harness from owner checkpoint `0efbc876c3` (only its test subtree, with source provenance retained). `compare` is also strict about process exits, answer counts and all five byte parts. Behavioural diff suppression has been removed from the cargo golden test. The gate requires a fresh Python oracle against the Rust binary from the same source snapshot; a golden-only result is insufficient. |
| Storage client and provider wire | The engine depends on `packages/tofu-storage-client` (P3a): settlement redaction is the storage client's own `CredentialRedactor::current(owner)` with `ModelRoutingKnownSecrets` (`engine/storage_v2.rs`), so settlement and write-time redaction cannot disagree. `storage_v2/binding.rs` now binds real storage.v2 commands with full host-owned projection patches, explicit owner/Attempt identity, CAS revision fences, frozen retry payloads and exact applied/count acknowledgement checks. `provider_wire.rs` binds canonical bodies, sealed wire and the landed P4 parser through an explicit transport port (no duplicate network authority). It is buffered and fixed-model; host route resolution and incremental transport remain gaps. | `engine_storage_v2`: seven active tests, including isolated TofuDB, rollback, checkpoint readback, stale-intent refusal, terminal receipt replay, and the scratch SIGKILL regression. The child-only entry is ignored in the top-level run and invoked by the regression; `engine_provider_wire`: ten tests including a real scratch TCP midstream disconnect |

**Checkpoint versus presentation.** Problem: a provider reply can commit text
before the loop adopts it into model history; a protocol-valid checkpoint is
therefore not the whole durable Turn. Decision: commit contracted deltas and
recover presentation separately from checkpoint history. The replay composition
folds only acknowledged delta/reset events and carries that text into the next
session, without appending it to model input or re-entering settled tools.
Alternatives rejected: dropping the committed prefix on resume, copying it into
model history, or normalising the outcome diff. `test_restart_projection.py`
queries the real Python authority at the interrupted frontier;
`engine_restart_projection.rs` pins both frontiers, Unicode, failed delta commits
and unchanged request/tool history. This does not provide the missing resident
host's accepted-Attempt recovery adapter.

The scripted tool-fault cases include the answer to the real intent-stall nudge.
`test_fault_scenarios.py` independently requires a completed blocked report,
three model requests, one tool result, the expected effect/error classification
and no tool re-execution. Byte agreement alone is insufficient: an earlier
26/26 run matched four unintended script-exhaustion errors and is not evidence
for these corrected paths. Goldens are generated exclusively by the Python
oracle; there is no behavioural diff suppression. Corrected corpus SHA-256:
`fe0a4c4f3e115557b9cbc23cee1f74b12be36ef74c4c711535c309331ceec920`;
Python golden: `b1206797bf43ae5afb652bd499d0172c386ddd9e10679f53b75f9f9cb8ca24fe`.
The buffered gate compares five declared projected parts. It does not run
resident cases or certify raw-wire or full durable-table parity. The separate
Rust scratch test below exercises one real engine SIGKILL and a fail-closed
readback, not successful continuation. Neither check is a production-readiness
claim.
The canonical Python refusal/publication repair is already shared and is not
part of this tranche.

Not yet, and why the route stays closed:
- The application bindings for the existing full Turn projector and
  scheduled-Turn steer admission, provider route selection, and the resident
  execution-host invocation/control/recovery composition are not built.
  StorageProjection and SteerRequeue are explicit host ports, not replacement
  projection or queue implementations.
- The actual-process gate `tests/rust_parity_engine/process_faults.py` sends
  SIGKILL, joins the dead child, starts a fresh child and requires the exact
  authority/checkpoint receipt. It remains red: neither engine has an admitted
  process-recovery adapter for this boundary. Its passing protocol-probe tests
  certify the runner only. The old `catch_unwind`/same-Python-process replay is
  diagnostic, never crash durability evidence.
- The Rust-only scratch boundary (`tests/support/engine_recovery.rs` in
  `tofu-core-rs`) uses a separate real storage.v2 authority that outlives both
  children. It kills the first engine after the tool round and the completed
  `scratch-recovery-checkpoint` view are readable, before a second dispatch.
  The fresh child independently reads the exact completed witness and paired
  assistant/tool view. Its scratch test driver reports
  `accepted_successor_and_claim_required` before constructing dispatch ports;
  this refusal is a test boundary, not runtime admission logic. The test
  requires one recorded dispatch, one synthetic mutation, one matched
  entered/returned operation pair and unchanged Turn/task rows across the refusal. This is readback,
  **not view adoption or process parity**. Correct continuation requires the
  host to retire the proven dead source, validate the storage-issued accepted
  successor relation and acquire its current worker publication fence, then
  validate the archive against authenticated source history/policy and the
  committed tool and presentation prefixes. The archive command ACK and the
  Rust v1 witness alone provide neither authority nor that validation.
- Nonterminal `turn.event.record` has no command receipt. CAS fences prevent
  duplicate intent publication, but committed-intent/lost-ACK reconciliation
  is not implemented; refusal is conservative and does not authorize entry.
  Terminal receipt replay is tested. Pending checkpoint creation and
  completed-view publication are two separate storage commands, not one
  transaction. Publication does not adopt the view into a successor Attempt.
- Unported Python lanes the corpus does not reach: pool rescue and reactive
  overflow recovery, the `run_command` restart-class shell classifier,
  HTTP-status classification of tool failures, the L1 compaction transforms,
  tool display rendering, cost and cache accounting.
- `edit_file`'s parallel-safe per-path mutex and the workspace lease model
  belong to the tool owners (Phase 5).

**Phase 6 progress (2026-11-18).** `tofu node` exists in `packages/tofu-node`
(an independent crate, excluded from the workspace like `tofu-db`): pairing and
join, per-host node identity digest-identical to the Python node, the v3 poll
loop with acknowledged-prefix outboxes and replay deadlines, the call journal,
Home-ceiling AND device-switch enforcement, files, system info, project
commands and supervised command execution. It passes
`tests/desktop/test_rust_node_interop.py` (frames validated against both
schemas; confirm-before-join, read-only default, intersection, refusal of an
unaddressed call without a default, twins on one home) and
`tests/desktop/test_device_routing_faults_slow.py` against the Python Home, and
`tests/chaos/test_chaos_nodes.py` runs every fault for both node programs. It
builds for Linux and checks for `x86_64-pc-windows-gnu` and
`x86_64-apple-darwin`.

Second step (2026-11-18): every `desktop_node_v1` command is ported (no
command answers `Unsupported`; `commands::tests::every_contract_command_is_implemented`):
GUI (X11 in pure Rust, Core Graphics, GDI/SendInput), egress with stream
cancel, the subscription adapter supervisor, `codex_login_read`, device paper
builds, plus the tray (`tofu node --tray`; StatusNotifierItem on Linux,
`tray-icon` on macOS/Windows), the packaged app's local link
(`--local-server`) and pairing/polling through the browser relay. Interop tests
against the Python Home: `tests/desktop/test_rust_node_{gui,egress,codex_login,paper,browser_relay,tray}.py`
(Xvfb, TinyTeX and a private dbus-daemon provisioned without root; each test
skips with the reason when its tool is absent). The paper case compares the
Rust and Python nodes' received receipts and manifests on one document. macOS
retires detached grandchildren through an inherited tree cookie (no subreaper
there; limit: a descendant that scrubs its environment and leaves the process
group survives). Found on the way and fixed: the pair-code LAN route used the
inherited `PORT` instead of the minting server's own port
(`tests/desktop/test_pair_code_lan_route.py`), and `lib/desktop/egress.py`
`open_stream` failed against any real node because the bridge creates a stream
entry only with its first frame. Still Python: the server bundled by the
packaged app (later phases) and therefore the PyInstaller distribution;
`lib/desktop_agent` stays until distribution ships `tofu node`. macOS and
Windows code is compile-checked only.

### Behaviours that are easy to lose

Each row is a property a rewrite can drop silently because it is not visible in
the happy path. Each must be a named parity case before its owner moves.

| Area | Behaviour | Source |
|---|---|---|
| Resources | Admission, concurrency and fan-out are always unlimited; only retention may follow the launch probe | `docs/RESOURCE_CONTROL.md` "Hardware never limits work"; `contracts/resource_policy_v1.json`; `tests/meta/test_hardware_never_limits_work.py` |
| Resources | No refusal on RSS, memory headroom or cgroup pressure; no `server_busy`; no worker recycling; executors start every accepted job | same; tokio's default 512-thread blocking cap and CPU-sized workers must not reintroduce a limit |
| Resources | Coordinator delegation ignores a persisted `maxConcurrent` | RESOURCE_CONTROL.md (owner clarification 2026-10-26); `tests/project_mod/test_coordinator_workers.py` |
| Resources | Tofu-started work writes `oom_score_adj=1000`; server processes keep the inherited value | RESOURCE_CONTROL.md "Tofu-started work is the kernel's first OOM victim"; `tests/project_mod/test_work_process_oom_priority.py` |
| Resources | Durable state is reclaimed only by reachability, never by budget; 1,000 idle conversations add no resident state | RESOURCE_CONTROL.md "Durable store growth is bounded by structure"; `tests/resource_control/test_thousand_conversations.py` |
| Defensive | A failing `__str__` cannot replace the original failure or its recovery class | `docs/DEFENSIVE_PATTERNS.md` "Failure diagnostics cannot replace the original failure"; `tests/tasks_pkg/test_tool_diagnostic_recovery.py` |
| Defensive | Tool calls take no workspace lease; per-path mutex for one read-modify-write, sorted, reentrant; calls in a round run in parallel | DEFENSIVE_PATTERNS.md "A tool call's edit is atomic per file"; `tests/project_mod/test_tool_calls_take_no_workspace_lease.py` |
| Defensive | Fair shared/exclusive admission lapses on its own; a holder never yields to itself | "Fair admission must lapse on its own"; `tests/project_mod/test_workspace_lease_conflict_model.py` |
| Defensive | Atomic writers own the temp descriptor and close it in every path; ENOENT during a racing replace on FUSE is not absence | "Atomic file writers retain sole descriptor ownership", "A missing store is decided under its writers' lock"; `tests/json_store/test_atomic_writer_lifecycle.py`, `test_replace_window_reads.py` |
| Defensive | Heartbeat persistence cannot block the serving loop | "Heartbeat persistence cannot block the serving loop"; `tests/server/test_heartbeat_persistence_isolation.py` |
| Defensive | Storage socket replies, half-closes, then waits for client EOF | "Response delivery before close"; `tests/storage/test_storage_server_overload.py` |
| Defensive | A finalizer (`Drop`) never blocks; an interrupted close restores the open state | "A finalizer never waits"; `tests/tofudb/test_embedded_runtime_lifecycle.py` |
| Defensive | Timeout, cancellation, exit status and error kind are reported independently | "Report orthogonal outcomes independently"; `lib/project_mod/run_command.py` |
| Defensive | Post-commit notices publish after the lock, in commit order, from one publisher | "Commit wakeups publish after the writer's critical section"; `tests/turn_lifecycle/test_commit_wakeup_publication.py` |
| Defensive | Signal handling is a self-pipe; 500 SIGTERMs stop the host exactly once | "Signal handlers take no lock"; `tests/server/test_execution_host_signal_stop.py` |
| Defensive | Helpers spawn from the code loaded at boot, never the live checkout (in Rust: a spawned helper matches the running binary's digest) | "Long-running processes spawn helpers from their loaded source" |
| Defensive | Untrusted output gets random exclusive paths in a 0700 directory; children get a scrubbed environment | "Untrusted output gets no predictable path" |
| Defensive | Chaos fault contracts keep their documented outcomes | "Fault contracts of the chaos suite"; `tests/chaos/` |
| Settlement | Terminal state is monotonic; settlement and terminal projection commit together; a terminal queue drain dispatches at most one successor | `docs/contracts/TURN_SETTLEMENT.md` "Invariants to test"; `tests/turn_lifecycle/test_turn_lifecycle.py` |
| Settlement | One `ABORT_CAUSES` table; an unknown reason reads as a person's Stop and logs ERROR once | TURN_SETTLEMENT.md "Abort causes"; `tests/turn_lifecycle/test_abort_cause_table.py` |
| Settlement | A failed terminal commit replays the retained terminal event; nothing reaches the browser before commit | TURN_SETTLEMENT.md "Retrying a failed terminal commit" |
| Settlement | Interruption recovery rewinds to the last protocol-valid boundary and resends byte-identically: no prefill, no synthetic "continue" | TURN_SETTLEMENT.md "Interruption recovery: rewind and resend"; `tests/llm/test_upstream_turn_replay.py` |
| Settlement | A Stop ack means accepted, not settled; late frames raise typed cancellation, never `server_busy` | TURN_SETTLEMENT.md "Explicit user cancellation"; `tests/tasks_pkg/test_internal_delivery_error_attribution.py` |
| Settlement | `git_guard` refusals stay failed with no automatic retry | TURN_SETTLEMENT.md "Known external blockers and unattended recovery" |
| Settlement | Background-command receipt and queue-row removal commit in one transaction; ACK only after verified completion | JOURNAL 2026-09-08 (后台命令投递根修); `tests/background/test_background_delivery_receipt.py` |
| Settlement | A steer receipt stays within 64 KiB even for a 1.2 MB Turn | JOURNAL 2026-09-07 (长回复立即插入 500) |
| Fallback | A content-filter or refusal skips same-model retries, bills `R<n>-REFUSED`, discards partial text and retries once on the fallback | TURN_SETTLEMENT.md "Content-filter outcomes"; `tests/tasks_pkg/test_model_refusal_fallback.py` |
| Fallback | A refusal applies to one request; the next returns to the primary except the single request repeating the refused payload; every switch emits `MODEL_FALLBACK` | same |
| Fallback | Empty or truncated stop is not a content filter; 402 and 429 never stamp one | `tests/tasks_pkg/test_empty_stop_not_content_filter.py` |
| Fallback | A resumed Attempt runs on the accepted model, not the last fallback | `docs/RUN_CONTINUITY.md` "A resumed Turn returns to the accepted model"; `tests/tasks_pkg/test_resume_model_continuation.py` |
| Tool effects | Non-read-only calls commit an intent with an exact storage ack before entry; reads pay nothing | `docs/contracts/TOOL_EFFECT_RECOVERY.md`; `tests/tasks_pkg/test_tool_outcome_finality.py` |
| Tool effects | `entered` is not success; post-entry exceptions are `unknown`; only typed pre-entry refusals become `not_started` | same; `tests/tasks_pkg/test_tool_admission_refusal_effect.py` |
| Tool effects | A lost-executor `write_file` gets a non-retryable `tool_effect_unknown` receipt; a new write to that target needs an earlier-round inspection and, if still unknown, a human gate | TOOL_EFFECT_RECOVERY.md; `tests/tasks_pkg/test_file_recovery_admission.py` |
| Tool effects | After restart, only `unknown`-class cut calls block resume; a call stopped before it ran is repeatable | RUN_CONTINUITY.md "A call the restart stopped before it ran is repeatable"; `tests/tools/test_restart_effect_class.py` |
| Restart | One commit point (stopping the old worker); past it the manager always ends serving | `docs/modules/infra_runtime.md` "A restart that stopped its old server always ends serving"; `tests/server/test_restart_always_serves.py` |
| Restart | A failed or slow engine build never blocks an application restart | `docs/NATIVE_STORAGE_LIFECYCLE.md` "Application restarts never depend on the storage engine build" |
| Restart | A refused restart names every bridge and reason; outcomes go only to `logs/server-manager.log` | infra_runtime.md "A refused restart names every bridge"; `tests/lifecycle_approval/test_restart_outcome_visible.py` |
| Restart | Preparation phases and timing fields; the browser warns at 180 s but keeps observing | `contracts/restart_preparation_v1.json`; infra_runtime.md "Restart observation" |
| Restart | Quiesced Turns resume exactly once from their checkpoint, including Routers and swarm masters | RUN_CONTINUITY.md "Restart-paused conversations resume by themselves"; `tests/conversation/test_restart_resume.py`, `tests/swarm/test_swarm_restart_continue.py` |
| Restart | Landings and exports hold a protected-steps marker; restart never boots a half-landed tree | infra_runtime.md "Restart callers and restart protection"; `tests/lifecycle_approval/test_update_restart_guard.py` |
| Delegation authority | Forwarded human input is byte-identical text from a genuine human Turn; a router note is never authority | `docs/ROUTER.md` R2; `tests/router/test_lead_delegation.py` |
| Delegation authority | Live restart needs a human decision or the owner's revocable Global Router grant, bound to one router and revision; every use audited | ROUTER.md R9; `tests/router/test_owner_delegation.py`, `tests/router/test_delegated_action.py` |
| Delegation authority | Continuations keep the full role catalog; owner-only decisions are gated at execution | ROUTER.md R15; `tests/project_mod/test_continuation_catalog.py`, `tests/meta/test_lead_owner_only_boundaries.py` |
| Delegation authority | Synthetic users never inherit human authorization | AGENTS.md design principle 4 |
| Hope credentials | The Hope queue ban is re-read on every submit, rerun, clone and retry; a missing policy enforces the default | ROUTER.md R16; `tests/tools/test_hope_queue_policy.py`, `tests/project_mod/test_hope_queue_command_guard.py` |
| Hope credentials | The Hope token is read in place, never copied or logged, and verified on an endpoint that checks it; the MLP refresh token rotates and must never be copied (a copy revokes the host session); the patrol never renews or prompts | `docs/COMPUTE_PATROL.md` (Decision, Alternatives rejected); commit `7ed36ddd56`; `tests/compute_patrol/test_probe.py` |
| Compaction | No wall-clock deadline on summaries; only verified complete outputs qualify; failure keeps the sources; an overfull prompt is refused, never head-truncated | `docs/CONTEXT_COMPACTION.md` "Complete summaries without a wall-clock deadline"; `tests/compaction/test_summary_completion_policy.py` |
| Compaction | One provider-independent compactor; L1 and L2 planned jointly on a detached candidate, adopted durably | "One provider-independent compaction owner", "Durable adoption and Continue"; `tests/compaction/test_compaction_joint_projection.py` |
| Compaction | Credential outages and rotation retry within the documented bound and settle failed attempts authentically | "Credential-outage robustness"; `tests/compaction/test_summary_credential_resilience.py`, `test_summary_credential_rotation.py` |
| Compaction | The full active checklist survives final admission and compaction | "Authoritative checklist restoration"; JOURNAL 2026-09-07; `tests/goals/test_checklist_context.py` |
| Network | Only proven connection-setup failures change route; status codes, read timeouts and ambiguous disconnects never replay | `docs/NETWORK.md` "Routing and delivery"; `tests/proxy/test_unified_network.py` |
| Network | Provider pacing is FIFO per quota lane and learns limits only from provider signals | NETWORK.md "Provider pacing"; `tests/proxy/test_provider_pacing.py`, `tests/llm_dispatch/test_provider_pacing_loopback.py` |
| Storage | Replay-safe calls wait out transient errors with no total window while the authority is alive; `request_not_dispatched` must be literal `true` | `docs/STORAGE.md` "Client retry contract"; `tests/storage/test_storage_v2_managed_recovery.py`, `tests/agent_core/test_storage_error_contract.py` |
| Storage | A queued Turn pair runs before any later Turn-less source | STORAGE.md "A queued Turn pair runs before any later source"; `tests/message_queue/test_message_queue.py` |
| Storage | Canonical JSON bytes (sorted keys, orjson number and string formatting) match where digests, receipts or storage.v1 depend on them | `lib/storage/protocol.py`; storage receipt contracts in `tofudb_ir_v1.json` |
| Swarm | Admission waits heartbeat liveness; `AbortedError` lands as CANCELLED with no retry | JOURNAL 2026-09-06; `tests/swarm/test_swarm_admission_liveness.py` |
| Wire | SSE resume, heartbeat and frame vocabulary; push WebSocket limits and error codes | `contracts/conversation_sync_v3.yaml`, `contracts/control_rpc_v1.yaml`; `tests/sse`, `tests/websocket` |

### Phase 2 progress: manager and lifecycle

State on 2026-11-18. **Not cut over**: the live install still runs
`supervisor.py` + `server_manager.py`; nothing is deleted yet.

**What exists.** `packages/tofu-manager` (workspace member, binary
`tofu-manager manager [--source-root PATH]`) speaks the `supervisor.py` HTTP
protocol byte-for-byte in its fields, so `serverctl.py`, the application's
restart door and the Android shell are unchanged clients. It owns: the HTTP door
and allow-list (`http.rs`); the state machine, the one restart commit point,
failure budget, exit accounting, storage-lease deferral, never-served storage
recovery and execution recovery (`manager.rs`); resident-host ownership with the
inherited lease and manager-control pipe EOF (`resident.rs`, port of
`resident_service.py`); engine certification record verification and the
operator-override validation (`certification.rs`); preparation children and
the `TOFU_ENGINE_DECISION` gate (`preparation.rs`, `child.rs`); the
`restart_preparation_v1` projection (`restart_preparation.rs`, checked against
the contract file); `logs/server-manager.log` lines in the Python logging
layout (`log.rs`); the state file in the Python JSON layout under the shared
`<path>.lock`, so either manager takes over the other's record (`json.rs`).

**Python policy still reached from Rust** through one seam,
`serverctl_pkg/manager_bridge.py` (one child per call, run from the manager's
source checkout): worker launch-environment composition (resource policy),
retained-engine contract refusal, known-good engine retain/restore, lead audit,
log retention, background precertification, `process_owner` exit records,
`execution_plan.source_only_change`, and worker runtime selection
(`runtime_replica.select_worker_runtime`, `python_bytecode_cache`: the
`prepare-worker-runtime`, `request-runtime-build`, `request-bytecode-sweep`,
`reject-runtime-generation` and `validate-bytecode-namespace` operations). Each
operation is deleted when its owner moves (Phases 3 and 5). Engine builds,
deployment and the certification gate's decision still run in the
`serverctl.py prepare-frontend` child, as for the Python manager.

**Runtime replicas and the bytecode cache are ported** (`runtime.rs`): the bridge
decides, and the manager itself re-takes the shared leases the worker lifetime
needs (generation `.tofu-replica.lease`, cache namespace `.active.lock`) with
the owners' exact lock rules, because the bridge child's leases end with it. A
lease it cannot re-take never runs the worker there: the spawn falls back to the
source interpreter or no cache prefix. Suspicion, rejection, post-ready builds,
throttled sweeps and lease restore on adoption follow `server_manager`.
*Alternative considered:* launching the source interpreter only. Rejected by
measurement on this host (2026-11-18, five alternating runs, interpreter start
plus eight heavy imports): median 1.32 s from the FUSE-resident environment
against 0.55 s from its local replica.

**TLS readiness is ported** (`probe.rs`): a worker serving HTTPS
(`TOFU_TLS=1`, `.last_serve_mode == https`) is probed over rustls (ring) with
its certificate unverified, exactly like Python's
`ssl._create_unverified_context()`; identity still comes from the payload PID.

**Ownership** (`ownership.rs`) ports `server_manager.SupervisorOwnership`: the
checkout's `supervisor.py` sentinel, the 10 s / 0.5 s graces, replaced-directory
identity and owner start ticks.

**Threads.** Resident threads are fixed: one accept loop, one signal reader, one
monitor per project. Each accepted control request gets its own thread, as
`ThreadingHTTPServer` does. *Alternative considered:* a fixed request pool. It
was rejected because a restart request holds its thread through preparation
(up to `LIFECYCLE_REQUEST_SECONDS`), so a pool would queue status polls and
stops behind it, an admission limit the hardware rule forbids. Nothing reads the
CPU count; no tokio runtime is used.

**Cutover.** `supervisor.sh` runs the binary named by `TOFU_MANAGER_BINARY`
instead of `supervisor.py` (daemon, run and systemd paths); unset keeps Python.
The Rust manager reports the same `sourceFingerprint` closure as
`supervisor_protocol.SUPERVISOR_SOURCE_FILES` (a unit test pins the list), so
`serverctl.py ensure_manager` accepts it, plus `implementation: rust` and
`binarySha256`. `tests/server/rust_manager_parity/mapping.json` has no `gap`
category: every manager behaviour of `tests/server` and
`tests/lifecycle_approval` has a black-box parity test or a Rust unit test, or
specifies a Python owner the Rust manager reaches through the bridge or a
preparation or host child. What remains is the owner's cutover decision.

**Verification.**
- `cargo test`, `cargo clippy --all-targets -D warnings` and `cargo fmt --check`
  for `-p tofu-manager`.
- `tests/server/rust_manager_parity/`: the same black-box tests run against
  both managers. They cover start, restart, crash retry, refuse-before-stop
  (frontend, engine `refused`, state-file persistence/quota, resident host owned
  elsewhere), build-then-stop ordering, storage-host replacement and unconfirmed
  close, deferred restart, reload refusal, owner-exit retirement and adoption of
  a live worker by a successor of either implementation. Since the rework they
  also cover a real `EDQUOT` after the stop (`parity_shim.c`, preloaded into
  the manager), a storage host past its 100 s stop budget being SIGKILLed,
  known-good engine rollback with the real deployment owner, the real SQLite
  storage host, the resident execution host (composition, retention,
  replacement, failed close, recovery, interpreter), the real `/reload` re-exec
  from a private closure copy, checkout ownership, the state-file lock,
  `TCP_NODELAY`, exit accounting, runtime replica and bytecode-cache leases,
  and HTTPS readiness. The quota case found a Python gap fixed in
  `server_manager`: the planned-exit and stop-outcome saves raised after the
  commit point and dropped the `/restart` request.
- `mapping.json` + `test_mapping.py` classify every `tests/server` and
  `tests/lifecycle_approval` file (application owner, retained Python owner,
  unchanged client, Rust/parity equivalent, or gap).
- `python3 -m tests.server.rust_manager_parity.isolated_instance <root>` runs a
  real instance on its own ports and data root through
  `serverctl.py start` -> `supervisor.sh daemon` -> Rust manager. It checks an
  update restart that resumes a seeded interrupted chat from its checkpoint with
  one restart notice, a refused restart that leaves the old worker serving, a
  quota-full volume from the stop on (accepted, retried, served once space
  returns), and stop and owner-exit retirement, with the outcomes in
  `logs/server-manager.log`. Engine rollback is proven by the parity instances
  only: the isolated instance uses SQLite, and landing a deliberately failing
  TofuDB engine there is refused by the certification gate by design.

On "Hope token rotation": nothing in `lib/` rotates a Hope token. The rotating
credential is the MLP SSO refresh token. The rule to keep is "never copy, never
renew, never prompt", which is the Hope credentials rows above.

## Where time goes

Question: where does real recorded work spend wall time and resources today,
and what is the most a rewrite could save? Measured 2026-11-18, read-only
against the live instance, which was not restarted. Measurement script:
`benchmarks/rust_rewrite_time_breakdown.py`, with its spec in
`tests/meta/test_rust_rewrite_time_breakdown.py`. Data:
`output/rust_rewrite/time_breakdown/`.

### Short answer

- **Model and tool waits make up 85-99 % of a Turn's wall time.** Most of the
  rest is Python harness work. On current code (Turns created on 2026-11-18),
  the **upper bound** on the end-to-end saving if every Python cost went to
  zero is **4.2 % for chat, 3.0 % for leads, 2.1 % for Workers and 0.9 % for
  swarm children** (pooled wall time). Per Turn, the median share of the
  harness-per-round bucket is 1.0-2.1 %.
- **Over the whole sample the bounds are larger: 14.2 %, 25.3 % and 16.9 %.**
  The extra comes almost entirely from 27 "stall" Turns (2026-11-15 to 11-17)
  that sat for hours in the round-preparation bucket until a restart or a
  storage restart released them (see [Stall Turns](#stall-turns)). Without
  those 27 Turns the bounds are 7.5 % (lead) and 8.3 % (Worker). The
  durable evidence does not show the cause. Neither Python CPU time nor GIL
  starvation is proven.
- **The GIL's cost to concurrency has already been removed by process
  sharding.** With 20 concurrent sessions the per-round overhead is
  88 ms now, against 2,029 ms at the 2026-11-17 single-interpreter baseline.
  No live process holds the GIL more than 7.2 % of the time. The price is
  memory: 28 forked execution shards use **10.4 GB PSS**. The Python
  processes together use about 12.5 GB, and the storage engine (already
  Rust) uses 21 GB.
- **CPU per model request at 20 sessions:** 84 ms of Python (shards 79.3 ms,
  execution host 3.5 ms, API host 1.1 ms) against 33.6 ms in the Rust storage
  engine.
- **Failures:** of 478 fix commits in the last 14 days, static types plus no
  GIL would have structurally prevented 9 and partly prevented 9 more
  (about 4 %). See [Failure classes](#failure-classes-last-14-days).

### Sample and method

- **Population.** Every task row returned by `/api/v4/tasks/by-conv` for all
  476 conversations, created in the window 2026-11-11 12:54 to 2026-11-18
  12:54 (+08:00). The store holds no task older than 2026-11-15 15:58, so
  the 7-day window actually covers 2026-11-15 to 11-18.
- **This is a census, not a sample.** The analysis covers **1,328 settled
  root Turns** (chat 83, lead 457, Worker 788; 2 have no summary) and **238
  swarm children** (chat parent 34, Worker parent 204): 1,566 in total.
  - Kind is assigned as follows: Worker = a conversation with
    `_coordinatorSource`; lead = a `coordinator-*` conversation (Project
    Routers, Global Router, Watchdog); chat = any other conversation; swarm =
    `isSwarmAgent` rows.
  - Turns still running are excluded (28 rows).
- **Root Turns.** The durable attempt trace from `/api/v4/tasks/<id>/trace`
  ([TURN_TRACE_CONTRACT.md](contracts/TURN_TRACE_CONTRACT.md)) splits each
  Turn's wall time into disjoint buckets that add up to `totalMs`. The
  report's components map onto those buckets:

  | component | trace buckets |
  |---|---|
  | model wait | time to first token = `ttftMs`; streaming = `llmMs − ttftMs − failedAttemptMs` |
  | tool execution | `toolMs`, split by family in proportion to each tool's elapsed time |
  | harness Python per round | `roundPrepMs + roundSettleMs + finalizeMs` |
  | startup and queueing | `startupMs + admissionWaitMs + spawnWaitMs` |
  | compaction | `compactionMs` |

  - **Where tool time comes from.** A long trace keeps at most 256 spans. For
    the 629 traces that dropped spans, tool time comes from the Turn's
    complete `toolRounds` (`/api/v4/conversations/<c>/turns/<t>`, current
    attempt only).
  - **Clock caveats.** 142 traces are `partial/clock-domain-change` (a
    restart inside the attempt) and 5 are `no-round-markers`. They are kept.
- **Swarm children.** Swarm children have no attempt trace. Their round
  boundaries come from Request Inspector request snapshots (`/requests`).
  - For a deterministic subsample of 48 children (hash order), the post-tool
    state snapshot (`/requests/<n>?kind=state`) splits each round at the
    snapshot timestamp:
    - in-round time (request → state): model, tools and in-round settle;
    - between-round harness time (state → next request).
  - Model and tool time inside a child round cannot be separated from
    retained evidence. Swarm status keeps per-tool timings only while the
    session lives, and only for its last 30 calls (6 of 238 children).
  - The final round's model time is not observable.
- **Storage commits** are not a trace bucket. They sit inside round
  preparation and settlement (durability barrier, input reads) and startup,
  and are itemized from logs and `/metrics` below.
- **Reads.** 4,881 GETs (4.4 GB). Retained `app.log*` covers
  2026-11-18 00:00 to 13:01 only (509 Turn summaries).

Reproduce:

```sh
P=benchmarks/rust_rewrite_time_breakdown.py
.venv/bin/python3 $P collect --out evidence.json            # GET-only, loopback 15000
.venv/bin/python3 $P analyze evidence.json --harness-perf <harness_perf report.json> --out analysis.json
.venv/bin/python3 $P render analysis.json                   # the tables below
.venv/bin/python3 $P logs logs/app.log logs/app.log.2026-11-18* --out log_timings.json
.venv/bin/python3 $P resources --manager-pid <supervisor pid> --seconds 60 --out resources.json
```

### Wall-time shares per kind (median / p90 of per-Turn shares, and the pooled share), %

The pooled share is the component's sum divided by the kind's total wall
time. It is the figure that bounds end-to-end savings. Medians are 0 when
most Turns never use a component (for example `poll`).

**Whole sample (2026-11-15 to 11-18)**

| component | chat | lead | Worker |
|---|---|---|---|
| Model: time to first token | 30.9 / 69.3 / 24.9 | 38.3 / 84.8 / 7.2 | 30.8 / 74.4 / 19.9 |
| Model: streaming | 4.7 / 54.5 / 18.4 | 9.8 / 60.7 / 2.2 | 1.9 / 23.7 / 3.7 |
| Model: failed attempts + provider retry waits (pooled) | 0.0 | 0.1 | 0.2 |
| Tool: shell (`run_command`) | 15.9 / 40.4 / 17.5 | 0 / 0 / 0.0 | 16.3 / 42.2 / 14.0 |
| Tool: file | 0.5 / 4.2 / 1.1 | 0 / 0 / 0.1 | 0.3 / 4.1 / 1.7 |
| Tool: web / browser / MCP (pooled) | 0.0 / 0.05 / 0.0 | 0 / 0 / 0 | 0.06 / 0.0 / 0.4 |
| Tool: waiting on agents, background commands or human (`poll`, `await_*`, `ask_human`) | 0 / 36.7 / 23.9 | 0 / 88.2 / 65.1 | 5.3 / 76.4 / 43.1 |
| Tool: Tofu-internal Python tools (pooled) | 0.2 | 2.7 | 1.0 |
| **Harness Python per round** (prep + settle + finalize) | 3.8 / 34.1 / 9.6 | 1.3 / 7.3 / 22.2 | 1.1 / 9.3 / 12.4 |
| Startup + queueing | 3.4 / 18.6 / 4.2 | 0.7 / 5.6 / 0.3 | 1.4 / 16.1 / 3.4 |
| Compaction (marked spans) | 0 / 0 / 0.03 | 0 / 0 / 0.08 | 0 / 0 / 0.01 |
| Turns; wall median / p90 s; total h; rounds | 83; 886 / 4,673; 48 h; 4,178 | 457; 72 / 3,782; 196 h; 4,028 | 788; 1,675 / 8,473; 792 h; 51,676 |
| Harness ms per round, median / p90 | 976 / 20,112 | 256 / 2,638 | 320 / 8,362 |

**Current code (Turns created 2026-11-18 00:00-12:54)**

| component | chat | lead | Worker |
|---|---|---|---|
| Model: time to first token | 31.8 / 55.6 / 24.6 | 39.5 / 84.8 / 9.3 | 47.0 / 83.7 / 30.8 |
| Model: streaming | 13.9 / 60.2 / 17.1 | 17.5 / 62.2 / 4.9 | 2.0 / 23.7 / 5.6 |
| Model: failed attempts + retry waits (pooled) | 0.0 | 0.4 | 0.8 |
| Tool: shell | 13.5 / 50.6 / 16.6 | 0 / 0 / 0.0 | 14.5 / 35.9 / 15.0 |
| Tool: file | 0.0 / 1.2 / 0.6 | 0 / 0 / 0.0 | 0.2 / 1.6 / 0.7 |
| Tool: waiting on agents/commands/human | 0 / 48.2 / 37.0 | 0 / 91.2 / 82.3 | 0.5 / 78.0 / 45.1 |
| Tool: Tofu-internal (pooled) | 0.1 | 2.4 | 0.2 |
| **Harness Python per round** | 2.1 / 5.6 / 3.2 | 1.1 / 4.5 / 0.4 | 1.0 / 3.0 / 0.9 |
| Startup + queueing | 1.2 / 28.7 / 0.7 | 0.6 / 4.5 / 0.1 | 0.7 / 4.5 / 0.8 |
| Turns; wall median / p90 s; total h | 8; 469 / 7,939; 4 h | 195; 43 / 2,134; 36 h | 278; 1,033 / 4,507; 147 h |
| Harness ms per round, median / p90 | 308 / 2,909 | 223 / 636 | 229 / 726 |

**Swarm children (state-timestamp subsample)**

| | whole sample (48 children, 18.1 h, 2,110 rounds) | 2026-11-18 (14 children, 2.9 h, 506 rounds) |
|---|---|---|
| In-round: model + tools + in-round settle, med / p90 / pooled % | 98.4 / 99.7 / 95.7 | 99.5 / 99.8 / 92.5 |
| **Between-round harness Python**, med / p90 / pooled % | 1.0 / 8.3 / 3.0 | 0.4 / 1.5 / 0.5 |
| Harness ms per round, median / p90 | 99 / 4,310 | 48 / 206 |

Over all 238 children (190 without a state timestamp), the observable window
is 77 h. Of that, 76 % is "model + harness, unsplit" and 22.6 % is "in-round,
unsplit". The tool families observed directly (6 children) total 0.4 %.

**What "harness per round" contains.** Itemized from `[RoundPrepTiming]`
for the 509 Turns of 2026-11-18. Rounds whose preparation takes at least 1 s
are logged with named steps; they account for 2,277 s, or 44 % of all 5,201 s
of round preparation. As shares of all round preparation:

| step | share | notes |
|---|---|---|
| `build_body` | 27.4 % | `mcp_authority` 15.0 %, `request_body` 10.6 %, context telemetry 1.3 % |
| `compaction` | 6.2 % | 19 L2 summaries, median 16.1 s each: these are model calls, not Python |
| `barrier` | 4.9 % | storage durability wait |
| `inputs` | 2.3 % | inbox, working facts and project messages read from storage |
| `admission` | 1.8 % | |
| `receipts` | 1.0 % | |
| sub-second rounds | 56 % | not itemized |

Round settlement adds 1,519 s and finalize adds 15 s.

**Startup.** Over the whole sample, the startup-phase totals are, in
seconds:

| phase | Worker | lead | chat |
|---|---|---|---|
| `worker_start` | 4,483 | 208 | 225 |
| `tools` | 2,383 | 631 | 230 |
| `context` | 1,620 | 350 | 277 |
| `dispatch` (queue before the first event) | 1,368 | 469 | 131 |
| `workspace` (Git binding) | 940 | — | — |

`[TaskStartupTiming]` (2026-11-18, median / p90 ms) shows the slow stages:

| stage | median ms | p90 ms |
|---|---|---|
| `worker_entry` | 307 | 3,453 |
| `workspace_binding` | 494 | 5,080 |
| `startup_context` | 144 | 705 |
| `startup_tools` | 71 | 439 |
| `memory_rank` | 18 | 222 |

### Storage commits

Storage is not a separate bucket in the trace. It is visible in four places:

| where | measurement | figure |
|---|---|---|
| Inside round preparation | durability barrier | 4.9 % of round preparation |
| Inside round preparation | input reads | 2.3 % of round preparation |
| Turn start (median / p90) | `context_read` | 17 / 190 ms |
| Turn start (median / p90) | `turn_persist` | 8 / 42 ms (7,299 commits) |
| Turn start (median / p90) | `claim_next` | 9 / 28 ms |
| Completion (median / p90 per Turn, 654 Turns) | `completion.persistence` | 362 / 2,578 ms |
| Completion (median / p90 per Turn, 654 Turns) | `completion.workspace_delivery` | 321 / 2,352 ms |

Completion work appears to run after the trace's terminal event: finalize
medians are a few milliseconds. This is an inference, not measured.

Live client-side RPC load (`/metrics` delta over 60 s with 13 sessions
dispatching; `live/storage_rpc_delta.json`):

- **API host:** 79 RPC/s and 0.46 client-seconds per second. Of that,
  `turn.sync.changes` is 73 % (mean 5.4 ms) and `model_routing.get` 14 %
  (mean 176 ms).
- **Resident execution host:** 12.7 RPC/s and 0.019 client-seconds per
  second.
- **Execution shards** export no storage metrics, which is a measurement
  gap.

The engine that serves all of this is already Rust: 0.5-1.07 cores live.

### Resource cost

Commands and raw output are in `output/rust_rewrite/time_breakdown/`.

**Private instance.** `benchmarks/harness_perf/run.py --root <tmp> --out <tmp>
--run-id rust-evidence-full --py-spy <py-spy>` ran in full mode at commit
`235644fae368` against a zero-latency stub model, with host load 7-18 on
58 visible CPUs (`harness_perf/report.{json,md}`).

| process | idle RSS MB | idle CPU cores | 20 sessions RSS MB mean / max | 20 sessions CPU cores | per session at 20 |
|---|---|---|---|---|---|
| API host | 135.6 | 0.041 | 136.9 / 136.9 | 0.157 | 0.008 cores |
| execution host | 193.1 | 0.004 | 262.8 / 266.2 | 0.476 | 3.5 MB, 0.024 cores |
| execution shards | 0 processes (fork server 0.072 cores) | 0.072 | 3,912.7 / 4,549.1 (23 processes) | 10.883 | ≈196 MB, 0.54 cores |
| storage engine (Rust) | 38.3 | 0.007 | 1,074.7 / 1,954.4 | 4.612 | 0.23 cores |

The zero-latency model makes every session run rounds back to back, so the
per-session CPU here is a worst case. CPU per model request: shards 79.3 ms,
engine 33.6 ms, execution host 3.5 ms, API host 1.1 ms.

**Cold start.** Storage 0.52 s, execution host 7.56 s, API host
2.02 / 2.24 / 2.30 / 2.11 / 2.11 s over 5 starts.

**Live instance.** `rust_rewrite_time_breakdown.py resources --manager-pid
31411 --seconds 60` ran with 4-5 sessions active (`live/resources_4_sessions.json`,
which includes PSS). An earlier window with 13 sessions is in
`live/resources_13_sessions.json`; it has RSS only, and its "mcp" row
wrongly counted a measurement shell.

| process | RSS MB | PSS MB | CPU cores (4-5 sessions) | CPU cores (13 sessions) |
|---|---|---|---|---|
| API host | 1,217 | 1,187 | 0.067 | 0.121 |
| execution host | 969 | 942 | 0.013 | 0.013 |
| 28 execution shards | 11,884 | 10,380 | 0.370 | 0.274 |
| MCP children (dx bridge node + wrapper) | 114 | 100 | 0.000 | 0.001 |
| storage host + engine (Rust) | 41 + 21,017 | 27 + 21,028 | 0.505 | 1.079 |

- **Per active live session**, Python CPU across the API host, execution
  host and shards is about 0.03-0.1 cores. Sessions mostly wait on the
  model.
- **Shard memory does not scale with sessions.** It is a fixed pool of 28
  forks, about 370 MB PSS each.
- **MCP children** start on demand: `hope-mcp` was 40 MB (uv) plus 89 MB
  (python) at 1.4 % CPU when present.
- **Live restarts on 2026-11-18** (lead-audit `restart_outcome`, 5
  restarts): API-host `bootSeconds` 25.2-61.3 s, and 64-159 s from request to
  ready. That is 12-30x the private cold start; the difference is boot work
  on the production store and volume, which these sources do not itemize.

**GIL contention.** `py-spy record --pid <pid> --duration 45 --gil
--nonblocking --threads --format raw --rate 50`, live with 13 sessions
(`live/gil_pyspy.json`). The share of time the GIL was held:

| process | GIL held | top frames |
|---|---|---|
| API host | 7.2 % | storage-client decode, JSON identity, deep copies |
| execution host | 0.7 % | |
| one shard | 0.2 % | |

At 20 private sessions, 23 shards share 10.9 cores, about 0.47 cores each,
so no interpreter is saturated. Before sharding, the 2026-11-17 baseline had
the execution host at 1.03 cores with 20-session rounds at 2,029 ms
([HARNESS_PERFORMANCE.md](HARNESS_PERFORMANCE.md)), against 88 ms now. A
loaded per-shard `--gil` profile was not taken.

### Upper bound for a rewrite

**This is an upper bound, not a prediction.** For each component, the bound
is the end-to-end saving if that component's Python cost went to zero:

| component | Python share assumed by the bound |
|---|---|
| harness per round, startup/queueing, Tofu-internal tools, unattributed gaps | 100 % (they may vanish entirely) |
| model wait | the measured Python cost per model request: 22.2 ms (harness_perf, 466 ms over 21 rounds at 20 sessions) |
| external tools and agent waits | the measured Python cost per tool call: 51.25 ms (1,025 ms over 20 calls at 20 sessions) |
| provider retries, human approval, model-based compaction | none |

All values are pooled shares in %, shown as component share / **upper bound**.

| component | chat | lead | lead w/o stall Turns | Worker | Worker w/o stall Turns | swarm (subsample) |
|---|---|---|---|---|---|---|
| model wait | 43.4 / **0.1** | 9.3 / **0.0** | 15.5 / **0.0** | 23.6 / **0.0** | 29.9 / **0.0** | 96.9 (incl. tools) / **0.1** |
| external tools + agent waits | 42.6 / **0.1** | 65.3 / **0.0** | 76.7 / **0.1** | 59.4 / **0.1** | 61.7 / **0.1** | in-round / **0.2** |
| Tofu-internal tools | 0.2 / **0.2** | 2.7 / **2.7** | 4.5 / **4.5** | 1.0 / **1.0** | 0.9 / **0.9** | 0.0 / **0.0** |
| harness per round | 9.6 / **9.6** | 22.2 / **22.2** | 2.5 / **2.5** | 12.4 / **12.4** | 3.0 / **3.0** | 3.0 / **3.0** |
| startup + queueing | 4.2 / **4.2** | 0.3 / **0.3** | 0.5 / **0.5** | 3.4 / **3.4** | 4.3 / **4.3** | n/a |
| compaction, provider retry | 0.03 / **0** | 0.2 / **0** | 0.3 / **0** | 0.1 / **0** | 0.2 / **0** | n/a |
| **total upper bound** | **14.2** | **25.3** | **7.5** | **16.9** | **8.3** | **3.2** |

The same bounds for current code (Turns created 2026-11-18):

| kind | total upper bound | made up of |
|---|---|---|
| chat | **4.2 %** | harness 3.2, startup 0.7 |
| lead | **3.0 %** | internal tools 2.4, harness 0.4 |
| Worker | **2.1 %** | harness 0.9, startup 0.8, external-tool overhead 0.2 |
| swarm | **0.9 %** | |

The bounds are generous. Inside the harness bucket, at least 6 % is model
compaction calls, 15 % is MCP authority waits and 5 % is storage barrier
waits. Startup includes Git workspace binding and executor queueing. All of
it is counted as removable.

### Stall Turns

27 Turns (8 lead, 19 Worker) spent at least an hour in the harness bucket.
Together they hold 121 h, which is 83 % of all harness time in the sample.
All of them started between 2026-11-15 and 11-17. Each ended when the
process or storage was cycled:

- 2026-11-16 12:25: supervisor stopped.
- 2026-11-17 11:16: worker live but storage restarting, then ready 57 s
  later.
- 2026-11-17 21:49: restart.

These events are in `logs/server-manager.log`. None of the 2026-11-18 Turns
stalled, and on that day the largest itemized round preparation was 40 s.
Their `round_prep` spans were dropped from the bounded trace and the logs
from those days have rotated, so the cause cannot be attributed. A
stall that ends only when the process restarts is a liveness defect. A
language without a GIL removes it only if it was GIL starvation, which the
2026-11-12/13 execution-host profiles made plausible before sharding.
Static types do not remove it if it was a blocked wait.

### Failure classes (last 14 days)

Source: `output/rust_rewrite/time_breakdown/failure_classes.{md,json}`.
That review covers 478 `fix` commits from 2026-11-04 to 11-18 and the
retained logs:

| log | covered period |
|---|---|
| `server-manager.log` | from 11-15 08:14 |
| `error.log*` | from 11-16 22:22 |
| `incident.jsonl*` | from 11-17 20:32 |
| faulthandler dumps | from 11-17 23:46 |

Each class has cited commits or log lines.

| class | fix commits | prevented by static types + no GIL? | why |
|---|---|---|---|
| logic / contract | 137 | no (1 partly) | Wrong decisions, ordering and state-machine edge cases compile in any language (48c96ba45e, 404755989c, 4289858cc2). |
| build / test / packaging | 99 | no | Not runtime faults. |
| restart / lifecycle | 75 | no (3 partly) | Port and lease handover, cross-process state, policy gates. Examples: `server-manager.log` 11-17 23:10-23:30 EADDRINUSE loop, 11-17 22:09 execution host killed after 100 s. Only stale module closure (4e12631be9, 9ab8ad84bb) and 7401821614 partly. |
| storage defects | 60 | no (1 partly) | Protocol and ordering bugs, several inside the already-Rust engine (848771345e, a26d76fc48, 74f917b812); data loss on Continue (e986bbaf8e). |
| provider / network | 42 | no | External causes (c44c1836dc, 92f2f6c1ac). |
| UI / frontend | 33 | no | Browser code. |
| races / concurrency | 15 | no (1 yes) | Cross-process, filesystem and storage CAS races (bd0c2cc0db, 97cd9baa53, 5d2eee1104). The one in-memory data race is 59c75e5fa8 (yes). |
| type / None / attribute | 8 | **yes** (6 yes, 2 partly) | Examples: 4485c8957b, 130d1a6288, 301c5f8973, 546356f1f7, 647450ff15; `server-console.log` 11-18 11:15:18 bytes TypeError. |
| deadlocks / hangs / GIL stalls | 7 | partly (1 yes, 1 partly) | Lock re-entry (300c9c53b2), accept/close semantics and blocking-in-async are not prevented. Import on the serving loop (6a8931e962) yes; select spin under GIL (bf0ca9c4ff) partly. Loop-stall dumps `tofu_faulthandler_43101.log` (no) and `_120472.log` (partly). |
| resource leaks | 2 | partly | RAII closes descriptor leaks (419fa064c9). |

Totals: 9 yes, 9 partly, 460 no. Limits:

- Commits are classified by keywords plus about 70 manual corrections;
  expect ±20 % per class.
- Logs cover only 1.5-3.5 days of the 14.
- The GIL attributions are inferred from stack shape.

### Limits

- Retention makes the "7 days" window 3.9 days (2026-11-15 to 11-18).
  Stall-free and current-code cohorts are reported separately because code
  changed fast in this window (execution shards 11-17, round-floor work
  11-18).
- **Model streaming includes client-side stream parsing.** The per-request
  bound (22.2 ms) comes from a stub with tiny responses, so the Python cost
  of parsing long real streams is not included in it. In the profiles,
  provider stream ingest was 0.2 % of execution-host samples and 6.6 % of
  API-host samples.
- **Swarm child splits are partial.** Model and tool time cannot be
  separated inside a child round, and the split comes from a 48-child
  subsample.
- **`poll` time is a wait, not work.** It is a wait on another agent or a
  background shell command; the waited-on shell time is not reattributed
  to the shell family.
- **Storage time is not a disjoint component.** It is shown inside the
  harness and startup buckets plus the completion persistence stage.
  Shard-side storage RPCs are not exported.
- **Idle shard figures come from the private instance.** It spawns no
  shards at idle; the live pool has 28.

## Core loop prototype

Question: how fast is Tofu's hot round when it is written in Rust and sends the
same bytes, and how much of the difference is the language rather than doing
less work? Measured 2026-11-18 at HEAD `235644fae3` (Python arm; the prototype
merged onto `2fcdfae69a`, which changes no round code). The prototype is
evidence only: nothing in the product links, spawns or imports it, and no
server was restarted.

- Code: `packages/tofu-core-rs` (standalone manifest/lock like
  `packages/tofu-db`, which it depends on for the storage client protocol).
- Drivers: `benchmarks/harness_perf/rust_prototype.py` (Rust arm and the
  Python concurrency arm) and `benchmarks/harness_perf/byte_identity.py --rust`.
- Data: `output/rust_rewrite/prototype/` (`summary.json` from `analyze.py`;
  `abab*/pair*/<arm>/result.json`, `extra/`, `byte-identity/`, and the
  `abab.sh`/`extra.sh` schedules that produced them).

### Short answer

- **Round overhead** over 12 quiet ABAB(C) pairs with the same pinned engine
  (median / p95, ms):

  | scenario | Python HEAD | Rust, same durable work | Rust, lean durable work | codex |
  |---|---|---|---|---|
  | `noop` steady round (n=912 each) | 47.3 / 58.1 | **7.0** / 10.0 | **3.3** / 4.8 | 38.3 |
  | `true` round (n=240 each) | 58.7 / 88.2 | **7.0** / 10.1 | **4.8** / 6.7 | 187.9 |

  Rust was faster in every pair. The wire bytes are identical to Python's on
  all 75 recorded requests on all three wire faces (OpenAI Chat, Anthropic
  Messages, Responses).
- **Language versus design.** Python's mean `noop` round is 49.3 ms. About
  14 ms of it is work the prototype never does (27 % of Python's Turn-thread
  samples). The remaining 35 ms is work both versions do, and Rust does it in
  7.05 ms. Durable commits make up 5.1 ms of those 7.05 ms. Of the 45.9 ms
  between Python and lean Rust:
  - about **28 ms (61 %) is the language and runtime** doing the same
    operations;
  - about **14 ms (31 %) is computation the prototype skips**;
  - **3.6 ms (8 %) is less durable work.**

  The full breakdown is in [Language versus design](#language-versus-design).
- **Concurrency.** Measured over 2 Turns × 20 rounds per session (median /
  p95, ms):

  | sessions | Python HEAD | Rust parity | Rust lean |
  |---|---|---|---|
  | 1 | 63.2 / 74.8 | 9.2 / 14.0 | — |
  | 20 | 91.9 / 138 | 32.9 / 46.4 | 10.4 / 22.1 |
  | 100 | 631 / 857 | 146 / 208 | 72.6 / 134 |

  With many sessions the Rust round is bound by the storage engine: at 100
  sessions, 137 ms of the 146 ms parity round is waiting on commits.
- **Memory.** Per-session RSS is about **200 MB in Python** (one forked
  execution shard per session: 4.1 GB peak at 20 sessions, 20.6 GB at 100).
  In **Rust it is about 1 MB**: the whole process peaked at 38 MB with 20
  sessions and 102 MB with 100.
- **Cold start.** The Rust process goes from launch to its first provider
  request in **15.7 ms** (median of 5). That includes connecting to storage,
  creating the Turn, claiming it and starting the attempt. Its peak RSS is
  6.6 MB. Python's execution host takes 7.7 s to become ready and the API
  host 2.2 s; the storage engine is 0.64 s for both arms.

### What the prototype does

One session is one real Turn. It goes through the same TofuDB operations as
the Python worker, sent through the engine's own storage.v2 framing types:

- `tofu_db::protocol::{read_frame, write_frame, Hello, Request}` and
  `tofu_db::daemon::derive_auth_token`; no storage was reimplemented.
- Setup: `turn.create_pair`, then `turn.attempt.dispatch_worker` and
  `worker_job.claim_next` (retained claim, fencing token), then
  `turn.attempt.start`.

Each round then does the following:

1. **Request build.** Rebuild the canonical body from the stored history
   (`canonical.rs`, the `build_body` subset):
   - API-field strip with same-role seam hints;
   - blank text-block folding;
   - tool-call wire-shape healing;
   - orphan repair;
   - ghost-assistant drop;
   - same-role merge;
   - empty placeholders;
   - canonical key order.
2. **Translate and seal** (`wire.rs`):
   - tool preflight: root `oneOf`/`anyOf` relaxation and the
     `decoded_result_schema` description;
   - translation to the selected wire face;
   - compact sealing.
3. **Provider.** POST to the harness stub over keep-alive HTTP/1.1 and decode
   the SSE stream (`provider.rs`).
4. **Raw archive.** `raw_archive.put` (zlib level 1, SHA-256) from a writer
   thread, as `lib/raw_archive.py` does.
5. **Tool intent.** Commit the intent before the tool runs:
   `turn.event.record_batch` with `model_request_complete` and `tool_start`.
6. **Tool.** Dispatch `read_files` (the harness's `noop`) or
   `run_command true` (`tools.rs`).
7. **Receipt.** Commit the durable receipt (`round_usage` and `tool_result`)
   before the next request.

The Turn ends with a terminal `turn.event.record` and `worker_job.complete`.
After every Rust run the driver reads each Turn back through the Python
storage client (`durable_check` in `result.json`). In every pair, all 28 Turns
were `completed` and each Turn had exactly one tool round per tool request,
each with its receipt.

There are two event modes:

| mode | per-round work | durable reads and writes |
|---|---|---|
| **parity** | repeats the Python round's durable work, from the storage trace of a Python run | 5 boundary reads (`conversation.get`, `turn.get`, `turn.attempt.get`, two `record.get`), pipelined in one exchange; the per-request `turn.attempt.start`; 14 projection events in 3 commits |
| **lean** | keeps only the durability contract | tool intent before its effect and the receipt before the next request: 4 events in 2 commits |

The two Rust arms differ only in event mode, so their gap is the cost of the
extra durable work at Rust speed.

The request uses the replay corpus's longest system prompt and the corpus
schemas of the 26 tools Tofu offered on its round-0 request. A request was
55-61 KB, against Python's 70 KB (Python also carries its runtime context
blocks).

### Wire-byte identity

`byte_identity.py OUT --rust BIN` sends each of the 25 recorded requests, on
each of the three wire faces, through both Python (`build_body` +
`prepare_request`) and `tofu-core-rs byte-identity`. It compares the sealed
payload bytes, the URL, the headers without the trace id, and the offered tool
names:

- **75/75 identical** (7.2 MB of payload), rerun 2026-11-18 after the route
  port below.
- Python's own line file is unchanged before and after the merge.

The same 75 requests now also run in `tests/recorded_golden.rs` on the
remote build node. `scripts/gen_recorded_golden.py` records only oracle
payload digests, lengths, URLs and headers; inputs reference the existing
normalized replay corpus rather than duplicating transcripts. This check
is included in `scripts/differential.py`, whose cargo test/clippy steps use
`scripts/remote_cargo.py` and whose fmt step stays local.

Route corpus (2026-11-18). `scripts/gen_wire_golden.py` runs synthetic
histories (a plain history, a tool loop ending on tool results, a trailing
assistant turn) through `build_body` + `prepare_request` for 53 cases covering
the routes Tofu uses, and `tests/wire_golden.rs` requires identical payload
bytes, URL and headers for each:

- the three generic wire faces;
- FRIDAY's Anthropic face: Claude with cache markers (system, last tool,
  tail hoisted onto the `tool_result` block), extended-TTL markers with the
  `anthropic-beta` header, the prefill conversion, the 448-byte tool-name
  budget (hidden only behind `search_tools`/`execute_tools`, never below the
  floor, not applied without the discovery pair or for Kimi), Kimi K3 without
  markers and with the MFJS schema projection, and `Mt-Session-Id`;
- FRIDAY's OpenAI face: GLM, DeepSeek and Qwen with markers (the tail marker
  walks back past tool messages), Claude on that face, Doubao and MiniMax
  without markers and with collapsed text content, a route that declares
  `chat_cache_breakpoints` unsupported, Kimi K2.7 Code with MFJS;
- FRIDAY's Responses faces (Kunlun, tokrouter): temperature omission and
  declared reasoning replay (`reasoning.context`, `include`);
- CatPaw: the declared system identity and `X-Working-Dir`.

The gateway traits come from the tracked `bundles/yourprovider/gateway_profiles`,
recorded in the fixture and read by the Rust port as data (`route.rs`), so no
hostname is compiled in. `tests/llm/test_core_rs_wire_oracle.py` fails when
the Python oracle changes a recorded byte; `scripts/differential.py` runs that
check, the cargo tests, clippy and fmt as one gate.

Types were not generated: no generator emits the canonical request or wire
shapes. They are Python code paths, not contracts. The generators that exist
(`gen_rust_chat_wire_oracle.py`, `gen_rust_execution_contract.py`) produce
oracles and constants for the `tofu-runtime` subset. The prototype therefore
ports the Python emitters by hand and uses Python as the oracle.

**Mergeable tranche, not a cutover claim (2026-11-18).** The catalog sweep
below verifies text-only histories, API-key authentication, the selected
fixture route/profile, and the listed reasoning arguments. It does not prove
all capabilities of a catalog model, all installed registrations, or every
route available to that model. Images, reasoning history, OAuth, named choices,
native lanes and cutting Tool Search budgets remain cutover gaps even when a
catalog model uses them. No live provider selection or execution is switched.

Registered catalog and reasoning (2026-11-18). Every model of the maintained
seed catalog (`current_model_defaults()`, the exact seeds
`lib/model_routing/registration.py` consults: 206 models after the GPT-6.2
seed update) has a path in this tranche, not full capability coverage.
Registration also admits independently configured profiles and routes; those
are not enumerated by `current_model_defaults()`. `scripts/gen_catalog_golden.py` runs each on
its home route (Claude and the K3 line on FRIDAY's Anthropic face, GPT
reasoning models on Kunlun Responses, Mistral on its official API, the rest
on FRIDAY's OpenAI face) with the catalog profile resolved against the face,
under 25 reasoning settings: default, the enablement toggle, every depth
(off, lowest, enabled, default, minimal to ultra), every exact effort (none
to ultra, `no_think`) and two explicit thinking budgets. That is 5,150 cases:
2,856 sealed payloads (URL, headers, SHA-256) and 2,294 oracle exceptions
(`ReasoningContractError` kinds and `ProtocolConversionError`), all
reproduced. `tests/catalog_golden.rs` enumerates the catalog from the fixture
and fails on any `Unsupported`. The wire corpus adds 28 full-byte
`catalog-*` cases for the same paths. What these cover:

- the profile compiler (`reasoning.rs`: `capture_intent`, `native_intent`,
  `resolve_reasoning_control`, preset clamping, `_apply_sampling`, budgets)
  for `thinking_type` (adaptive and display, `keep`, default budgets),
  `reasoning_effort`, `enable_thinking` and `reasoning_split`;
- the legacy boundary that the GPT-5.6/6 seeds reach, because they declare no
  reasoning support, via `gpt_reasoning_effort`;
- emitters: Anthropic `output_config.effort`, and Responses `reasoning.effort`
  with the selected route's exact-effort check;
- Gemini thought-signature injection, and tool-call ID contracts with
  respelling (Mistral's nine alphanumerics; Claude behind Chat/Responses),
  built on a `creator_family` port (`family.rs`, `call_ids.rs`).

Tool schemas (`scripts/gen_tool_schema_golden.py`, `schema.rs`): the whole
`sanitize_wire_tools` path is ported, covering the root relaxation (every
model), Kimi's nested rewrites (`allOf`, `oneOf`, `const`, type arrays,
`type` beside `anyOf`), MFJS `$defs`/`definitions`/`$ref` (recursive
included), and the final `mfjs_schema_error` validation with Python's drop
and raise outcomes. The corpus is 161 real built-in tools plus 90
hand-written schemas on six models (1,506 checks), plus 12 full requests.

Tool Search (`scripts/gen_tool_search_golden.py`, `tool_wire.rs`): the
`project_tool_wire` catalog projection is ported. The goldens use bodies
built by the real `attach_tool_request_metadata` over an assembled 54-tool
catalog, and cover local/full/off, fail-open, pins, direct and background
tools, MCP withholding, local programmatic and multi-agent projections, and
the tool-name budget floor. That is 31 byte-identical cases plus 3 refusal
cases, on the Anthropic, OpenAI and Kunlun faces.

Remaining `Unsupported` paths and separately owned cutover gaps. These ARE
reachable features of registered models; their absence from the synthetic
catalog history is not evidence that Tofu does not use them. The P4-wire
owner retains responsibility for closing these before a live cutover:

- images, and replayed reasoning or native sidecars in history
  (`reasoning_content`, `_responses_items`, `_anthropic_content_blocks`);
- legacy, name-based reasoning without a declared profile for families other
  than GPT-5, plugin dialects, and `extra` fields other than the wire
  spellings and sampling;
- inference-profile features other than `cache_affinity_mt_session_id`,
  `chat_cache_breakpoints` and `responses_reasoning_replay` (so no declared
  Responses summary, prompt cache or native programmatic/multi-agent lanes),
  and GPT-5.6 Responses without a route profile;
- non-ASCII model ids (name-based knowledge uses ASCII matchers);
- tool-name aliasing, OAuth headers and Claude cloaking;
- a named `tool_choice` on the Anthropic and Responses faces, `response_format`
  and `stop`;
- a Tool Search schema budget that could cut (Python counts tokens with
  tiktoken), `code_api` exposure, and unknown Tool Search fields;
- tool rows without a `function` object, non-string tool names, and a
  non-object `decoded_result_schema`.

Python exceptions are reproduced as `canonical::Error::Raised` (class and
`str()`), separate from `Error::Unsupported`.

The `parity` CLI now routes its `provider_wire` boundary through
`wire::prepare_request_json`, including reasoning and `body_extra` fields.
`tests/parity_cli.rs` executes the actual binary and checks propagation and
explicit refusals, including OAuth (formerly silently ignored), image and
reasoning history, native lanes and a cutting schema budget. Unsupported
boundaries still return `unsupported`; the old storage handler is not imported
because this checkout lacks its `store::RequestFrame` API. This is provider-wire
reconciliation, not a storage-parity implementation.

#### P4-wire cutover ownership and exit criteria

Owner of every provider-wire row: **P4-wire**, not model registration or the engine worker.
The separate `storage_request` boundary is **unported**, owned by **P3 storage
integration**: reconcile the public storage.v2 client interface and add actual
storage-boundary parity tests before enabling it. Provider-wire success does
not certify storage parity; the CLI must keep refusing this boundary meanwhile.
Status: **open; blocks live cutover**. The mergeable tranche is useful as a
parity-tested library; it is not permission to select Rust for production.

| Used feature outside the tranche | Required evidence before closing |
|---|---|
| Images and vision fallback | Python-oracle requests for native image input, tool images, text projection, resolution/validation failures and model switches; no filesystem/network side effects silently skipped |
| Reasoning history / continuity | Signed and unsigned Claude, DeepSeek required replay, Gemini real signatures, Responses encrypted items and foreign-lineage retirement, with full tool-loop round trips |
| OAuth and subscription routes | Header/cloaking and Codex request profile parity, credential-free fixtures, rejection of unsupported auth rather than API-key fallback |
| Named tool choices | All three protocols, missing/withheld tool, MCP choice removal, parallel-call discriminator and schema restoration |
| Native Responses lanes | Summary/cache/compaction, native programmatic and multi-agent fields plus response attribution under declared route capabilities |
| Cutting Tool Search schema budget | Exact Python token-count and compaction/removal ordering across pin/floor/MCP/PTC combinations, including deterministic tokenizer identity |

**Scoped acceptance.** This tranche integrates checkpoint
`17946ddcca780560fff7759e458a1eae5f7fb642` with the current branch, retaining
the text/API-key sweep, the original 25 request and 88 stream cases, all
75 recorded wire requests, and the separate Python SSE/MiniMax fixes. It
replaces full-capability acceptance for this tranche only; the overall Rust
cutover remains open. Unchanged broad-suite evidence is reused rather than
rerun. No new capability or live switch is part of this integration.

**Decision.** Keep these gaps separately visible and fail closed at the wire
boundary. Do not remove refusal guards merely to make a catalog sweep pass.
**Alternative rejected:** interpreting one text-only history for each model as
complete model support; it misses feature combinations Tofu actually sends.

`TOFU_CACHE_MID_MODE` and `TOFU_CACHE_MARKERS_NONCLAUDE` are taken at their
defaults (no mid-history anchor; non-Claude markers by model family).

### Stream parse identity

`tofu_core_rs::stream::parse(protocol, model, trace_id, body)` ports
`SSEAccumulator` with the `SSEByteStream` framing and the Anthropic and
Responses translators. It returns either the exception Python raises (class,
status, gateway and quota flags, reason, message) or the finished result:
message, provider and compatibility finish reasons, `[DONE]`/finish evidence,
malformed-frame count, stream state, `_model_refusal`, `is_model_refusal`,
the canonicalized usage, `normalize_usage`, `usage_cache_convention` and
`split_input_tokens`. `scripts/gen_stream_golden.py` feeds 213 raw SSE
bodies (the original 88 are unchanged) through the Python byte path, and
`tests/stream_golden.rs` requires equal projections. Token-limit learning,
which mutates process-global state in Python, is recorded as the
`learned_limit` call (`{model, limit, route_key}`) on the raised error. The
corpus includes:

- in-stream 429s inside HTTP 200 bodies (`http_code`, `Status Code: 429` in
  the text, quota phrases, shared-limit and insufficient-quota bodies), 503
  gateway throttles, retryable overloads, and Anthropic `rate_limit_error` /
  `overloaded_error` events;
- `content_filter` finishes on Chat, Claude `refusal` with `stop_details`
  (signalled through `_model_refusal`) and Responses `incomplete`;
  `is_model_refusal` holds only for a verified finish;
- usage normalisation where `prompt_tokens` includes the cached prefix,
  under each vendor spelling (`cached_tokens`, `effectiveCachedTokens` beside
  a pinned `cache_read_tokens: 0`, `prompt_tokens_details.cached_tokens`),
  the full-cache equality case, and Anthropic residual input;
- premature close, malformed frames, empty stops, gateway-limit text,
  tool-call finishes without payload, MiniMax `<think>` splitting and SSE
  framing edge cases;
- the in-stream error branches: subscription quota with `resets_in_seconds`,
  `invalid_encrypted_content` on Chat and Responses, the HTTP 400
  classification chain (route missing, token limit, thinking-block binding,
  unsupported parameter, image, prompt too long, stream-only, vendor
  transient), sensitive-words refusals, route-unavailable 5xx,
  protocol-unsupported 404, and non-text or non-scalar error fields;
- reasoning streams for every wire family: Responses reasoning summaries and
  items, `program_output` follow-ups, callers and multi-agent attribution;
  Anthropic `thinking`, `redacted_thinking` and `signature_delta`, plus
  `server_tool_use` and `compaction`; and Chat `reasoning_content` from
  Claude, DeepSeek, GLM, Qwen and MiniMax `reasoning_split`, plus Gemini
  `thought_signature`.

The corpus exposed one oracle defect, fixed in Python first: a gateway-limit
stop without a usage frame raised `TypeError` in `finalize`
(`tests/llm/test_gateway_limit_response.py`). These still refuse with
`Unsupported`:

- numbers beyond i64, and `int()` of non-ASCII or underscored strings;
- bodies that are not valid UTF-8, or JSON that serde and CPython parse
  differently (NaN, lone surrogates, deep nesting);
- floats inside a re-serialized provider error;
- `repr()` of text outside the verified printable set;
- a subscription cooldown from a future `resets_at` (Python reads the wall
  clock).

The private replay carriers Python attaches to the message
(`_responses_items`, `_anthropic_content_blocks`) are not projected.

Python defects exposed by the corpus are fixed in separate commits with
failing-first regression tests (`tests/llm/test_sse_field_regressions.py`):

- SSE status and tool-call index parsing validate decimal digits before
  integer conversion; superscripts such as `²` no longer crash the stream.
- MiniMax preserves raw usage but promotes a nested reasoning-token count
  only from an object and only when it is a positive number (not a boolean).

The Rust parser and oracle expectations follow these fixes; the original
88 stream cases are unchanged.

A rewrite needs these as contracts first: see
[Contracts as the single source of Rust types](#contracts-as-the-single-source-of-rust-types).

### Method

The method is the same as Round floor IV in `docs/HARNESS_PERFORMANCE.md`:

- `round_profile.py --no-spy` boots the pinned engine, the execution host and
  the API host against the zero-latency stub. `rust_prototype.py rust` boots
  the same pinned engine (`_pin_engine`, same binary and digest) and stub.
- Both run `noop` (4 Turns × 20 read rounds) and `true` (20 Turns × one
  `run_command true`) after the same warm-up.
- Overheads come from the stub's own records through
  `run.session_metrics`/`run.summarize`, so both arms are measured by the
  same clock and code.
- Each arm runs in a fresh root.
- Order is Python, Rust parity, Rust lean, repeated: 10 pairs, then 3 more.
- A pair starts only when the 1-minute load is below 40 on this 180-CPU host.
  Pair 1 is excluded because the load reached 141 during its Python arm
  (p95 545 ms). That leaves 12 quiet pairs.

Per-pair medians (ms; Python / parity / lean):

| | `noop` median | `true` median |
|---|---|---|
| range over the 12 pairs | 44.6-49.0 / 6.5-7.4 / 3.0-3.5 | 58.0-61.4 / 6.5-7.2 / 4.7-5.1 |

Concurrency used the `shell` scenario (2 Turns × 20 `run_command` rounds per
session):

- Python HEAD ran `run.py`'s `concurrent_phase` (`rust_prototype.py
  python-concurrency`).
- Rust ran one process with one thread per session.
- Each was run once, at load 15-33, so treat the 100-session figures as one
  observation.

### Profile: where the Rust round's time goes

Mean ms per round from the binary's own stage timers, pooled over all pairs.
"Unattributed" is the stub's measured gap minus the timed stages: loopback
transfer, the inline zlib and SHA-256 of the raw-archive record, and history
bookkeeping.

| stage | parity `noop` | parity `true` | lean `noop` | lean `true` |
|---|---|---|---|---|
| durable intent commit | 2.73 | 2.24 | 1.09 | 1.07 |
| durable receipt commit | 2.38 | 1.61 | 0.93 | 0.73 |
| per-request `turn.attempt.start` | 0.26 | 0.18 | — | — |
| boundary reads (5 in one exchange) | 0.19 | 0.23 | — | — |
| request build from history | 0.22 | 0.17 | 0.22 | 0.24 |
| translate + seal | 0.53 | 0.56 | 0.52 | 0.61 |
| tool | 0.02 | 1.68 | 0.02 | 1.66 |
| SSE decode, request send | 0.03 | 0.04 | 0.03 | 0.04 |
| unattributed | 0.70 | 0.68 | 0.64 | 0.64 |
| **round (stub-measured mean)** | **7.05** | **7.39** | **3.44** | **4.99** |

Where the remaining cost goes:

- **Durable commits are 73 % of the parity round and 59 % of the lean
  round.** The receipt commit is 1-2.4 ms on its own. That time is the
  engine's WAL barrier plus one loopback exchange, which is the price of the
  durability contract.
- **Translation and sealing take 0.5-0.6 ms** for a 58 KB request, and
  request build takes 0.2 ms. The prototype rebuilds the whole body every
  round with no memoization, so this is the cost without Python's incremental
  caches.
- **Network:** sending the request and decoding the stream take under
  0.05 ms. The rest of the loopback transfer falls inside "unattributed".
- **`true`:** forking `/bin/sh -c true` costs 1.7 ms.

Under load the commits dominate further. At 100 sessions the parity medians
are:

| stage | ms |
|---|---|
| intent commit | 70.5 |
| receipt commit | 35.6 |
| `turn.attempt.start` | 30.7 |
| translate + seal | 0.87 |

The engine used 4.7 cores and the Rust process 1.8.

### Language versus design

The Python reference is the Turn-thread profile of the same build
(`extra/python-profile`, `round_profile.py` with py-spy). Its 26 named
components (the finalize and start-up buckets are left out) are scaled to the
unprofiled mean of 49.3 ms. Components fall into three groups.

**Operations the Rust round skips (≈14.1 ms of Python):**

| component | ms |
|---|---|
| tool pipeline CPU (argument repair, result envelopes, budgets, settlement) | 4.5 |
| context composer, hygiene and inbox | 2.0 |
| post-tool state snapshot | 2.0 |
| slot adaptation, admission count, dispatcher pick | 1.6 |
| round close | 1.0 |
| prompt admission count | 0.9 |
| cache-break accounting | 0.6 |
| MCP relay RPC | 0.5 |
| stream tool callback | 0.4 |
| per-round thread creation | 0.3 |
| other synchronous RPCs | 0.3 |
| loop breaker | 0.15 |

Inside `prepare_request` the prototype also skips the token recount, the
semantic and prefix identities, the wire fingerprints and lineage sealing.
These are not separable in the profile, so they are counted as same work
below. That makes the language share an upper bound.

**Operations both versions do (Python ≈35.1 ms, Rust 7.05 ms):**

| component | Python, ms | Rust (parity), ms |
|---|---|---|
| request build + translate + seal | 6.0 + 2.4 | 0.75 |
| provider send / headers, body read, SSE parse | 4.7 + 0.1 + 2.0 | 0.05 + part of 0.7 unattributed |
| tool execution (`read_files`) | 4.4 | 0.02 |
| boundary reads (steers / turn head, working facts, requirements) | 2.7 + 2.6 + 1.1 | 0.19 (pipelined) + 0.26 (attempt start) |
| event barrier, event append | 3.2 + 2.0 | 5.1 (commits) |
| other loop work | 4.1 | — |

Same-work speedup: about 5×, 28 ms.

**Less durable work (parity → lean):** 3.6 ms. This is the boundary reads,
the per-request attempt start, and 14 events in 3 commits against 4 events in
2.

What these numbers mean:

- The language is most of the gain only per round on an idle host, because
  the work removed by design costs little once it is in Rust.
- Doing 10 fewer events and one commit less per round halves the Rust round:
  7.0 ms becomes 3.3 ms.
- Python spends 5.2 ms on event delivery (barrier plus append). Rust spends
  5.1 ms committing a similar volume synchronously. **Durable writes cost the
  same in both languages, because the engine is already Rust.**
- Under concurrency the per-session memory (≈200×) and the 100-session round
  (631 → 146 ms) are runtime and design effects that a per-round profile does
  not show:
  - Python needs a forked interpreter per session to escape the GIL.
  - Rust's 100-session round is bound by the engine, and lean halves it
    again (72.6 ms).

### Limits

- **Partial port.** The prototype ports the round's hot path only. It has no
  context composer, compaction, steering, approvals, MCP, sandbox, result
  budgets, retries, cancellation, recovery or push delivery. Each of these
  costs Python time today, and some of it will come back in a complete Rust
  implementation. The "skipped" bucket is therefore what this prototype does
  not do, not what a rewrite can delete.
- **Parity events are reproduced, not derived.** Their payloads follow the
  Python trace's shape (operation, patch base/target revisions, task event)
  but carry less projection detail. Commit counts and sizes are of the same
  order, not byte-equal.
- **Byte identity covers the corpus only.** The 75 requests do not exercise
  the refused features listed above.
- **Concurrency and cold start were run once each.** Rust sessions share one
  process and a single setup lock around claim. Python sessions are full
  Turns through the API host.
- **Python profile shares are from a py-spy run** (perturbed). Milliseconds
  are scaled from the unprofiled ABAB mean.

## Phase 5 progress: leaf owners

Two Phase 5 leaf owners now have complete Rust ports. Neither depends on Phase 0, and
neither is wired into the product yet. The Python owners remain the oracle and
the production path until a cutover deletes them. Status 2026-11-18.

| Owner | Rust port | Python binding (parity seam) | Parity evidence |
|---|---|---|---|
| MCP client (`lib/mcp`: bridge call path and lifecycle, `queue_policy.py`, `stdio_environment.py`, `transport.py`, `result_content.py`, `documents.py`, `bridge_relay.py`, `health_probe.py`, `browser_credentials.py`, `client/_install.py`, `client/_vendor.py`, `vendored.py`, `tool_search.py`, `progressive.py`, `connector_guidance.py`, `updates.py`, the registry edition filter) | `packages/tofu-mcp` (rmcp 3.11.0 client; binary `tofu-mcp-host`) | `lib/mcp/rust_host.py` (`RustMCPHost`, `RustMCPBridge`, host-to-binding callbacks) | `cargo test` (104); ten `tests/mcp_protocol/test_rust_*.py` suites (729), and `test_rust_coverage_map.py` proves every `tests/mcp_protocol` oracle file is Rust-backed |
| Channel stdio host (`lib/channels/protocol/stdio.py`) | `packages/tofu-channel-stdio` (std threads; binary `tofu-channel-stdio-link`) | `lib/channels/protocol/rust_stdio.py` (`RustStdioLink`) | `cargo test` (32, including a 26-step differential against Python's `StdioLink`); `tests/channels/conformance/test_rust_stdio_link.py` (31 pass and 3 n/a, matching `reference-stdio` in `baseline.json`, plus the malformed-response cases shared with Python) |

### MCP client: what the port keeps

`Bridge::call_tool` (`packages/tofu-mcp/src/bridge.rs`) runs the oracle's
gates in the oracle's order:

1. Cancellation, name parse, server and tool lookup, and the user-disabled
   refusal.
2. The owner Hope queue-policy guard, before any wake or send. It covers
   `submit_job`, `confirm_rerun`, and `catalog_write` with `job_clone`,
   `job_rerun` or `codelab_create`. It also covers servers launched as
   `hope-mcp` under any name.
   - The policy file is re-read on every call.
   - A missing, unreadable, invalid or unnamed file enforces exactly the
     contract's `m17` entry.
   - A refusal writes the `mcp_queue_policy_refusal` audit record.
3. Wake of a parked server, then schema coercion.
4. The deadline: an explicit override, else the server budget, extended by a
   declared bounded wait.
5. The degraded fast-fail.
6. Health settlement: a timeout adds to the streak, and a peer answer resets
   it.
7. Replay after a dead transport, only for an unchanged enabled read
   declaration or a trusted opt-in. A dead transport's catalog is re-read.

The stdio child environment (`stdio_env.rs`) follows the oracle step by step:

1. The inherited environment, minus ambient credentials and storage handles.
2. The overlay, with `vault:` references resolved fail-closed.
3. The owner id and its Hope policy path.
4. `bind_runtime_base` (`TOFU_DATA_DIR`).
5. Proxies, including the proxy-pool fallback.
6. The tool-cache redirect of the cache and data directories (`XDG_DATA_HOME`
   and `UV_*`), the supply cutoff, and the `UV_PYTHON` pin.
7. The tool PATHs. An explicit overlay `PATH` wins last.

Children run in their own session with `oom_score_adj=1000`.

Credential redaction (`transport.rs`: `redact_config`, `redact_url`,
`scrub_text`) filters every connect error and SDK error text.

Result projection (`result.rs`) reproduces the display text, structured JSON,
document, retention receipts, ordered block descriptors and image carriers.

`relay.rs` is wire-compatible with `lib/runtime/loopback_relay.py`. An
unchanged Python `RelayedMCPBridge` (an execution shard) reaches the Rust
owner for the catalog, list and call verbs, including kept session
connections. Cancellation on caller EOF and the failure categories are
preserved.

**Differential evidence.**

- **The recorded session.** `tests/fixtures/mcp_sessions/replay_server.py`
  replays a recorded-shape session (`hope_like.json`): a hope-mcp-like catalog
  plus every result shape the bridge projects.
- **Live comparison.** The Python bridge and the Rust client connect to that
  server over real stdio. `test_rust_mcp_parity.py` compares, byte for byte:
  each outcome's text, runtime payloads and failure category; what reached the
  server; the catalog rows; and the audit records.
- **Pure owners on wider inputs.** The same file also compares projection
  limits, the environment, redaction, coercion, policy validation messages and
  CLI submission parsing.
- **Without importing Python.** `recorded_session.rs` checks the Rust client
  against the Python bridge's recorded outcomes
  (`packages/tofu-mcp/tests/fixtures/hope_like.golden.json`, regenerated by
  `packages/tofu-mcp/scripts/gen_session_golden.py`).

### MCP client: the owners beyond the call path

Every `lib/mcp` behaviour is ported. Python-owned state is never copied: the
host asks its binding through host-to-binding calls (`callback.rs`;
`{"call"}` answered by `{"reply"}`, thin handlers in `lib/mcp/rust_host.py`
`DEFAULT_CALLBACKS`) for the network route plan, stored server rows (written
by a locked compare-and-set, `mcp.update_row`), the vault, browser devices,
declarations and the live registry `CATALOG` (bundles extend and hot-reload
it, so a generated copy would drift).

| Owner | Rust | Oracle |
|---|---|---|
| Keepalive/liveness sweep, idle parking, `tools/list_changed`, reconnects and the breaker, first-connect retry, shutdown disposal, discover-first negotiation | `bridge/lifecycle.rs`, `rmcp_session.rs` (`ServerMessages` client handler) | `_keepalive_loop`, `_probe_liveness`, `_park_idle_stdio_server`, `_handle_server_message`, `_reconnect_server`, `_breaker_*`, `_disconnect_one` |
| Credential probes, renewal, follow-up notice/replay, browser credential learning | `health_probe.rs`, `browser_credentials.rs`, `bridge/credentials.rs` | `health_probe.py`, `browser_credentials.py`, `_run_cred_probe`, `_renew_expired_credential`, `_credential_followup` |
| Launcher install and vendored launch, pre-warm markers, npx reconciliation | `launcher.rs`, `vendor.rs` | `client/_install.py`, `client/_vendor.py`, `vendored.py` |
| Tool search, progressive disclosure, connector guidance, edition filter, catalog projections | `tool_search.rs`, `progressive.rs`, `connector_guidance.rs`, `registry.rs`, `bridge/catalog.rs`, `generated_pyunicode.rs` (CPython casefold/`\w`, `scripts/gen_pyunicode.py --check`) | `tool_search.py`, `progressive.py`, `connector_guidance.py`, `registry.is_opensource_build` |
| Upstream updates | `updates.rs` | `updates.py` |
| Streamable HTTP and registry fetches through the shared network authority | `network.rs` (`RoutedHttpClient`, rmcp's `StreamableHttpClient`) | `lib/http_transport/outbound_*.py` routing transports |

**Network authority.** Each HTTP request asks the binding for
`lib.proxy.network_route_candidates` (`network.route_candidates`) and reports
every attempt to `report_network_route` (`network.report`), so plan, health
and recovery stay with `docs/NETWORK.md`'s single owner. Routes are tried once
each in plan order; only a proven connection-setup failure (reqwest
`is_connect`: DNS, TCP, TLS, proxy connect) is reported failed and advances.
Any response, a truncated body or an ambiguous disconnect is never replayed;
exhausted routes are a typed connect failure. One idle-only client per proxy
URL, no request cap. Problem: the first port connected directly, outside the
route policy. Decision: route plan and health over the binding at the HTTP
transport boundary. Alternatives rejected: a Rust copy of the proxy/route
state (a second authority that drifts), environment proxies (no health or
failover), retrying inside the MCP layer (replays delivered requests). With
no binding (standalone use) the client takes one direct route and reports
nothing, the explicit composition port docs/NETWORK.md requires of standalone
SDKs; a failing binding fails the request closed.

**Test seams, not knobs.** Time-driven behaviour is driven by host methods
(`sweep_once`, `advance_clock`, `credentials.sweep`, `age_cred_probe`), never
by shortened intervals. Every interval, timeout and size is the oracle's
constant or environment variable (`lib/mcp/types.py`, `_WARM_SECONDS`,
`DEFAULT_ACTIVE_LIMIT`). An opensource export bakes the Rust edition default
(`registry.rs` `OPENSOURCE_BUILD_DEFAULT`) like `registry.py`'s.

**Coverage.** `tests/mcp_protocol/test_rust_coverage_map.py` reads a literal
`COVERS` dict from each `test_rust_*.py` suite and fails if any oracle file in
`tests/mcp_protocol` has no Rust-backed test, if a named test does not exist,
or if a suite never reaches the Rust client. Suites: `test_rust_mcp_parity.py`
(call path, queue guard, relay), `test_rust_results_parity.py` (results and
the display consumers), `test_rust_lifecycle_parity.py`,
`test_rust_credentials_parity.py`, `test_rust_launcher_parity.py`,
`test_rust_catalog_parity.py`, `test_rust_network_parity.py`,
`test_rust_updates_parity.py`. Files that test a Python layer consuming MCP
output (tool display, task gateway, declarations) run that unchanged layer
over the Rust client's output and require identical results; files whose MCP
dependency is one predicate (`test_brand_mascot_single.py`,
`test_audio_chat_has_text_companion.py`: `is_opensource_build`) cover exactly
that predicate. Not ported because they are not client behaviour:
`config.py` persistence/migration, Quart routes, Python SDK packaging pins,
and Python event-loop/thread internals (their Rust ownership equivalents are
tested instead).

**Known differences:**

- rmcp adds the 2026-07-28 inline protocol keys (`io.modelcontextprotocol/*`
  and `progressToken`) to every request's `_meta`. The trusted `tofu/project`
  meta still arrives unchanged.
- rmcp parses the 2026-07-28 discover and list results strictly: a peer that
  omits the schema-required `resultType`/`ttlMs`/`cacheScope` is refused; the
  Python SDK tolerates it. Neither client opens `subscriptions/listen`; both
  act on an unsolicited `tools/list_changed` in either era.
- A `NaN` in `structuredContent` or a wire snake_case field (`is_error`) is
  rejected or ignored by rmcp's typed models; pydantic accepts it, and also
  coerces a junk `readOnlyHint: "true"` to read-only.
- Wording differs for JSON syntax errors after `is unparsable:`, timeouts and
  non-object `structuredContent`; categories are identical.
- rmcp has no legacy HTTP+SSE client, so an `sse` server is refused with an
  explicit error. SOCKS routes need reqwest's `socks` feature (off).
- Reported route latency covers rmcp's whole attempt; the registry fetch uses
  a 10 s total deadline where httpx applies 10 s per phase.
- Lifecycle: a notification from a replaced session is ignored; a stuck close
  past the 5 s budget is dropped; malformed integer env values fall back to
  the default instead of failing at import.
- Launcher: Python's log lines are not emitted; install jobs and warms are
  retired on host shutdown instead of abandoned.
- Catalog: a parked server that wakes with an unchanged catalog keeps its
  generation (Python advances it once); Final_Sigma lowercasing and the
  `urlsplit` NFKC check follow Rust.

**Python oracle bugs found by the parity work (reported, not changed):**
`updates.compare_versions('1.0.0RC1', '1.0.0')` is `None` (case-sensitive
pre-release regex) and `check_server_update` turns that unknown into
`update_available: False`; `_vendor._prewarm_unreachable_reason` raises
`AttributeError` on a JSON marker that is not an object; the Python SDK drops
an unparsable reply, so a call with no deadline waits forever.

### Channel stdio host: what the port keeps

`packages/tofu-channel-stdio` ports `_Peer`, `StdioLink` and `serve`:

- One JSON object per line, in Python's byte format.
- Answers by id.
- In-order dispatch on one dispatcher thread, so a nested call cannot
  deadlock.
- The parse, invalid-request, method-not-found, invalid-params and
  internal-error answers.
- Every pending request fails with `internalError` when the binding closes.
- The adapter runs in its own process group, and its stderr is diagnostics
  only.
- `close` closes stdin, waits, then signals the whole group with SIGTERM and
  then SIGKILL, and always reaps it.

Method directions and error codes come from
`contracts/channel_protocol_v1.schema.json` at build time. The known
differences are listed in `packages/tofu-channel-stdio/AGENTS.md`.

Both bindings answer malformed responses the same way. A response with a
non-integer `id` matches no request; an error response whose `code` is not an
integer or whose `data` is not an object fails only its own request with
`internalError`, and the binding keeps answering. Python's reader thread used
to die on these, leaving the caller of an already-popped request waiting
forever; that was fixed with a failing-first test
(`test_a_malformed_stdio_response_fails_its_request_and_the_link_keeps_answering`),
and the Rust link now runs the same cases.

### Resource rule

Neither port sizes anything by the machine:

- `tofu-mcp-host` runs one current-thread tokio runtime and does no
  blocking-pool work.
- The channel link uses a fixed set of threads per child: reader, dispatcher,
  stderr drain and exit waiter.
- There is no CPU-sized pool, admission cap or memory refusal.
  `tests/meta/test_hardware_never_limits_work.py` passes.

## Phase 3a: Rust storage.v2 client

Status (2026-11-18): **implemented and differentially verified**, not yet
wired into the product. `packages/tofu-storage-client` (crate
`tofu_storage_client`) is the one storage client for every Rust phase: the
front door (P1c/P1d), the execution engine (P4) and the domain owners (P5).
No second client may be written. Like `tofu-core-rs`, it has its own manifest
and lock. It builds on the engine's own framing (`tofu_db::protocol`,
`tofu_db::daemon::derive_auth_token`) and never starts, selects or switches a
backend.

### What it covers

| Python (`lib/storage/v2_client.py`, `tofu_db/wire.py`) | Rust |
|---|---|
| `StorageV2Session`: Hello once, correlation IDs byte-identical, upload streams past `MAX_PAYLOAD_BYTES`, strict response-chunk reassembly, `request_many` in `SO_SNDBUF` segments, liveness probe, interrupt | `session.rs` |
| `SessionPool`: demand-created sessions, idle reuse bounded by the readiness budget (lean 4, `maximumConnections / 2`), 20 s freshness cap, never admission | `pool.rs` |
| `_call`: replay classification, busy schedule (16 attempts, 2 ms doubling to 250 ms), availability backoff (server hint, then 0.5 s doubling to 8 s, jittered), full wire deadline per replay, wait-while-alive through `bind_runtime_recovery` with scope fence, cancellation | `client.rs` (`RuntimeRecovery`, `Cancellation`) |
| `_to_storage_error`, `StorageError` taxonomy folding, `request_not_dispatched` | `error.rs` |
| `query`, `query_many`, `command` (committed-event envelope, notices), `maintenance`, `health` (own lane, `conversation.count` fallback), `metrics`, `worker_watch`, `conversation_watch`, `native_checkpoint_available/control` | `client.rs`, `checkpoint.rs` |
| `v2_semantics.py`: owner injection, bounded progress, snapshot settings filter, notice mapping | `semantics.rs` |
| `credential_redaction.py` and the vault/`server_config.json` known set | `redaction.rs` |

The client issues operations by name, so it covers the whole catalog.
`contract.rs` reads `contracts/storage_operations_v1.json` (hand-authored
since P0c) plus the `storage_v2.json` control operations, together with every
other constant it uses (owner-omitted operations, bounded progress, notice
topics, watch and checkpoint operations, private setting keys, the error
taxonomy). They are embedded at compile time, so a contract edit rebuilds the
crate. `contract_tests` and `test_rust_contract_projection_matches_python`
check these values against the Python projections and the catalog.

Verification: `cargo test` (30 cases, including a scripted peer for replay,
streaming, pipelining, health, recovery and cancellation),
`cargo clippy --all-targets -- -D warnings` and `cargo fmt --check`.
`tests/storage/test_storage_v2_rust_client_differential.py` runs one recorded
corpus through both clients. Each client gets its own real `tofu-db serve` in
an isolated data directory, behind a framing-aware fault proxy that injects
identical faults on both sides:

- **Disconnects:** before dispatch (once, and on every attempt) and after
  commit (the lost acknowledgement replays the receipt).
- **Timeouts:** a response delayed past the socket deadline, once and on
  every attempt, for both a query and a command.
- **Synthetic answers:** busy, retryable and non-retryable load shedding,
  `deadline_elapsed`, a scope mismatch, and an engine-only code.
- **Real quotas:** the billing scan bound, the paper anchor bound, and the
  swarm session capacity.

The corpus also covers the following, and every result and classified error
matches:

- receipt reuse and CAS conflicts;
- unknown and invalid operations;
- 9 MiB streamed upload and download;
- `query_many`;
- write-time redaction;
- health, metrics, both commit watches and checkpoint capability;
- calls after `close()`.

Two values are normalized: commit wall clocks (`*_at_ms`) and the watch
epoch, because each authority has its own.

### Write-time redaction stays in `command()`

**Problem.** Credential redaction runs inside the Python storage client's
`command()`. A Rust client without it would persist raw vault and provider
keys.

**Decision.** The hook stays at the same seam. `StorageV2Client::command`
passes every payload, with the client's authenticated owner, through the
installed `WriteRedactor` before anything is sent. `CredentialRedactor` is
the equivalent of `known_credential_redactor(owner_user_id=...)`:

- the same exact known set: vault entries, `server_config.json` provider keys
  and, for the owner, that owner's model-routing keys;
- the same 8-character floor and the same raw, `quote` and `quote_plus`
  variants;
- the same no-plaintext index: per variant a keyed digest (BLAKE3 here,
  keyed BLAKE2b in Python, so digests differ but the matches do not), plus a
  keyed hash of every aligned block;
- the same match selection (longest first, then earliest, no overlap, one
  pass), with lengths counted in characters as in Python;
- the same streaming form (`redact_stream`) for spooled output;
- the same cache: one entry per owner, rebuilt only when the vault,
  `server_config.json` or that owner's `model_routing_secrets.<owner>.stamp`
  changes. A failed file reload keeps the previous set; a failed owner read
  is retried on the next call.

It reads the vault itself: Fernet HMAC verification and AES-128-CBC with the
existing key file. Its output is compared with Python's on a shared vault,
`server_config.json` and a recorded set of owner keys, both for plain
redaction and for a command's stored value.

**The model-routing source is ported, read-only.** An owner's model-routing
keys come from the model-routing owner's decrypting read
(`lib/model_routing/known_secrets.owner_credential_keys`).
`ModelRoutingKnownSecrets` (`src/model_routing_secrets.rs`) is that read in
Rust: `model_routing.get` and `model_routing.secret.get` through the client it
is bound to, then `lib/secret_envelope.open_secret` (Fernet under
`TOFU_SECRET_ENCRYPTION_KEY` or `data/config/.secret_encryption.key`, then the
exact purpose/owner/record binding), with the same envelope, kind and
missing-secret rules. It never mints a key, writes a stamp or caches
plaintext. One deliberate difference: it does not re-run
`normalize_document`, so for a document Python would reject it still
recognises the keys (more redaction, never less).
`tests/storage/test_storage_v2_rust_client_differential.py::test_rust_model_routing_read_matches_python_for_an_owners_keys`
seals and commits real credentials through `ModelRoutingRepository` into one
TofuDB and checks that Rust's redaction, a written command's stored value and
the failure path (an unopenable secret keeps the previous set) match Python's.

**Fail closed.** `CredentialRedactor::new` requires an owner source, and
`command()` refuses a payload with `database_protocol_error` ("no write
redactor is installed") before sending anything when no redactor is
installed. Python's `command()` always redacts, so no Rust writer can store
unredacted output by forgetting the hook.

**Composition rule.** The Rust application calls
`StorageV2Client::install_credential_redaction(data/config)` once per client
(an `Arc`; the source holds the client weakly). Tool settlement (P4) reuses
the same `KnownCredentialRedactor` and does not implement a second matcher.
The Rust model-routing owner (P5) takes over `ModelRoutingKnownSecrets` with
the rest of model routing.

**Alternatives considered.**
- *Redact in callers.* Rejected: every new writer would have to remember it,
  which is why it moved into the client.
- *Keep vault decryption in Python and pass values across.* Rejected: Python
  would become a runtime dependency of every Rust write.

### Synchronous by design

**Decision.** The client is synchronous, and each call runs on its caller's
thread.

**Rule for async callers.** Run a call on a dedicated thread. Never use a
capped or CPU-sized pool: tokio's default 512-thread blocking pool queues
work, which the hardware policy forbids. Nothing in the crate is sized from
the host.

**Alternatives considered.** *An async client over tokio.* Deferred: the
engine's framing is synchronous (`Read`/`Write`). An async adapter can wrap
the same session later without changing the retry contract.

### Divergences and deliberate omissions

- **One Python fix.** The Python wire client used to send an operation over
  128 bytes or a command ID over 256 (or an empty one). The daemon answers
  such a frame by closing the connection. The client read that as an
  availability failure and replayed a request that can never succeed: with a
  runtime-bound client the wait never ended. `tofu_db/wire.py` now refuses
  these locally with the engine's own wording, from generated
  `MAX_OPERATION_BYTES` and `MAX_COMMAND_ID_BYTES`. Both clients raise
  `database_protocol_error`. Tested in `tests/tofudb/test_tofudb_v2_client.py`.
- **Not ported**, because it adds no behaviour a Rust caller needs:
  - the `send_query_many` split halves (a caller overlaps on its own thread);
  - the duck-typed response-frame admission hook (the default Python budget
    never rejects);
  - the asyncio event-loop guard (Rust has no implicit loop: see the rule
    above);
  - parsing of the `eventBatchComposition` readiness field
    (`readiness_budget()` returns the raw budget).
- **Logging and metrics** go through `StorageObserver`: per-call outcome,
  retries, slow waits, committed notices and committed operations. The
  composing process attaches its logger and histogram.

## Phase 5 leaf: tofu-search

Status (2026-11-18): two steps landed, not complete. The package's CPU-bound
text kernels, URL canonicalisation, authority classifier, citation parsing and
the HTML extraction that follows trafilatura run in Rust. Network, browser,
trafilatura's article pass, PDF, orchestration, engines, verticals, the
citation HTTP checks and MCP remain Python. Nothing was published; PyPI stays
at the owner's held version.

### What is ported

`packages/tofu-search/rust/` (crate `tofu-search-native`, PyO3 0.27, abi3 for
CPython >= 3.10) exposes `tofu_search._native`:

| Python owner | Ported functions | Python keeps |
|---|---|---|
| `lexical.py` | `tokenize`, `matching_text`, `subject_terms`, `query_subject` | nothing else in the module |
| `passages.py` | `select_relevant_passages` | link-inventory constant only |
| `search/dedup.py` | snippet-group membership, full-content mirror decisions, both shingle sets, `canonical_url_key` | row merges, listing attachment, logging |
| `search/rerank.py` | BM25 scoring (`_bm25_scores`) | authority priors, RRF fusion, entity/domain diversity |
| `search/authority.py` | `classify_authority`, `host_brand_labels`, `brand_tokens` (and through them `authority_label`) | IDNA 2003 encoding of non-ASCII hosts |
| `verify/parse.py` | `parse_bibtex`, `parse_references`, `extract_citations_from_text`, `extract_identifiers`, with the DOI/arXiv patterns of `search/vertical/{doi,arxiv}.py` | the `Citation` dataclass; `verify/citations.py` HTTP checks |
| `fetch/html_extract.py` | `_assemble_document`: `html.parser` + BeautifulSoup tree, `_sanitized_soup`, `javascript_notice`/`rendering_signals`, main region, fallback text, table/code/button supplements, page links (`urljoin`), link ranking, budget | trafilatura's article pass and `_prepare_article_tree` (lxml), code-blob, fragment and stream-restore paths, publication dates |

Through these names the kernels also serve `search/quality.py`,
`search/prefetch_gate.py`, fetch admission and the host's extraction windows.
Each module rebinds its public names to the native functions when
`_native_kernels.py` finds the extension, and keeps its Python bodies in
`PYTHON_KERNELS`: the fallback for the pure wheel and source checkouts, and the
parity oracle. A present but unloadable extension raises.

**Problem:** the 955 tests are mostly white-box: they monkeypatch Python
module attributes (`vertical.base.http_get`, transports, providers, clocks).
A Rust replacement of the I/O and orchestration layers would leave them
nothing to patch, so they could no longer serve as the oracle.
**Decision:** port the pure kernels first, bit for bit, behind the unchanged
Python names. The suite then runs against the native code without edits.
Porting I/O needs the black-box/white-box test classification from Phase 0
before the tests can judge a Rust implementation.
**Alternatives considered:** a Rust CLI or service that replaces the package,
which the white-box suite cannot test and which breaks the PyPI import
contract; switching the package's build backend to maturin, which would make
every `uv sync` of the monorepo workspace and every sdist install compile Rust.

Exactness rules the port keeps:

- Character classes and case maps (`\w`, `\s`, `isalnum`, `isdigit`,
  `casefold`, `lower` with final sigma, `re.I` folding for `ſ`, `İ` and `ı`)
  come from tables exported from CPython's `unicodedata` 15.0 by
  `rust/scripts/gen_unicode_tables.py`. Rust's standard library tables are
  newer and classify differently: combining marks, for example, are alphabetic
  in Rust but not `\w` in CPython.
- NFKC goes through `unicode-normalization` (Unicode 16). Code points that are
  unassigned in the interpreter's database are kept verbatim as opaque
  starters, so characters added after 15.0 normalize as they do in CPython.
- Offsets are code-point indices. The passage heap orders `-0.0 == 0.0` the
  way Python tuples do, and BM25 sums its terms in the caller's set-iteration
  order, so scores are bit-identical floats.

### Verification

- `tests/test_native_parity.py` has 16 cases. It compares every native
  function with its oracle over a corpus, seeded random text and 400 random
  code-point strings, covering notation, CJK, Greek sigma, ligatures, combining
  marks, unassigned and astral code points, and the dedup head/tail window
  above 28,000 characters. It found two defects during the port: a lone `"`
  in `query_subject` and Unicode 16 NFKC. After both were fixed, an ad-hoc
  fuzz of 12,000 cases, which is not committed, found no mismatch.
- `tests/test_native_packaging.py` checks that the maturin `[project]` table
  equals `pyproject.toml` and that the crate version tracks it.
- The suite now has 966 test functions (955 existing, plus 8 parity, 2
  packaging and 1 transport-check specification), collected as 1,658 cases.
  - **Installed native wheel**, outside the checkout (`tofu_search` from the
    wheel, `tests/` copied beside it): 1,648 passed, 8 skipped (opt-in
    Playwright, declared transport owners) and 2 failed. The 2 failures were
    the packaging cases, which read `rust/`, and that directory had not been
    copied into the staging tree. After it was copied, they and the parity
    cases passed (18 of 18).
  - **Pure checkout** (extension absent): 1,634 passed, 24 skipped (16 of them
    native parity), 0 failed.
  - The baseline had one deterministic failure, `test_plugin_contract`
    `[_worker_executor.py]`. It flagged a local queue named `requests` as an
    HTTP call and is fixed in this change. Under host load ≈ 170,
    `test_local_rerank_parent_lifetime`, `test_local_rerank_process`,
    `test_host_throttle`, `test_demand_workers` and
    `test_fetch_request_budget` fail intermittently with and without the
    extension, and pass when rerun serially.
- `cargo test` (14 tests), `cargo clippy --all-targets [--all-features] -- -D
  warnings` and `cargo fmt --check` pass. The kernels start no threads and size
  nothing by CPU count; `tests/meta/test_hardware_never_limits_work.py`
  passes.

### Packaging

`python packages/tofu-search/rust/scripts/build_wheel.py --out DIR` stages
the crate, the package's `.py` modules, README, LICENSE and a pyproject
derived from the package's own, then runs `maturin build --release`. It
produced `tofu_search-0.12.28-cp310-abi3-manylinux_2_34_x86_64.whl`. Against
the pure `py3-none-any` wheel built by `uv build`, it has the same 91 module
files with identical bytes plus `tofu_search/_native.abi3.so`. Name, version,
Requires-Python, extras, License and every requirement compare equal after
parsing, and both wheels declare the `tofu-search-mcp` entry point. The pure
build is unchanged. A PyPI release would need manylinux2014 (or zig) builds
for each platform plus the pure wheel as fallback. That release is the
owner's held decision and was not attempted.

### Benchmark

`rust/scripts/bench_native.py` runs each arm in a fresh interpreter. The
Python arm blocks the extension, so it is the package as the pure wheel ships
it. The workload is the post-fetch CPU stage of `search()` over 20 tracked repository
Markdown documents (724,073 characters) read as fetched bodies. The queries are
six English and Chinese queries. 21 samples were taken at host load ≈ 168
under `nice -n 19`, with no network, model or browser. Raw results are in
`output/rust_rewrite/tofu_search_native_bench.json`.

| Workload | Python median (p90) ms | Native median (p90) ms | Median ratio |
|---|---:|---:|---:|
| `tokenize` × 20 bodies | 191 (474) | 41.6 (231) | 4.6× |
| `select_relevant_passages` × 20, 8,000 chars | 306 (497) | 38.8 (89) | 7.9× |
| `group_by_content`, 20 rows | 0.67 (0.70) | 0.29 (0.33) | 2.3× |
| `dedup_by_full_content`, 20 bodies | 100 (363) | 21.3 (121) | 4.7× |
| `rerank_by_bm25`, 20 bodies | 512 (865) | 51.3 (143) | 10.0× |
| group, dedup, rerank and annotate, 6 queries | 4,704 (5,380) | 1,227 (1,626) | 3.8× |
| 8 concurrent `rerank_by_bm25` threads, wall | 4,216 (5,800) | 147 (334) | 28.6× |

The ranked URLs were identical in both arms for all six queries. Peak process
RSS was 79.8 MiB for Python and 74.0 MiB for native. Growth during the
workload, after imports and corpus load, was 23.2 MiB and 16.9 MiB.

Limits of these numbers:

- The host was shared and loaded, so p90s are wide, and an earlier run gave
  Python medians 25–40 % different.
- The bodies are documentation, not captured web pages.
- In the four-step stage the remaining native time is mostly the passage
  kernel, plus `quality.py` Python calling the kernels per field.
- Network fetch and extraction, which dominate a real search's wall time,
  were not measured or changed.
- The concurrent row shows that the GIL is released. It does not predict
  production concurrency.

### Second step: URL, authority, citation and HTML-extraction layers

The first step's kernels are unchanged. The new layers follow the same
pattern: the public name is a thin wrapper that calls the native function for
`str` input, and the Python body stays in the module's `PYTHON_KERNELS`.
`urllib.parse`, `ipaddress`, `html.parser`, `html.unescape` and the parts of
BeautifulSoup 4.15 the extractor touches are ported as the interpreter ships
them (`rust/src/pyurl.rs`, `pyre.rs`, `html/`). That includes CPython
3.12.13's patched `html.parser`, which treats `iframe`, `xmp`, `noembed` and
`noframes` as raw text and recognises `--!>`. `re` `\d` and the
`re.IGNORECASE` folds of `İ`, `ı`, `ſ` and the Kelvin sign, the HTML5 entity
table, BeautifulSoup's entity and Windows-1252 reference tables are exported
by `rust/scripts/gen_py_tables.py` (`--check`). Each regular expression is a
scanner that returns the same leftmost match as the backtracking original,
including `html.parser`'s unterminated-quote fallback in tag attributes.

Two inputs keep the Python path, by design. The authority classifier returns
`None` for a host that is non-ASCII after lowercasing, because Python encodes
it with the IDNA 2003 codec (nameprep over Unicode 3.2 tables). A string with
a lone surrogate cannot cross into Rust. Both cases are tested.

**Problem:** trafilatura is a third-party heuristic stack over libxml2's
recovering HTML parser and two fallback extractors. Producing the same text in
Rust would mean reimplementing libxml2's error recovery and every heuristic,
for about a third of extraction time, and the lxml call is already C behind a
process-wide lock. Profiling a 1 MB Wikipedia page put about 65% of
`extract_html_text` in the package's own BeautifulSoup half (`html.parser`
tokenizing, tree building, `find_all`, `get_text`) and about 31% in
trafilatura.
**Decision:** port the package's own extraction code bit for bit, meaning
everything `extract_html_text` derives from the tree after trafilatura
returns, and keep calling trafilatura from Python. The kernel receives
trafilatura's text and releases the GIL while it parses and assembles.
**Alternatives considered:** a Rust reimplementation of trafilatura (no
parity path for libxml2 recovery, and its output is not ours to define); an
html5ever or scraper tree (it builds different trees from `html.parser`, so
links, fallback text and supplements would diverge on malformed pages); a
sidecar process (it adds a process boundary and does not change the cost).

The captured corpus is `packages/tofu-search/tests/fixtures/html_corpus/`.
It holds 64 pages from 57 hosts in 16 languages: English, Chinese,
Japanese, Korean, German, French, Spanish, Portuguese, Italian, Russian,
Arabic, Hindi, Hebrew, Thai, Vietnamese and Turkish. They cover reference
pages, documentation, specs, code hosting, news, forums and portals.
`scripts/capture_search_html_corpus.py` fetched them through
`lib.http_transport.client` and decoded them with the package's own
`_decode_bytes`. Script and style bodies are replaced by a stub, JSON-LD and
JSON data blocks are kept, comments, `data:` URIs and SVG path data are
dropped, and each page is capped at 160,000 characters at a tag boundary. The
corpus is 9.6 MB of plain HTML and JSON. 13 URLs were refused (403/503/404) or unreachable and
are listed in `manifest.json`. `golden.json` records `extract_html_text`
output, rendering scope and document-text digest from the implementation
before the port, at two budgets, together with the library versions that
produced it (trafilatura 2.3.1, beautifulsoup4 4.15.0, lxml 6.1.3).

### Verification of the second step

- `tests/test_html_extraction_corpus.py` requires the recorded output for all
  128 corpus cases on the Python path, and on the native path when the
  extension is installed. Both passed. It is skipped under other library
  versions.
- `tests/test_native_layers_parity.py` compares each kernel with its oracle:
  - `canonical_url_key`, `classify_authority`, `host_brand_labels` and
    `brand_tokens` on all 10,974 distinct corpus links plus 4,000 generated
    URLs each.
    The generated URLs cover IPv6 and IPvFuture brackets, zone ids, ports,
    NFKC-unsafe netlocs, IDNA label limits, tracking parameters and
    percent-escapes.
  - 2,500 generated BibTeX files, reference blobs and identifier texts each.
  - `_assemble_document` on every corpus page in four variants, including
    the fallback-text path without trafilatura output and a negative budget,
    then 1,500 generated documents and 40 mutated real pages.
- A separate randomized run, not committed, used 4 seeds and the same
  generators. It made about 716,000 URL-kernel comparisons, 60,000 BibTeX
  files, 60,000 reference blobs, 120,000 identifier comparisons, 48,000
  generated HTML documents and 85,532 real pages cut and spliced mid-tag and
  mid-quote. There were 0 mismatches.
- The package suite collects 1,667 cases, 9 more than after the first step: 6
  layer-parity cases and 3 corpus cases. Without the extension the native
  corpus case is not collected, which leaves 1,666.
  - **Installed native wheel**, rebuilt from the committed source and run
    outside the checkout: 1,666 passed, 8 skipped. The one failure was
    `test_local_rerank_parent_lifetime[mismatched]`, the load flake recorded
    for the first step. Run serially it passed 5 of 5, twice.
  - **Checkout with the extension**: 1,659 passed, 8 skipped, 0 failed.
  - **Checkout without the extension**: 1,666 cases, 1,636 passed and 30
    skipped (22 of them native parity), 0 failed. The corpus golden test
    passes on the Python path.
- `cargo test` has 28 unit tests. `cargo clippy --all-targets
  [--all-features] -- -D warnings`, `cargo fmt --check`,
  `gen_py_tables.py --check`, `scripts/check_static_gates.py` and
  `tests/meta/test_hardware_never_limits_work.py` pass. The new kernels start
  no threads and size nothing by CPU count.

### Benchmark of the second step

`rust/scripts/bench_native.py` now runs three fresh-interpreter arms:
`python` (the pure package), `previous` (the first step's kernels with these
layers restored to Python) and `native`. 15 samples were taken, 5 for the
extraction workloads, at host load ≈ 40 on 180 CPUs. Outputs were identical in
all three arms: ranked URLs, a digest of all 64 extractions, the six searches'
labels and the parsed citations. Raw results are in
`output/rust_rewrite/tofu_search_native_layers_bench.json`.

| Workload | Python median ms | Previous native ms | Native median (p90) ms | vs Python | vs previous |
|---|---:|---:|---:|---:|---:|
| `canonical_url_key`, 11k corpus links | 36.1 | 35.0 | 9.6 (9.7) | 3.8× | 3.7× |
| `classify_authority`, 11k corpus links | 124 | 123 | 14.5 (14.8) | 8.6× | 8.5× |
| `parse_bibtex`, 200 entries | 8.3 | 8.3 | 1.07 (1.09) | 7.8× | 7.7× |
| `parse_references`, 200 entries | 3.2 | 3.3 | 0.81 (0.83) | 4.0× | 4.1× |
| `extract_citations_from_text`, 20 docs | 43.0 | 45.5 | 3.3 (3.3) | 13.1× | 13.9× |
| extraction after trafilatura, 64 pages | 2,136 | 2,210 | 144 (163) | 14.8× | 15.3× |
| `extract_html_text`, 64 pages | 4,664 | 4,691 | 2,539 (2,595) | 1.8× | 1.8× |
| post-fetch stage with extraction, 6 searches × 16 pages | 7,981 | 7,358 | 4,150 (6,303) | 1.9× | 1.8× |
| 8 threads × 8 pages `extract_html_text`, wall | 4,796 | 4,919 | 2,892 (3,338) | 1.7× | 1.7× |
| first-step post-fetch stage (no extraction), 6 queries | 2,150 | 518 | 524 (536) | 4.1× | 1.0× |

Limits of these numbers:

- The host is shared. The Python p90 of full extraction was 8,003 ms against a
  median of 4,664, and an earlier 3-sample run differed by up to 25%. Only the
  ratios of medians are meaningful.
- Whole-page extraction is now about 95% trafilatura. Its lxml pass is
  serialized by `_TRAFILATURA_LOCK` in every arm, so threads gain only what
  the released assembly saves.
- The first-step stage is unchanged (0.99×) because it contains no URL, citation
  or HTML work beyond a few authority lookups.
- Peak RSS was 213 MiB (Python), 217 MiB (previous) and 221 MiB (native). The
  arena tree adds a few MiB during a page.
- Network time, which dominates a real search, is not included.

### Remaining for this leaf

In order:

1. trafilatura's article pass, the code-blob, fragment and stream-restore
   paths, and publication dates. The corpus and goldens now give these a
   parity gate.
2. The citation verifier's HTTP checks and the HTTP, fetch and engine layers
   on reqwest, after Phase 0 classifies the suite so that black-box cases can
   judge them.
3. MCP delivery.

The Python oracle bodies are deleted only once a module's whole surface is
native and the pure wheel is retired, which is an owner decision.
