---
name: tool-spec-registry-plugin-seam
description: Agent-base architecture: tofu.tools/tofu.providers seams, profiles, lib/agent_core (Stage-1 leaves relocated + facade), AST boundary test — guarded
enabled: true
tags: [tools, architecture, plugin, convention]
created: 2026-06-01T04:02:21Z
updated: 2026-06-01T04:50:23Z
---

# Agent-Base Architecture — plugin seams, profiles, core boundary

As of 2026-06, Tofu is a clean agent base: large capabilities (orchestration,
swarm scheduling, endpoint Planner→Worker→Critic loop, compaction, push) +
base tools are reusable; tools, model backends, and capability bundles are
declarative drop-ins. Repo is now a git repo (init'd 2026-06 for reversibility).

## 1. Tools — `lib/tools/registry.py` (entry-point group `tofu.tools`)
- `ToolSpec(key, build, phase, provides, write_tools, idempotent_tools,
  category, description, handler, handler_names, handler_special)` +
  `ToolContext` + `assemble_tool_list(ctx)`. Cache-stable order: search→fetch→
  read_files→project|code_exec→browser→desktop→image_gen→conv_ref→
  human_guidance→⟨boundary⟩→memory→scheduler→swarm→mcp. `build(ctx)` lazy.
- Schema+gate+handler from ONE package via `handler=`; executor calls
  `sync_spec_handlers(tool_registry)` at startup, late plugins self-sync.
- `_assemble_tool_list` legacy signature kept; caller `cfg['tools']`→early return.
- `tool_dispatch._WRITE_TOOLS`/`_IDEMPOTENT_TOOLS` = base UNION spec flags.

## 2. Providers — `lib/llm_dispatch/provider_registry.py` (`tofu.providers`)
- Pluggable axis = body dialect (`Slot.thinking_format`).
  `BodyDialect(key, apply_build, apply_readjust)`. Registry holds PLUGIN dialects
  only; built-ins stay in `lib/llm/body.py` + `lib/llm_dispatch/api.py` ladders
  (registry first branch, built-ins fall through → BYTE-IDENTICAL).
- `is_valid_thinking_format` used by `Slot.__post_init__` + byo validator; typos raise.

## 3. Capability profiles — `lib/agent_core/profiles.py` (shim: `lib/agent_profiles.py`)
- Profile = named cfg-default bundle. `apply_profile(cfg)`={**defaults,**cfg}
  → explicit cfg wins. Selected via `cfg['profile']` (in agent_options _FIELDS +
  TofuOptions.profile). Built-ins default/research/coding/minimal +
  `data/config/profiles/*.json`. Applied once in run_task after cfg=task['config'].

## 4. Core/plugin boundary — `lib/agent_core_manifest.py` + AST test
- Manifest: `CORE_MODULES`, `REGISTRY_SEAMS`, `CONCRETE_PLUGIN_MODULES`.
  `is_core_module` also accepts REGISTRY_SEAMS + `_CORE_PACKAGE_FACADES` (lib.llm,
  lib.llm_dispatch). `tests/test_agent_core_boundary.py` (4 tests): CORE resolves
  to files; NO core file imports concrete plugin; facade members within core;
  facade __all__ importable.

## 5. `lib/agent_core/` package — Stage-1 physical relocation DONE (2026-06)
- **Relocated leaves** (self-contained, no core-sibling back-imports), via
  `git mv`: `push.py`, `task_runtime.py`, `agent_profiles.py`→`profiles.py`.
  Thin re-export SHIMS at the old paths (`lib/push.py`, `lib/task_runtime.py`,
  `lib/agent_profiles.py`) preserve all ~30 import sites AND the `hub` singleton
  identity (`lib.push.hub is lib.agent_core.push.hub`).
- **Facade `__init__.py` is LAZY** (PEP 562 `__getattr__` over `CORE_MEMBERS`):
  importing a leaf submodule (`from lib.agent_core.push import hub`) must NOT pull
  the heavy orchestrator chain. `CORE_MEMBERS` maps symbol→module (relocated leaves
  point at new homes; cross-cutting members named-in-place at tasks_pkg/llm_dispatch).
- **Cross-cutting members NOT moved** (orchestrator, model_config, endpoint,
  compaction, llm, llm_dispatch): a naive move creates agent_core→tasks_pkg
  back-imports + ~960 import rewrites. They migrate later when sibling coupling untangles.
- **CRITICAL gotcha — push_event resolution**: `TaskRuntime.append_event` and
  `manager.append_event`'s runtime path now import `push_event` from
  `lib.agent_core.push` (canonical home). Tests that monkeypatch push_event to
  capture must patch `lib.agent_core.push.push_event`, NOT `lib.push.push_event`
  (the shim re-export is bypassed). Fixed in test_task_runtime, test_restart_smoke,
  test_chat_manager_migration.

## LESSON: never `git stash` mid-migration with untracked new files
- During this work I ran `git stash`/`pop` to A/B a baseline. Stash treated the
  NEW shim files (untracked at stash time, paths that git saw as renamed-away) as
  deletions and DID NOT restore them on pop — silently lost all 3 shims.
  Recreated from known content. To compare against baseline cleanly: COMMIT first,
  then `git checkout <baseline> -- <path>` or use a worktree — don't stash a tree
  with untracked files that collide with tracked renames.

## Guards — all green (115 passed in the core suite)
- test_core_tool_isolation, test_tool_registry, test_provider_registry,
  test_agent_core_boundary (4), test_agent_options, test_task_runtime,
  test_swarm_async (TestSwarmDecoupledFromProject calls `_assemble_tool_list`
  directly — signature MUST stay stable).

## Pre-existing unrelated failures (NOT regressions; verified vs baseline)
- test_restart_smoke (server.py import harness: blueprint reg, 401/404/405 envelope),
  test_api_integration/test_features_needs_restart (QuartClient ctx-manager harness),
  test_billing_phase2 (shared-state order). All fail identically on baseline.
- `routes/push.py` `@push_bp.websocket` AttributeError under bare `python -c` is
  the Flask-vs-Quart shim (only installed when server.py boots) — not a regression.

