---
name: chatui-proactive-agent-scheduler
description: Proactive Agent Scheduler: poll→decide→execute lifecycle for task_type='agent' — cheap model polls, full agentic execution in target conversation, visible in frontend via SSE, scheduler panel dropdown with poll log
enabled: true
tags: [python, javascript, scheduler, proactive-agent, architecture, cron, poll, llm, tools]
created: 2026-03-31T11:03:59Z
updated: 2026-03-31T11:03:59Z
---

# Proactive Agent Scheduler

## Architecture

The proactive agent extends the scheduler with `task_type='agent'`:

### Three Phases
1. **Define** — User tells LLM (via chat) to create a proactive task using `schedule_create` tool with `task_type='agent'`
2. **Poll (Phase B)** — At each cron tick, a lightweight cheap-model LLM call decides whether to act (independent context per poll, no history between polls)
3. **Execute (Phase C)** — If poll says act=true, creates a full agentic task in the target conversation with all tools, SSE streaming, visible to frontend

### Key Files
- `lib/scheduler/proactive.py` — Poll engine, status gathering, execution trigger
- `lib/scheduler/manager.py` — `_run_proactive_poll()` method, `task_type='agent'` support
- `lib/scheduler/tool_defs.py` — Updated `schedule_create` with agent fields
- `lib/scheduler/executor.py` — `_source_conv_id` injection for 'current' conv resolution
- `lib/tasks_pkg/model_config.py` — `schedulerEnabled` config → `_assemble_tool_list` wiring
- `lib/tasks_pkg/manager.py` — `_update_proactive_execution_status()` hook in `persist_task_result()`
- `routes/scheduler.py` — REST endpoints: poll-log, proactive/status, trigger, pause/resume
- `routes/__init__.py` — `start_scheduler_worker()` called from `register_all()`

### Database
- `scheduled_tasks` table gains proactive columns: target_conv_id, tools_config, poll_count, last_poll_at/decision/reason, execution_count, max_executions, expires_at
- `proactive_poll_log` table: per-poll journal (task_id, poll_time, decision, reason, tokens, exec_task_id)
- Schema migration via ALTER TABLE for existing installs (version 1→2)

### Frontend
- `schedulerEnabled` toggle already existed, now wired through backend
- Scheduler badge is clickable → dropdown panel showing all proactive tasks with poll status
- Proactive messages tagged with `_proactive: true` and `_proactiveTaskId` → rendered with ⏰ banner
- Panel has Trigger/Log/Pause/Resume buttons per task
- Auto-refreshes every 60s

### Token Economy
- Each poll: ~500 tokens (cheap model, no tools, no history)
- Execution: full token cost (same as user-initiated task)
- Polls are stateless — no cross-poll history

### Key Design Decisions
- Poll uses `smart_chat(capability='cheap')` for low cost
- Execution happens in target conversation, visible as normal task
- If previous execution still running, poll is skipped
- max_executions for auto-disable (one-shot tasks use max_executions=1)
- expires_at for time-limited tasks

