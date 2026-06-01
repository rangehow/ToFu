---
name: stdin-detection-rg-false-positive-and-timeout
description: Complete 3-layer fix for stdin detection: allowlist + auto-close stdin for non-interactive commands + timeout
enabled: true
tags: [bug-fix, stdin, rg, ripgrep, false-positive, timeout, task-hang]
created: 2026-04-12T02:47:59Z
updated: 2026-04-12T05:26:25Z
---

# Stdin Detection False Positive & Missing Timeout Bug

## Root Cause
When `run_command` executes a pipeline like `rg -l ... | sort`, the child processes inherit
the stdin pipe we created. `rg` detects its stdin is a pipe (not a tty) and tries to read
from it as a data source. `_is_any_child_reading_stdin()` sees `rg` blocked on `read(0, ...)`
and triggers the stdin-wait flow.

The stdin handler (`request_stdin()`) then blocks **indefinitely** waiting for user input
that never comes, permanently hanging the task thread.

## Fixes Applied (3-layer defense)

### 1. Non-interactive command allowlist
`_NON_INTERACTIVE_COMMANDS` frozenset in `lib/project_mod/tools.py`:
- Commands like `rg`, `grep`, `sort`, `awk`, etc.
- `_is_any_child_reading_stdin()` skips these when checking for interactive prompts

### 2. Auto-close stdin for non-interactive readers (KEY FIX)
The allowlist alone wasn't sufficient — it prevented the interactive prompt from showing,
but `rg` was still blocked on `read(0)` waiting for data that never comes (infinite hang).

`_is_any_child_reading_stdin()` now returns `_STDIN_NON_INTERACTIVE` sentinel when only
non-interactive commands are reading stdin. The caller closes the pipe immediately, sending
EOF so `rg` proceeds (reads nothing from stdin → completes quickly with no output).

Note: `rg` with no explicit path and a pipe on stdin reads from stdin, not the directory.
Commands like `rg -l pattern --type py | sort` need an explicit `.` to search the directory:
`rg -l pattern --type py . | sort`

### 3. 120s timeout in `lib/tasks_pkg/stdin_handler.py`
`_STDIN_TIMEOUT = 120.0` — auto-closes stdin after 2 minutes as a safety net.

## Symptoms
- Task appears stuck with no progress in logs after stdin detection
- DB has partial checkpoint data but task never completes
- Frontend shows truncated content with no finish marker
- Stuck `rg`/`sort` processes visible in `ps aux`

## Important: Server Restart Required
Changes to `lib/project_mod/tools.py` require a server restart — the running server keeps
the old module in memory even after the `.py` file is updated on disk.

