---
name: tool-spec-registry-plugin-seam
description: Agent-base architecture: tofu.tools/tofu.providers seams, profiles, lib/agent_core facade + manifest, AST boundary test — all guarded
enabled: true
tags: [tools, architecture, plugin, convention]
created: 2026-06-01T04:02:21Z
updated: 2026-06-01T04:34:01Z
---

# Agent-Base Architecture — plugin seams, profiles, core boundary

As of 2026-06, Tofu is a clean agent base: large capabilities (orchestration,
swarm scheduling, endpoint Planner→Worker→Critic loop, compaction, push) +
base tools are reusable; tools, model backends, and capability bundles are
declarative drop-ins. Five pieces, all test-guarded:

## 1. Tools — `lib/tools/registry.py` (entry-point group `tofu.tools`)
- `ToolSpec(key, build, phase, provides, write_tools, idempotent_tools,
  category, description, handler, handler_names, handler_special)` +
  `ToolContext` + `assemble_tool_list(ctx)`.
- Built-ins register via `_register_builtins()` in cache-stable order: search →
  fetch → read_files → project|code_exec → browser → desktop → image_gen →
  conv_ref → human_guidance → ⟨base/capability boundary⟩ → memory → scheduler →
  swarm → mcp. Two phases; `phase='base'` counted toward `has_real_tools`
  (snapshotted into `ctx.has_base_tools`); capability phase self-gates.
- `build(ctx)` called at REQUEST time → lazy imports inside.
- **Schema+gate+handler from ONE external package**: attach `handler=`. Executor
  calls `sync_spec_handlers(tool_registry)` at startup; late plugins self-sync
  via `register_tool_spec`→`_sync_one`. `handler_special` → special dispatch key.
- `_assemble_tool_list` legacy signature kept (swarm tests/autopilot). Caller
  `cfg['tools']` override → early return.
- `tool_dispatch._WRITE_TOOLS`/`_IDEMPOTENT_TOOLS` = base set UNION spec flags.

## 2. Providers — `lib/llm_dispatch/provider_registry.py` (group `tofu.providers`)
- Pluggable axis = body dialect (`Slot.thinking_format`).
  `BodyDialect(key, apply_build, apply_readjust)`.
- Registry holds PLUGIN dialects only. Built-ins (`''`,`none`,`enable_thinking`,
  `thinking_type`,`chat_template_kwargs`) stay in the ladders in `lib/llm/body.py`
  + `lib/llm_dispatch/api.py`. Registry consulted as FIRST branch; built-ins
  return None → fall through → BYTE-IDENTICAL (characterization-tested).
- `is_valid_thinking_format(v)` = built-in OR plugin. Used by `Slot.__post_init__`
  (via `_is_valid_thinking_format` w/ fallback) + `byo_providers._validate_thinking_format`. Typos still raise.

## 3. Capability profiles — `lib/agent_profiles.py`
- Profile = named cfg-default bundle. `apply_profile(cfg)` = `{**defaults, **cfg}`
  → explicit cfg ALWAYS wins, profile fills gaps.
- Selected via `cfg['profile']` (wire key `profile`, in `lib/agent_options.py`
  `_FIELDS` + `TofuOptions.profile`).
- Built-ins: `default`(no-op), `research`, `coding`, `minimal`. Operators
  add/override via `data/config/profiles/<name>.json` (camelCase; filename ==
  profile name replaces built-in).
- Applied ONCE in `orchestrator.run_task` right after `cfg = task['config']`,
  BEFORE `_resolve_model_config` + tool assembly. No-op when absent/'default'.

## 4. Core/plugin boundary — `lib/agent_core_manifest.py` + AST test
- Boundary is a DECLARED manifest: `CORE_MODULES`, `REGISTRY_SEAMS`,
  `CONCRETE_PLUGIN_MODULES`. `is_core_module()` also accepts REGISTRY_SEAMS +
  `_CORE_PACKAGE_FACADES` (`lib.llm`, `lib.llm_dispatch`).
- `tests/test_agent_core_boundary.py` (AST, 4 tests): (a) CORE_MODULES resolve
  to files; (b) NO core file imports a concrete plugin (only seams
  `lib.tools.registry`/`lib.llm_dispatch.provider_registry`); (c) facade members
  are within core; (d) facade __all__ all importable.

## 5. Browsable facade — `lib/agent_core/__init__.py` (added 2026-06)
- We did NOT physically move files (~960 import sites; not a git repo → no
  undo; core is a CROSS-CUTTING subset of tasks_pkg/llm_dispatch/swarm, so a
  literal move would create `agent_core→tasks_pkg` back-imports that INVERT the
  dependency direction). Instead: a FACADE package that re-exports the base
  public surface (`run_task`, `_assemble_tool_list`, `run_compaction_pipeline`,
  `build_body`/`chat`/`stream_chat`, `dispatch_chat`/`dispatch_stream`/
  `get_dispatcher`/`reset_dispatcher`, `TaskRuntime`, `hub`/`push_event`,
  `apply_profile`/`get_profile`/`list_profiles`/`resolve_profile_name`, +
  seams `ToolSpec`/`ToolContext`/`assemble_tool_list`/`register_tool_spec`/
  `BodyDialect`/`register_dialect`). `CORE_MEMBERS` maps symbol→module;
  kept consistent with the manifest by the boundary test.
- Existing imports unchanged; facade NAMES the base, doesn't replace modules.
- `lib.agent_core` is itself listed in CORE_MODULES (must obey no-concrete-plugin).

## Guards (DO NOT regress) — all green
- `test_core_tool_isolation.py`, `test_tool_registry.py`, `test_provider_registry.py`,
  `test_agent_core_boundary.py` (4), `test_agent_options.py`,
  `test_swarm_async.py` (TestSwarmDecoupledFromProject calls `_assemble_tool_list`
  directly — signature MUST stay stable).

## Gotchas
- Memory tools gate on `has_base_tools` ONLY (not `memoryEnabled`).
- swarm + mcp NOT gated on has_base_tools; read_files always on → has_real_tools ~always True.
- `build_body` HOT_PATH; provider plugin branch is first if/elif so Claude post-proc runs.
- Pre-existing unrelated failures (NOT regressions): `test_billing_phase2`
  (shared-state order), `test_api_integration`/`test_bridge_auth` (QuartClient
  ctx-manager harness), `test_chat_manager_migration` (poll 404 race). Fail in isolation.
- Doc updated: `docs/ARCHITECTURE.md` §3.2 has agent_core / agent_core_manifest / agent_profiles rows.

