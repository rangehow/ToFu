---
name: swarm-decoupled-from-project
description: Swarm tools fully decoupled from project mode (mirrors read_files decoupling)
enabled: true
tags: [swarm, convention, decoupling]
created: 2026-05-28T11:32:18Z
updated: 2026-05-28T11:32:18Z
---

# Swarm tools fully decoupled from project mode (2026-05-28)

## Why
Originally `if swarm_enabled and project_enabled` gated both tool
injection AND prompt injection. A user enabling Swarm without Project
got silent zero-tool-install — no error, no UI hint. Real users hit
this and reported "spawn_agents never gets called". Diagnosed by
console: `swarmEnabled: true, projectMode: undefined` → gate failed.

There's no architectural reason swarm needs a project. A bare
"compare these 3 libraries" research swarm is a perfectly valid use case.

## Final design
**`if swarm_enabled:`** is the ONLY gate.

- `lib/tasks_pkg/model_config.py:_assemble_tool_list` — drops the
  `and project_enabled` clause; swarm tools always inject when toggle on.
- `lib/tasks_pkg/system_context.py:_inject_system_contexts` — same.
- Sub-agents that need project-scoped tools (read/write/grep) STILL
  get them via the existing `all_tools` propagation in `_make_agent`,
  scoped by role.

## UI hint
- `static/js/i18n.js`:
  - `toolbar.swarmAgentsDesc`: "并行子代理分解任务 · 独立工具，无需开启 Project"
  - `mobile.parallelAgents`: "并行子代理 · 无需 Project"
- No forced coupling — toggle works independently.

## Tests
- `tests/test_swarm_async.py::TestSwarmDecoupledFromProject`:
  - `test_bare_conversation_swarm_has_all_three_tools`
  - `test_swarm_off_does_not_inject_tools` (negative)
  - `test_swarm_prompt_injected_without_project`

## Pattern parallel
Same shape as `read_files` decoupling done 2026-04-20 (see
`read-files-tool-always-on-decoupled` memory). Both follow:
"a tool's gate should reflect what the tool *needs*, not historical
co-location with other features."

