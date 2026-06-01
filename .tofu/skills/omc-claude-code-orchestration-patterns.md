---
name: omc-claude-code-orchestration-patterns
description: OMC Atlas/Sisyphus + Claude Code AgentTool/TodoWriteTool orchestration patterns compared to Tofu — gap analysis and 6 backport recommendations
enabled: true
tags: [orchestration, architecture, swarm, endpoint-mode, analysis]
created: 2026-04-03T05:54:55Z
updated: 2026-04-03T05:54:55Z
---

# OMC / Claude Code Orchestration Patterns → Tofu Backport

## Key Patterns to Backport (Priority Order)

### 1. Structured Todo Tracking (HIGH)
- OMC: TodoWrite tool + todo continuation enforcer hook
- CC: TodoWriteTool (pending/in_progress/completed), verification nudge at 3+ items
- Tofu gap: No structured todo tracking — planner brief is text-only
- Implement: `lib/tools/todo.py` new tool + handler in executor

### 2. Todo Continuation Enforcer (HIGH)
- OMC: Hook injects "You have incomplete todos!" when agent tries to stop
- Implement: In orchestrator.py, after finish_reason='stop', check task['_todos'] and re-inject user message
- Depends on #1

### 3. Adversarial Critic Prompt (MEDIUM)
- CC: Verification agent has anti-rationalization language, evidence requirements
- Key phrases: "Reading code is not verification. Run it." / "Try to break it"
- Implement: Update CRITIC_SYSTEM_PROMPT in endpoint_prompts.py
- ⚠️ Requires user approval (§10 — prompt is hyperparameter-adjacent)

### 4. 6-Section Delegation Template (MEDIUM)
- OMC: Task, Expected Outcome, Required Tools, Must Do, Must Not Do, Context
- Implement: Extend SubTaskSpec + build structured user message in SubAgent

### 5. Planner Write-Block Hook (MEDIUM)
- OMC/CC: Planners are read-only enforced via hooks/disallowedTools
- Implement: Pre-hook in tool_hooks.py blocking write_file/apply_diff during _endpoint_phase='planning'

### 6. Swarm Wisdom Passing (LOW)
- OMC: Extract learnings per task → pass to subsequent tasks
- Tofu already has session_memory + skills — gap is within-swarm-run passing

## What Tofu Does Better
- DB-persistent endpoint turns (vs OMC's boulder.json file)
- StreamingScheduler with DAG (vs OMC's sequential/parallel without scheduler)
- Reactive master review with incremental prompts
- Session memory + skills system (cross-session learning)
- Suspicious completion detection

## Not Applicable to Tofu (Terminal-Specific)
- Hashline, LSP tools, Tmux backend, fork subagent (prompt cache sharing), AGENTS.md hierarchy

## Key Files
- `lib/tasks_pkg/endpoint.py` — Planner→Worker→Critic loop
- `lib/tasks_pkg/endpoint_prompts.py` — Prompts for planner/critic
- `lib/tasks_pkg/tool_hooks.py` — Hook infrastructure (pre/post hooks)
- `lib/swarm/master.py` — MasterOrchestrator with reactive mode
- `lib/swarm/agent.py` — SubAgent implementation
- `lib/swarm/registry.py` — Role-based tool scoping + model tiers
- `lib/tasks_pkg/session_memory.py` — Background memory extraction
- `docs/omc-claude-code-backport-analysis.md` — Full analysis document

