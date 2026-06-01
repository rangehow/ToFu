---
name: run-command-abort-kill-subprocess
description: Fix: run_command now checks task['aborted'] every 0.5s and kills subprocess — prevents unkillable zombie commands when user clicks Stop
enabled: true
tags: [bug-fix, run_command, abort, subprocess, zombie, kill, process-group]
created: 2026-04-16T09:09:55Z
updated: 2026-04-16T09:09:55Z
---

# run_command Abort-Aware Subprocess Kill

## Problem
When user clicks Stop on a task running `run_command` with long/unlimited timeout,
the subprocess keeps running because `subprocess.run()` blocks until completion.
The `task['aborted']` flag is only checked between tool rounds, not during execution.
This causes:
- "Unkillable" tasks that keep running despite abort
- Multiple tasks spawning for same conversation (old zombie + new)
- User frustration: "every time I stop, another agent is created"

## Fix (3 files)

### 1. `lib/project_mod/tools.py`
- `_run_command_simple()` now uses `Popen` + polling loop instead of `subprocess.run()`
- Checks `task['aborted']` every 0.5s
- Stores `_subprocess_pid` and `_subprocess_pgid` on task dict
- `_kill_process_tree()` kills via process group (SIGTERM → SIGKILL fallback)
- `start_new_session=True` on Popen so subprocess gets own process group

### 2. `lib/tasks_pkg/handlers/project.py`
- Passes `task=task` kwarg to `execute_tool()` for `run_command` calls

### 3. `routes/chat.py` → `chat_abort()`
- Reads `_subprocess_pid` / `_subprocess_pgid` from task
- Sends SIGTERM to process group immediately on abort (before backend kills)
- This provides instant kill even before the polling loop notices the flag

