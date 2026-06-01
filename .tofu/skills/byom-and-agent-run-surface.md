---
name: byom-and-agent-run-surface
description: BYO model + /agent/run surface (polished): per-key providers, ephemeral slots, model+provider split, unified config, flat trajectory, BYO models in /v1/models
enabled: true
tags: [api, byom, agent, providers, trajectory, headless]
created: 2026-05-28T06:04:21Z
updated: 2026-05-28T06:20:39Z
---

# BYOM + /api/v1/agent/run surface (added 2026-05-28; polished same day)

External callers register/inline their own LLM endpoint; Tofu provides
the agent runtime, tools, memory, swarm, and trajectory capture.

## API surface

### `model` is ALWAYS a string

* `model: "deepseek-v4-pro"` — global slot pool
* `model: "deepseek-v4-pro@prov_a3f2c1"` — registered BYO provider lookup
* `model: "deepseek-v4-pro"` + `provider: {base_url, api_key, extra_headers}`
  — inline BYO (one-shot ephemeral slot)

Combining `@prov_xxx` suffix with an inline `provider` block is
ambiguous → 400.

### `config` accepts BOTH curated aliases AND raw orchestrator keys

`lib/tasks_pkg/agent_run.py::_build_cfg` translates:

| Alias (snake)   | → orchestrator key      | Notes |
|-----------------|-------------------------|-------|
| `thinking`      | `thinkingDepth` + `thinkingEnabled` | string `"low"`…`"max"` |
| `tools`         | per-tool toggles        | `["search","fetch","memory","mcp",...]` or `"*"` / `["*"]` |
| `search`        | `searchMode`            | `"off"`/`"single"`/`"multi"` or bool |
| `memory`/`swarm`/`mcp`/`browser`/`desktop`/`code_exec`/`image_gen`/`human_guidance`/`scheduler` | `*Enabled` | bool |
| `project`       | `projectPath`           | absolute path |
| `max_tokens`    | `maxTokens`             | int |
| `temperature`   | `temperature`           | float |

Raw orchestrator keys (`thinkingDepth`, `searchMode`, `memoryEnabled`,
…) flow through unchanged and **win** when both alias + raw key are
present (last write wins). Unknown keys pass through (forward-compat).
Legacy `capabilities` field still accepted, merged into `config`.

### Trajectory envelope is FLAT

Response carries top-level `trajectory_format: "sharegpt"` +
`trajectory: [...]` — NOT `{trajectory: {format, trajectory}}`.

### `extra_headers` allowlist (security)

`routes/api_v1/providers.py::sanitise_extra_headers` rejects:
`Authorization`, `x-api-key`, `Cookie`, `Set-Cookie`, `Host`,
`Content-Length`, `Transfer-Encoding`, `Proxy-Authorization` (case-
insensitive). Max 16 entries, max 2048 chars per value, scalar values
only. Used by both providers CRUD and inline `provider` blocks on
agent/run.

### `auto_discover` defaults to FALSE

`POST /api/v1/providers` no longer probes `/v1/models` synchronously
by default. Registration is fast & unconditional; follow up with
`POST /api/v1/providers/{id}/probe` to ingest models.

### `GET /v1/models` surfaces caller's BYO providers

When called with a Bearer key that owns BYO providers, the response
includes those providers' models with the suffix attached:
`{id: "deepseek-v4-pro@prov_a3f2c1", owned_by: "prov_a3f2c1",
tofu_provider_name: "cluster-A", capabilities: [...]}`. Lets stock
OpenAI SDKs (LangChain, Cline, OpenWebUI, Aider) populate model
dropdowns including BYO endpoints with zero custom code.

### 403 errors are structured

`require_scope` decorator (routes/api_v1/auth.py:401) emits
`api_forbidden(msg, missing_scope=sc, required_scopes=[...],
granted_scopes=[...])`. Top-level fields (NOT nested under `error`)
because `api_response.api_error()` puts `**extras` at top level.

## Implementation files

* `lib/byo_providers.py` — key-scoped provider store. Per-key quota:
  `_MAX_PROVIDERS_PER_KEY = 32`, `_MAX_MODELS_PER_PROVIDER = 64`.
  api_key stored plaintext on disk (proxy needs it); `redact()` =>
  `key_hint = "sk-int…ar"` for any user-facing response.
* `lib/llm_dispatch/ephemeral.py` — `mint_ephemeral_slot()` returns
  `EphemeralSlotHandle`; `dispose_ephemeral_slot(h)` is idempotent.
  Auto-registers private hosts via `register_no_proxy_url`. Process
  ceiling: 1024 live handles.
* `lib/trajectory.py` — `flatten(task, fmt)` over 4 formats.
* `routes/api_v1/agent_run.py` — single-call `/agent/run` route.
  `_resolve_model_and_provider()` returns
  `(model_id, handle, byo_row, error_msg, http_status)`.
* `routes/api_v1/providers.py` — providers CRUD + `sanitise_extra_headers`.
* `lib/compat/openai.py::models_payload(owner_key_id=…)` — appends
  caller's BYO models to `/v1/models`.

## New scopes

* `providers` — `/api/v1/providers/*`
* `agents:run` — `/api/v1/agent/run` (does NOT imply `chat`)

## Tests (50/50 green)

* `tests/test_byo_providers.py` (10) — CRUD, isolation, redact, resolve, quota
* `tests/test_ephemeral_slot.py` (5) — mint/dispose, no_proxy, validation
* `tests/test_trajectory.py` (8) — 4 formats × tools/no-tools, multimodal
* `tests/test_api_v1_agent_run.py` (11) — model+provider split, config
  aliases, flat trajectory, missing_scope, ambiguity check
* `tests/test_api_v1_byo_surface_polish.py` (11) — header allowlist,
  /v1/models BYO injection, fast registration

## Test boilerplate gotcha (still valid)

Route tests need:
1. `_install_shim()` called BEFORE first `routes.api_v1.*` import — call
   it at module level, not just in setUpClass, when the test imports
   sub-modules at function scope.
2. Override `lib.api_keys._STORE_PATH` AND `lib.byo_providers._STORE_PATH`
   to a tempdir.
3. `os.remove(_STORE_PATH)` in setUp (clearing the in-memory cache alone
   is not enough — JSON file persists).
4. Stub `lib.tasks_pkg.spawn_task` to set terminal task state synthetically.

