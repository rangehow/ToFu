# Rust rewrite: evidence and target architecture

Status: design evidence for the owner's decision of 2026-11-18 whether to rewrite
Tofu entirely in Rust. Nothing here is implemented in the product; the one
Rust round that exists is the evidence-only prototype in
[Core loop prototype](#core-loop-prototype). [Where time goes](#where-time-goes)
measures real Turns. This document answers four questions: what
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

The generators stay in-repo, with check mode in CI. In the last phase they move
from Python scripts to a Rust `xtask`, which removes Python from the build.
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
| **5. Domains, one owner at a time** | Order follows dependencies: conversations and sync; `project_mod` (with `run_command`); model routing and registration; MCP (rmcp); channels (the stdio protocol keeps bundle adapters running); router, orchestration and swarm; scheduler and automations; memory, knowledge and research; paper and slides orchestration (rendering through the document host); browser and desktop server side; tofu-search | Each owner's `tests/<owner>/` black-box subset plus ported white-box cases; owner contracts from `inventory.json` |
| **6. Node and distribution** | `tofu node` replaces `lib/desktop_agent`; one binary per platform served by `lib/desktop_dist`'s successor; Android kernel without proot Python; PyInstaller and `install.sh` Python bootstrap retired | `tests/desktop`, `tests/desktop_dist`, `tests/installer`, `tests/mobile`, `desktop_node_v1` and `desktop_bridge_v3` schemas |
| **7. Endpoint** | Generators as a Rust `xtask`; Python absent from the core install; uv-managed runtime provisioned lazily; document-worker capabilities ported as their Rust golden-file parity passes | Golden-file corpora for pptx, docx and PDF extraction; `tests/slides`, `tests/pdf_parser`, `tests/doc_parser`; full e2e |

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
| Fallback | A refusal applies to one request; the next returns to the primary except inside the refused tool loop; every switch emits `MODEL_FALLBACK` | same |
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

- **75/75 identical** (7.2 MB of payload).
- Python's own line file is unchanged before and after the merge.
- The cargo test `tests/wire_golden.rs` pins one synthetic history covering
  every ported stage against Python-generated bytes
  (`scripts/gen_wire_golden.py`).

Types were not generated: no generator emits the canonical request or wire
shapes. They are Python code paths, not contracts. The generators that exist
(`gen_rust_chat_wire_oracle.py`, `gen_rust_execution_contract.py`) produce
oracles and constants for the `tofu-runtime` subset. The prototype therefore
ports the Python emitters by hand and uses Python as the oracle.

The port covers what the corpus exercises. The following refuse with
`Unsupported` instead of producing guessed bytes:

- images;
- reasoning and replay sidecars;
- Claude, Gemini, Kimi and Mistral model families;
- tool-call ID respelling;
- tool-name aliasing;
- gateway profiles and OAuth headers;
- `tool_choice`, `response_format` and `stop`.

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
