---
name: endpoint-anti-analysis-spiral-rewrite-2026-04
description: Endpoint mode 2026-04-26 rewrite: PLAN_DEFECT gate, deliverables snapshot, zero-deliverable guard, progress carryover on replan, STOP-with-❌→worker
enabled: true
tags: [endpoint, planner, worker, critic, analysis-spiral, prompts, architecture, redesign]
created: 2026-04-26T13:58:58Z
updated: 2026-04-26T13:58:58Z
---

# Endpoint mode — anti-analysis-spiral rewrite (2026-04-26)

## Problem

Previously endpoint mode would spiral into deep analysis with zero actual
work shipped. Recent observed failure modes (see logs/app.log.2026-04-23):

- **Task 00d009c6**: 4 plans (10k → 13k chars, growing each replan),
  7 iterations, 0 net deliverables, stop_reason=max_replans.
- **Tasks 1cb64dbf, 4e0316d3**: a single residual ❌ in critic feedback
  triggered STOP→CONTINUE_PLANNER override, wiping worker context and
  escalating what was just a "finish the last step" problem.

Root causes:
1. Critic's override guard (STOP-with-❌) escalated to CONTINUE_PLANNER,
   not CONTINUE_WORKER. Residual ❌ is almost always a *worker-execution*
   problem, not a *plan-structural* problem.
2. CONTINUE_PLANNER had no structural gate — the LLM could rationalize
   a replan from any feedback.
3. Replan wiped worker context (`_reset_worker_messages_with_plan` was
   called with only `original_messages`), so the worker re-explored
   the codebase from scratch.
4. Replan had no plan-size discipline — new plans kept growing as the
   planner folded in everything the critic complained about.
5. No signal distinguished "worker did real work" from "worker did
   only analysis".

## Solution — 3 file surgical patch

### `lib/tasks_pkg/endpoint_prompts.py`
- Rewrote **PLANNER_SYSTEM_PROMPT**: ≤6000 chars, 2-8 items, concrete-verb
  items only, special re-plan rule ("produce a DELTA, don't grow the plan").
- Rewrote **CRITIC_SYSTEM_PROMPT**: added **BEFORE you verdict** MANDATORY
  pre-check section that says "count state-changing tool calls, 0 = CONTINUE_WORKER",
  mandatory `[PLAN_DEFECT: ...]` tag before `[VERDICT: CONTINUE_PLANNER]`,
  examples of good vs bad defects, length discipline (≤2000 chars).
- Added new **WORKER_DIRECTIVE_HEADER** constant: "START WITH ACTION,
  NOT ANALYSIS. Your first tool call MUST be state-changing."

### `lib/tasks_pkg/endpoint_review.py`
- Added `STATE_CHANGING_TOOLS = {'write_file', 'apply_diff', 'insert_content',
  'run_command', 'create_project', 'generate_image'}` + `code_exec`.
- Added `_count_state_changing_rounds(tool_rounds) → (sc_count, exp_count, names)`.
- Added `_format_deliverables_snapshot(...)` — renders a snapshot block
  with verdict-hint for the critic.
- `_run_critic_turn` now accepts `iteration`, `latest_tool_rounds`,
  `cumulative_state_changing` kwargs and injects the Deliverables
  Snapshot into the critic's invocation prompt.
- `_run_planner_turn` accepts `planner_tag='initial'|'replan-N'` for logs.
- **`_parse_verdict` now returns a 3-tuple `(feedback, next_phase, plan_defect)`**:
  - STOP-with-❌ downgrades to CONTINUE_WORKER (was: CONTINUE_PLANNER).
  - CONTINUE_PLANNER without `[PLAN_DEFECT: ...]` downgrades to CONTINUE_WORKER.
  - CONTINUE_PLANNER with a defect reason containing worker-rationalization
    phrases ("worker didn't…", "remaining ❌", "more iterations") is
    also downgraded.

### `lib/tasks_pkg/endpoint.py`
- Added `MAX_ZERO_DELIVERABLE_TURNS = 2` and `_ZERO_DELIVERABLE_DIRECTIVE` constant.
- `_build_worker_directive(plan)` uses `WORKER_DIRECTIVE_HEADER` from
  endpoint_prompts (single source of truth).
- `_reset_worker_messages_with_plan(..., progress_summary='')` — when
  supplied (replan path), appends a compact assistant-turn summary of
  what already worked so the worker doesn't re-explore from scratch.
- Added `_build_progress_summary(endpoint_turns)` — walks worker turns,
  summarizes state-changing tool calls and narrative, ≤4000 chars.
- `_build_replan_input_messages(..., prior_plan, plan_defect, replan_count)` —
  planner now sees prior plan + defect and is told to produce a DELTA
  with a hard "must not grow" rule.
- Main loop tracks `cumulative_state_changing` and `zero_deliverable_streak`.
- **Zero-deliverable guard**: after `MAX_ZERO_DELIVERABLE_TURNS` consecutive
  worker turns with zero state-changing tool calls, orchestrator **skips**
  the critic and injects `_ZERO_DELIVERABLE_DIRECTIVE` as a synthetic
  CONTINUE_WORKER. Synthetic critic row carries `_isSyntheticCritic=True`.
- Replan branch logs `endpoint_replan_size_violation` audit event when
  new plan > 1.5× prior plan (audit only, doesn't reject).
- Worker turn msg carries `_epStateChangingCount` + `_epExploratoryCount`
  metadata for future UI use.
- Audit events: `endpoint_worker_turn`, `endpoint_zero_deliverable_guard`,
  `endpoint_replan_size_violation`.

## Tests

- `debug/test_endpoint_verdict.py` — updated for 3-tuple return, new
  override semantics (STOP-with-❌→worker), PLAN_DEFECT gate.
- `debug/test_endpoint_replan_loop.py` — fake planner/critic accept new
  kwargs; critic seq includes `plan_defect` field; worker seq has
  state-changing tools in `toolRounds`; asserts `_epStateChangingCount`.
- `tests/test_endpoint_messages.py::TestVerdictParsing` — new tests for
  PLAN_DEFECT gate (with/without defect, worker-rationalization defect
  rejection) and STOP-with-❌→worker.

All 28 pytest endpoint tests + both debug scripts pass.

## Kill switches

- `CHATUI_ENDPOINT_REPLAN=0` — downgrades CONTINUE_PLANNER→CONTINUE_WORKER
  at the parser layer (existing kill switch, still works).
- Flipping MAX_ZERO_DELIVERABLE_TURNS very high (e.g. 100) effectively
  disables the orchestrator-side guard; the critic-prompt-level pre-check
  still drives behaviour.

## Expected impact on the task 00d009c6 pattern

- Plan #1 → Worker(0 deliverables) → Critic sees snapshot, emits
  CONTINUE_WORKER with "execute, stop analyzing" feedback.
- Plan #1 → Worker(0 deliverables) twice → orchestrator-side guard
  skips critic, injects directive, worker must act on 3rd attempt.
- If the critic does emit CONTINUE_PLANNER, it MUST justify with
  `[PLAN_DEFECT: ...]` or be auto-downgraded to CONTINUE_WORKER.
- If replan is legitimate, worker carries progress summary into the
  new context (no re-exploration) and planner is instructed to produce
  a DELTA not a rewrite.

