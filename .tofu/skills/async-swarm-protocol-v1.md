---
name: async-swarm-protocol-v1
description: Async swarm protocol: spawn returns handle, sub-agent updates flow through agent_inbox, no review/synthesis
enabled: true
tags: [swarm, architecture, convention]
created: 2026-05-27T16:21:46Z
updated: 2026-05-27T16:21:46Z
---

# Async Swarm Protocol (v1 — replaces all reactive/synthesis paths)

## Tools the master LLM sees
- `spawn_agents(agents=[{objective, context?, role?, depends_on?}])` — fire-and-forget; returns JSON `{status:"async_launched", swarm_id, agents:[{id, role, objective, output_file}]}`. Calling again with an existing session injects new specs into the live scheduler (no separate spawn_more tool).
- `await_agents(ids?, mode='any'|'all', timeout_seconds<=120)` — block until ≥1 / all complete; returns `{completed:[...], still_running:[...], timed_out}`.
- `get_agent_result(agent_id)` — full final answer, or status notice for running/pending.
- `store_artifact / read_artifact / list_artifacts` — shared K-V.

**Removed forever**: `spawn_more_agents`, `swarm_done`, `check_agents`, `REACTIVE_MASTER_TOOLS`, `_master_review`, `_synthesise`, `run_reactive*`, `run_swarm_task`, `lib/swarm/compat.py`, `lib/swarm/review.py`, `lib/swarm/synthesis.py`.

## Sub-agent denylist
`lib.swarm.tools.SUB_AGENT_DENYLIST = {spawn_agents, await_agents, get_agent_result, ask_human}`. Stripped by `scope_tools_for_role` for ALL roles (including `general`). Sub-agents only get the artifact tools on top of role-scoped tools.

## Inbox — the model-facing notification queue
- `lib/agent_inbox.py` — per-task priority queue (now > next > later), distinct from `lib/push.py` (UI-facing).
- On every sub-agent completion, `MasterOrchestrator._on_agent_complete_callback` calls `agent_inbox.enqueue(task_id, format_swarm_update(...), priority='later', mode='swarm-update')`.
- `lib/tasks_pkg/orchestrator.py` drains the inbox **right before each LLM call**, prepending each item as a `user` `_isMeta` message. Skipped when the previous turn ended with an unmatched `assistant tool_calls` — wait for the tool_result pair to close.
- Task end (`task['status'] = 'done'` block in orchestrator) calls `agent_inbox.clear(task_id)` and `_remove_session(task_id)` to prevent leaks.

## Per-agent output files
Each sub-agent streams content + thinking to `data/swarm/<task_id>/<agent_id>.log`. Path returned in handle and in `<swarm-update>` so the model can `read_files` it if explicitly asked, but system prompt tells it not to.

## System prompt
`<parallel_execution>` block in `lib/tasks_pkg/system_context.py:_inject_system_contexts`. Teaches:
- "fire and forget, don't poll, don't peek output_file, don't fabricate results, use await_agents only when nothing else to do".
- Worked example with multi-round timing including "user asks mid-wait → give status, not guess".

## Key files
- `lib/agent_inbox.py` (new) — queue + format_swarm_update XML builder.
- `lib/swarm/master.py` (rewritten) — `MasterOrchestrator.run_in_background()` only. Daemon thread runs `StreamingScheduler.iter_completions()`.
- `lib/swarm/integration.py` (rewritten) — `_handle_spawn_agents` returns handle; `_handle_await_agents`, `_handle_get_agent_result`.
- `lib/swarm/tools.py` (rewritten) — `MASTER_TOOLS`, `SUB_AGENT_TOOLS`, `SUB_AGENT_DENYLIST`, `SWARM_CONTROL_TOOL_NAMES`, `SWARM_TOOL_NAMES`.
- `lib/swarm/registry.py` — `scope_tools_for_role` always strips `SUB_AGENT_DENYLIST`.
- `lib/swarm/planner.py` (trimmed) — `resolve_execution_order` only.
- `lib/tasks_pkg/orchestrator.py` — adds drain hook just before LLM call + cleanup on task end.
- `lib/tasks_pkg/system_context.py` — `<parallel_execution>` async-mode prompt.
- `lib/tasks_pkg/model_config.py` — emits `SPAWN_AGENTS_TOOL + AWAIT_AGENTS_TOOL + GET_AGENT_RESULT_TOOL` instead of legacy.
- `lib/tasks_pkg/handlers/misc.py` — swarm tool icon map updated.

## Tests
`tests/test_agent_inbox.py`, `tests/test_swarm_tool_scoping.py`, `tests/test_swarm_async.py` — all pass. The legacy `tests/test_swarm_unit.py` and `debug/test_swarm*.py` files were deleted as part of the migration.

