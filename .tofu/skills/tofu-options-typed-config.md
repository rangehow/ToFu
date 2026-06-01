---
name: tofu-options-typed-config
description: TofuOptions typed dataclass: camelCase wire ↔ snake_case Python, additive (extras dict), feeds OpenAPI TofuConfig schema
enabled: true
tags: [sdk, openapi, config, agent-options]
created: 2026-05-26T13:07:29Z
updated: 2026-05-26T13:07:29Z
---

# TofuOptions — typed agent-run config

`lib/agent_options.py` (added 2026-05-26) is the single typed schema for the `cfg`
dict that flows from routes → `lib/tasks_pkg/orchestrator.py` + `model_config.py`.

## Why
`cfg` had ~30 keys read silently across the codebase. Typos passed through;
SDK callers had to grep source to discover what was tunable. OpenAPI's
`TofuConfig` schema only documented 12 of the 30+ keys.

## Design rules (DON'T BREAK)
1. **Backward-compatible — never reject unknown keys.** They go to `extras` and
   round-trip through `to_cfg()` unchanged. Hard validation breaks Feishu /
   scheduler / autopilot callers using ad-hoc keys.
2. **Wire format stays camelCase.** Frontend, SDKs, OpenAI compat all send
   `maxTokens`, `thinkingEnabled`, `searchMode`. Python attrs are snake_case.
3. **Defaults match orchestrator's `cfg.get(key, default)` calls.** When the
   orchestrator default differs from `TofuOptions` default the orchestrator
   wins because we emit explicit values.
4. **`_FIELDS` in `lib/agent_options.py` is the single source of truth.** Adding
   a field = one row in `_FIELDS` + one dataclass attribute. `openapi_schema()`
   auto-derives.

## How to add a new cfg key
1. Add a row to `_FIELDS` (camelCase wire, snake_case attr, type, default,
   enum-or-None, description).
2. Add the matching dataclass attribute on `TofuOptions`.
3. Add the camelCase key to `tests/test_agent_options.py::test_orchestrator_compatibility`
   if the orchestrator reads it.

## Files
- `lib/agent_options.py` — the dataclass.
- `tests/test_agent_options.py` — 17 tests covering parse / round-trip / coercion / extras / OpenAPI.
- `lib/openapi.py` — `_tofu_config_schema()` lazy-imports `TofuOptions.openapi_schema()`.

## NOT done yet (next steps)
- Routes still read `cfg` directly. The dataclass is currently parse-only.
- Future: route handlers should construct `opts = TofuOptions.from_cfg(...)`,
  pass `opts.to_cfg()` to the orchestrator, and read typed attrs locally.

