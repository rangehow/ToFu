---
name: grep-find-timeout-robustness-improvements
description: Grep/find FUSE timeout: recurs when a NEW unignored *_workdir eval dir appears; fix = `*_workdir/` glob in .gitignore (rg/grep honor it only if .git/ present). Plus timeout/depth caps.
enabled: true
tags: [performance, grep, find, timeout, robustness, fuse, project-tools, user-preference]
created: 2026-04-15T05:49:40Z
updated: 2026-06-16T23:53:23Z
---

# Grep/Find Timeout Robustness (updated 2026-06-16)

## RECURRING Root Cause #1: new unignored *_workdir eval dir (2026-06-16)
**Symptom:** `grep_search` / `rg .` at repo root times out (>40s) on the FUSE mount even though
rg DOES respect `.gitignore`. `rg --files | wc -l` shows 400K+ files.

**Diagnosis recipe** (fast, each dir capped):
```bash
rg --files 2>/dev/null | sed 's|^\./||' | cut -d/ -f1 | sort | uniq -c | sort -rn | head
```
This breaks the walked-file count down by top-level dir. The culprit is always a huge eval
scratch dir that is NOT matched by `.gitignore`. On 2026-06-16 it was
`swebench_glm_ab_workdir/` (701K files) + `swebench_pilot_workdir/` (120K files) — the
`.gitignore` only listed `swebench_workdir/ swebench_rerun_workdir/ abtest_workdir/` by exact name,
so the two new dirs were scanned. Each eval dir holds repo clones + conda envs.

**Fix:** replace the explicit per-name list in `.gitignore` with a GLOB:
```
# ── Eval / bench workdirs (large, causes FUSE timeouts) ──
*_workdir/
```
Result: 424,176 → 1,254 files walked; root grep 40s+ → **0.26s**. Verify with
`git check-ignore -q swebench_glm_ab_workdir && echo IGNORED`.

This is the SAME pattern as the code-server fileWatcher OOM memory — both fixed by a generic
`*_workdir` glob, not by chasing individual names. When a new eval dir appears, prefer the glob.

**Why rg/grep honor .gitignore here:** they auto-respect it only inside a git repo (`.git/` present).
For exported/workdir copies WITHOUT `.git/`, `_build_rg_cmd` passes `--ignore-file .gitignore`
and `_build_grep_cmd` parses dir names via `_load_gitignore_dirs()`.

## Kept Improvements
1. **Increased base timeouts**: rg/grep 30→60s, fd 15→30s, Python grep 20→40s, Python find 15→30s
2. **rg safety caps**: `--max-filesize 2M` (`_RG_MAX_FILESIZE`) + `--max-depth 30` (`_TOOL_MAX_DEPTH`)
3. **fd safety caps**: `--max-depth 30`
4. **.gitignore awareness** for Python fallback walkers (best-effort glob)

## User-Rejected Approaches (DO NOT add these back)
- ❌ **_EXTRA_PRUNE_DIRS** (preset list like `site-packages`, `wandb`) — explicitly rejected
- ❌ **_is_heavy_dir()** (skip dirs with >10K entries) — rejected
- ❌ **_MAX_WALK_DEPTH** (cap Python walker depth at 20) — rejected
The sanctioned lever is `.gitignore` (which the user controls), NOT in-code dir blacklists.

## Key locations in `lib/project_mod/read_tools.py`
- `_build_rg_cmd(base, target, …)` / `_build_grep_cmd(base, target, …)` — add ignore handling
- `_load_gitignore_dirs(path)` — dir names for GNU grep `--exclude-dir`
- `_get_io_timeout(base, default=60)` — cross-DC aware via `lib.cross_dc.get_timeout_multiplier()`
- `_fd_find(target, base, …)` — adds `--ignore-file` when no `.git/`

## Philosophy
Timeout message tells the user to narrow the path rather than silently excluding dirs.
But genuinely-junk eval scratch trees belong in `.gitignore` via a glob.

