---
name: sdk-parity-hooks-budget
description: Hook taxonomy expansion: UserPromptSubmit, PreCompact, modify-action wired end-to-end + max_budget_usd cost gate
enabled: true
tags: [sdk, hooks, budget, agent-options, claude-agent-sdk]
created: 2026-05-26T13:45:28Z
updated: 2026-05-26T13:45:28Z
---

# SDK-parity work: hook taxonomy + cost gate (2026-05-26)

Three additions made `lib.tasks_pkg` closer to Claude Agent SDK semantics
without breaking any existing call site.

## 1. UserPromptSubmit hook
- New: `register_user_prompt_hook(callable)` in `lib/tasks_pkg/tool_hooks.py`.
- Fires once per turn from `manager.create_task`, BEFORE the prompt enters
  the agent loop. Hooks may rewrite the latest user message; only string
  contents are touched (multimodal lists pass through).
- Use cases: PII redaction, safety filters, prompt augmentation.

## 2. PreCompact hook
- New: `register_pre_compact_hook(callable)`.
- Fires from `compaction/_pipeline.run_compaction_pipeline` BEFORE any
  compaction layer mutates messages. Hooks should treat the message list
  as read-only (deepcopy if they need to keep it).
- Use case: external archival / audit trail before lossy compaction.

## 3. HookResult.modify action honored end-to-end
- `run_pre_hooks` now mutates `args` in-place when a hook returns
  `HookResult(action='modify', modified_args={...})`, so every downstream
  consumer (parallel pool, serial dispatch, executor) sees the rewrite
  without us having to thread a new dict through the tuple-bound items.
- Tested: rewrite-then-block sees rewritten args; non-dict modified_args
  silently ignored; added/removed keys both work.

## 4. max_budget_usd cost ceiling
- New TofuOptions field: `max_budget_usd: float = 0.0` (camel: `maxBudgetUsd`).
- New module: `lib/cost_estimator.py` — `estimate_usage_cost(usage, model)`
  + `check_budget(task, accumulated_usage, model, max_budget_usd)`.
- Tolerates Anthropic and OpenAI usage shapes; respects cacheRead /
  cacheWrite multipliers from `lib.pricing.MODEL_PRICING`.
- Wired into `orchestrator.py` round loop AFTER `_emit_round_usage` so
  cumulative cost is checked at the same boundary the per-round usage
  log fires. Sets `task['error']`, sets `last_finish_reason='budget_exceeded'`,
  and `break`s out of the round loop.
- Unknown models cost 0.0 → never block; missing pricing data never raises.
- 0 / unset disables the gate.

## Tests
- `tests/test_hook_taxonomy.py` — 15 tests (modify action, UserPromptSubmit
  in-process & via `create_task`, PreCompact in-process & via pipeline).
- `tests/test_cost_estimator.py` — 14 tests covering token-shape coercion,
  Claude pricing math, cache discounts, gate enable/disable, unknown-model
  safety, OpenAPI schema.
- All `tests/test_e2e_headless_api.py` (36) still pass.

## Known unrelated failures
- `tests/test_chat_manager_migration.py::test_chat_streams_via_http_endpoints`
  and 5 `test_paper_migration.py` tests fail with 401 (pre-existing auth
  baseline issue; unrelated to these changes — they don't touch the hook
  surface, cost gate, or TofuOptions).

