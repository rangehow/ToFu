---
name: code-server-fileWatcher-OOM-and-user-settings-sync
description: code-server fileWatcher OOM recurs on new heavy dirs at PARENT workspace root; 2026-06 it was tofu-experiment/udocker_root/.udocker texlive tree (55GiB/2 workers). Fix=add **/udocker_root/** + **/.udocker/** to .vscode/settings.json AND User-scope, rerun lib.code_server_excludes, kill+respawn watchers. *_workdir glob does NOT cover udocker.
enabled: true
tags: [code-server, vscode, oom, filewatcher, settings, swebench_workdir, startup-hook]
created: 2026-05-06T04:17:37Z
updated: 2026-06-17T06:26:45Z
---

## Symptom
Shared host watcher (`bootstrap-fork --type=fileWatcher`) pegs CPU and/or balloons RSS. `ls -l /proc/<pid>/fd | grep -v socket` shows it recursing into a heavy dir holding huge file trees. Seen: 2026-05-06 28 workers × 6.7 GB = 189 GB; 2026-06-17 **2 workers × 27.5 GiB = 55 GiB** recursing into `ruanjunhao04/tofu-experiment/udocker_root/.udocker/containers/<id>/ROOT/usr/share/texlive/...` (full TeXLive distro inside a udocker container ROOT — hundreds of thousands of tiny FUSE files).

## Root cause (two-part — BOTH must hold for the fix to last)
1. `.vscode/settings.json` is **workspace-scoped** — only loaded when chatui is the workspace root. On a shared box the user opens the *parent* dir (`ruanjunhao04`), so the watch root is the parent and chatui's excludes never apply. Fixed by `lib/code_server_excludes.py` syncing excludes into User-scope `~/.local/share/code-server/User/settings.json` (+4 fallbacks). **User-scope is the one that actually matters when the workspace root is the parent dir.**
2. **The exclude list goes stale.** New heavy dirs keep appearing at/under the parent: `swebench_workdir/`, `abtest_workdir/`, `swebench_rerun_workdir/`, and (2026-06) **`tofu-experiment/udocker_root/` + `.udocker/`**. The generic `**/*_workdir/**` catch-all does NOT match `udocker_root`/`.udocker` — those needed explicit globs.

## Fix
Add to ALL FOUR exclude sections in BOTH `.vscode/settings.json` AND User-scope settings (watcherExclude, search.exclude, python.analysis.exclude, files.exclude):
- workdir: `**/swebench_rerun_workdir/**`, `**/abtest_workdir/**`, generic `**/*_workdir/**`
- udocker (2026-06): `**/udocker_root/**`, `**/.udocker/**` (and bare `**/udocker_root`, `**/.udocker` for list/search forms)
Then run `python -m lib.code_server_excludes` (idempotent additive merge into User scope, never flips user `false`).

## Diagnosis recipe (do this FIRST, don't assume)
- `free -g` + `ps -eo pid,ppid,rss,comm --sort=-rss | head` — confirm tightness and identify hogs. Full cmdline via `tr '\0' ' ' < /proc/<pid>/cmdline`.
- `dmesg | grep -i 'oom\|killed process'` — confirm real OOM kills (may be unavailable in container).
- Find watcher PIDs: `pgrep -af 'type=fileWatcher'`, then `ls -l /proc/<pid>/fd | grep -vE 'socket|pipe|anon_inode'` to see WHICH dir it walks. The new heavy dir is usually a SIBLING of chatui, not the previously-fixed one.
- Watch for stuck/zombie `pytest` (state `S`, CPU ticks `$14+$15` in /proc/<pid>/stat barely advancing over hours) — kill those too; each can pin ~10 GiB + a DB conn.

## Restart sequence (frees the leaked RSS)
- code-server only re-reads settings on **window reload** OR when a watcher is killed+respawned; the running bloated watcher keeps its RSS until then.
- Verify identity right before kill (PID reuse). `kill <watcher_pids>` (SIGTERM enough) → parent `code-server out/node/entry` respawns fresh lightweight workers honoring the new excludes (saw 27.5 GiB → 0.05 GiB fresh). Confirm via another `pgrep -af 'type=fileWatcher'` + RSS check.
- 2026-06-17 net: killed 2 zombie pytest + 2 orphan `du` (~22 GiB) and 2 bloated watchers (~55 GiB) → `used` 104→28 GiB, `available` 94→170 GiB.

## Guardrail / recovery
- After editing `.vscode/settings.json`, ALWAYS rerun `python -m lib.code_server_excludes`.
- `**/*_workdir/**` only covers `*_workdir`-named dirs; any other heavy dir (udocker, texlive, node_modules trees, conda envs, sibling repos) needs adding by hand.

## Files
- `lib/code_server_excludes.py` — startup daemon-thread sync (mirrors `lib/fs_keepalive.py`); wired in `server.py` after `start_fs_keepalive()`. `sync_once()` ~line 292; User-scope target list lines 78-82.
- `.vscode/settings.json` — canonical source of truth for the exclude globs.
