---
name: code-server-fileWatcher-OOM-and-user-settings-sync
description: code-server fileWatcher OOM/runaway CPU recurs whenever a NEW *_workdir eval scratch dir appears (abtest_workdir, swebench_rerun_workdir); fix = generic **/*_workdir/** glob in .vscode/settings.json + rerun lib.code_server_excludes
enabled: true
tags: [code-server, vscode, oom, filewatcher, settings, swebench_workdir, startup-hook]
created: 2026-05-06T04:17:37Z
updated: 2026-06-04T06:19:56Z
---

## Symptom
Shared host watcher (`bootstrap-fork --type=fileWatcher`) pegs CPU (saw a PID at 62%, 19min CPU time) and/or balloons RSS. `ls -l /proc/<pid>/fd | grep -v socket` shows it recursing into `chatui/<something>_workdir/workspaces*/django__django-*` repo checkouts. Earlier (2026-05-06) it was 28 workers × 6.7 GB = 189 GB.

## Root cause (two-part — BOTH must hold for the fix to last)
1. `.vscode/settings.json` is **workspace-scoped** — only loaded when chatui is the workspace root. User opens the *parent* dir on a shared box → not loaded. Fixed by `lib/code_server_excludes.py` syncing excludes into User-scope `~/.local/share/code-server/User/settings.json` (+4 fallbacks). This part keeps working.
2. **The exclude list goes stale.** New eval/benchmark scratch dirs keep appearing at chatui root (each holds thousands of full repo checkouts): originally `swebench_workdir/`, later `abtest_workdir/`, `swebench_rerun_workdir/`. Any new one NOT in excludes → watcher recurses → OOM/CPU again. This is the recurring failure mode.

## Fix (2026-06-04)
Added to ALL FOUR exclude sections in `.vscode/settings.json` (watcherExclude, search.exclude, python.analysis.exclude, files.exclude):
- specific: `**/swebench_rerun_workdir/**`, `**/abtest_workdir/**`
- **generic catch-all: `**/*_workdir/**`** (and bare `**/*_workdir` for the list/search forms) — so FUTURE `*_workdir` dirs are auto-excluded with no further code change.
Then ran `python -m lib.code_server_excludes` to merge into User settings (idempotent, additive merge, never flips user `false`).

## Diagnosis recipe (do this FIRST, don't assume)
- `free -g` and `ps aux --sort=-rss | head` — confirm whether memory is actually tight now or it's a slow leak.
- `dmesg | grep -i 'oom\|killed process'` — confirm real OOM kills.
- Find the watcher PID(s): `ps aux | grep fileWatcher`, then `ls -l /proc/<pid>/fd | grep -v 'socket\|pipe\|anon_inode'` to see WHICH dir it's walking. Don't assume it's the previously-fixed dir.

## Guardrail / recovery
- After editing `.vscode/settings.json`, ALWAYS rerun `python -m lib.code_server_excludes` (User-scope is what code-server reads when parent is workspace root).
- **code-server only re-reads settings on window reload** — tell user to refresh the browser tab / run "Developer: Reload Window". The already-running runaway watcher keeps its old watch set (and its RSS/CPU) until then; only a reload or killing that watcher clears it.
- The generic `**/*_workdir/**` glob should prevent recurrence for workdir-named dirs; any heavy dir with a different name still needs adding by hand.

## Files
- `lib/code_server_excludes.py` — startup daemon-thread sync (mirrors `lib/fs_keepalive.py` pattern); wired in `server.py` after `start_fs_keepalive()`.
- `.vscode/settings.json` — canonical source of truth for the exclude globs.

