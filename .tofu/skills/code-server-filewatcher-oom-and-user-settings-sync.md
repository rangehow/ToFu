---
name: code-server-fileWatcher-OOM-and-user-settings-sync
description: code-server fileWatcher OOM (189 GB / 28 workers) when workspace root is above project — fix via lib/code_server_excludes.py syncing excludes to ~/.local/share/code-server/User/settings.json
enabled: true
tags: [code-server, vscode, oom, filewatcher, settings, swebench_workdir, startup-hook]
created: 2026-05-06T04:17:37Z
updated: 2026-05-06T04:17:37Z
---

# code-server fileWatcher OOM — root cause & fix

## Symptom
Shared host shows ~28 `bootstrap-fork --type=fileWatcher` processes,
each ~6.7 GB RSS, total ~189 GB. Parent is a single code-server process
(`/workdir/cloud-ide/code-server/out/node/entry`).

## Root cause
The chatui project ships `.vscode/settings.json` with `files.watcherExclude`
covering `**/swebench_workdir/**`, `**/data/**`, `**/logs/**`, etc.

**BUT** `.vscode/settings.json` is **workspace-scoped** — only loaded when
`chatui/` itself is the workspace root. On shared dev boxes the user often
opens the *parent* dir (`ruanjunhao04/`) so they can see chatui alongside
sibling projects. Then chatui's settings are NEVER loaded, the watcher
recurses into `swebench_workdir/eval/` (3,637 repo checkouts), and each
worker balloons.

**Diagnostic**: check `/proc/<pid>/fd/` of a fileWatcher — if you see FDs
into `swebench_workdir/eval/<dir>/...`, settings aren't being applied.

## Fix
`lib/code_server_excludes.py` — daemon-thread startup hook in `server.py`
that mirrors the project's `.vscode/settings.json` exclude keys
(`files.watcherExclude`, `search.exclude`, `python.analysis.exclude`) into
the **User-scope** settings file:
- `~/.local/share/code-server/User/settings.json` (primary target)
- plus 4 fallback paths (vscode-server, Code, Insiders, VSCodium)

Wired in `server.py` after `start_fs_keepalive()` via:
```python
from lib.code_server_excludes import start_code_server_excludes_sync
start_code_server_excludes_sync()
```

### Properties
- **Idempotent** — fast no-op when already synced
- **User-override-safe** — never flips a user-set `false` back to `true`
- **JSONC-tolerant** — comments OK on input
- **String-aware comment stripper** — naïve regex `/\*.*?\*/` would corrupt
  glob patterns like `"**/data/**"` (the `*/` inside the string looks like
  comment close). Fixed with char-by-char scanner tracking string state.
  See `_strip_jsonc()`.
- **Atomic write** (tempfile + `os.replace`)
- **Best-effort** — exceptions logged, never propagated
- **CLI mode**: `python -m lib.code_server_excludes` prints summary dict

### IMPORTANT — user must reload window
code-server re-reads settings only on window reload. After sync, tell user
to refresh browser tab or run "Developer: Reload Window".

## Pattern: where startup hooks live
Mirrors `lib/fs_keepalive.py`. Real logic in `lib/<feature>.py`, server.py
just has 5-line `try / import / call / except → log` block. Keeps
server.py thin and the feature unit-testable.

