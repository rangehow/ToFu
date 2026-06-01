---
name: mandatory-approval-before-disruptive-actions
description: MANDATORY: Always ask user approval before: server restart/stop, modifying hyperparameters, model routing, DB schema, security config, temperature, max_tokens, retry counts, timeouts, rate limits, dispatch tables, killing processes, or any disruptive operational command
enabled: true
tags: [mandatory, approval, server-restart, hyperparameters, config, security, schema, dispatch, guard-rail, operational]
created: 2026-03-24T08:42:19Z
updated: 2026-03-24T09:00:49Z
---

# Mandatory Approval Before Disruptive Actions

> **STOP and ASK before making any of these changes or actions. No exceptions.**

## Categories Requiring Explicit User Approval

### 0. ⚠️ Server Restart / Stop / Process Kill (HIGHEST PRIORITY)
**NEVER execute these directly — always confirm with the user first:**
- Restarting the server (e.g., `kill`, `pkill`, `systemctl restart`, re-running `python app.py`)
- Stopping or killing any running process (`kill`, `pkill`, `killall`, `Ctrl-C`)
- Restarting background workers, schedulers, or cron jobs
- Any command that causes **downtime** or **service interruption**

**Correct behavior:**
> "The server needs to be restarted for these changes to take effect. Shall I restart it now?"

### 1. Hyperparameter & Configuration Changes
Any numeric/boolean constant that affects model behavior or system performance:
- **LLM parameters**: temperature, top_p, top_k, max_tokens, frequency_penalty, presence_penalty, stop sequences
- **Retry & timeout settings**: retry counts, backoff multipliers, request timeouts, SSE timeouts
- **Token budgets**: context window sizes, compaction thresholds, layer boundaries, max tool result lengths
- **Rate limiter settings**: RPM, TPM, concurrency caps, cooldown periods
- **Batch/queue sizes**: thread pool sizes, chunk sizes, polling intervals

### 2. Model Routing & Dispatch Logic
- Default model assignments or model aliases
- API key rotation rules or priority ordering
- Fallback chains or model capability mappings
- Any change to `lib/llm_dispatch.py` routing tables

### 3. Database Schema Changes
- Any `ALTER TABLE`, new tables, new indexes, or column changes to `data/chatui.db`
- Changes to migration scripts or DB initialization logic

### 4. Security-Sensitive Changes
- Authentication/authorization logic
- CORS, CSP, or proxy configurations
- API key handling or storage
- File system access permissions in `lib/project_mod/`

### 5. Destructive File Operations
- Deleting files or directories (`rm`, `rm -rf`)
- Overwriting database files or backups
- Git force push or branch deletion

## Compliance Steps

When a task touches ANY of the above:
1. **STOP** before executing the command or making the change.
2. **Present** what you want to do, why, and what the impact will be.
3. **WAIT** for explicit user approval (e.g., "yes", "go ahead", "approved").
4. Only then execute.

For config changes specifically, also:
- Present the current value, proposed new value, and reasoning.
- Log with `audit_log('config_change', param=name, old=old_val, new=new_val, approved_by='user')`.

## Reference
Full project conventions are in `CLAUDE.md` at the project root — read it at the start of any session involving significant code changes.

