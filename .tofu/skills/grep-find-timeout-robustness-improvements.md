---
name: grep-find-timeout-robustness-improvements
description: Grep/find timeout and robustness improvements: increased timeouts, rg max-filesize/depth caps, .gitignore for Python fallbacks. NO preset dir exclusions.
enabled: true
tags: [performance, grep, find, timeout, robustness, fuse, project-tools, user-preference]
created: 2026-04-15T05:49:40Z
updated: 2026-04-16T10:07:48Z
---

# Grep/Find Timeout Robustness (updated 2026-04-16)

## Root Cause of Chronic Timeouts (2026-04-16)
**rg, fd, and grep only auto-respect `.gitignore` when inside a git repo (`.git/` present).**
When the project has no `.git/` dir (e.g. exported copies, workdir deployments), `.gitignore`
is completely ignored, causing tools to crawl into massive directories like `swebench_workdir/`
(1500+ workspaces, 1400+ eval dirs, 64 conda envs, 9 full repo clones = 62K+ files at depth 3).

**Fix**: `_build_rg_cmd()` and `_fd_find()` now pass `--ignore-file .gitignore` when `.git/`
is absent. `_build_grep_cmd()` uses `_load_gitignore_dirs()` to add `--exclude-dir` flags.

**Impact**: grep went from 60s+ timeout → 0.27s (222x faster), find from timeout → 0.11s.

## Kept Improvements (from 2026-04-15)
1. **Increased base timeouts**: rg/grep 30→60s, fd 15→30s, Python grep 20→40s, Python find 15→30s
2. **rg safety caps**: `--max-filesize 2M` + `--max-depth 30` — skip huge binaries, cap recursion depth
3. **fd safety caps**: `--max-depth 30`
4. **.gitignore awareness**: Python fallback walkers parse `.gitignore` for basic glob matching (best-effort when rg/fd unavailable)

## User-Rejected Approaches (DO NOT add these back)
- ❌ **_EXTRA_PRUNE_DIRS** (preset list of dirs like `site-packages`, `wandb`, etc.) — user explicitly rejected this
- ❌ **_is_heavy_dir()** (skip dirs with >10K entries) — user rejected
- ❌ **_MAX_WALK_DEPTH** (cap Python walker depth at 20) — user rejected

## Key constants in `lib/project_mod/read_tools.py`
```python
_RG_MAX_FILESIZE = '2M'      # rg skips files larger than this
_TOOL_MAX_DEPTH = 30          # max depth for rg/fd
_get_io_timeout(base, default=60)  # cross-DC aware timeout with 60s default
```

## Key functions
- `_build_rg_cmd(base, target, ...)` — now takes `base` to check for `.git/` and `.gitignore`
- `_build_grep_cmd(base, target, ...)` — now takes `base` to load gitignore dirs
- `_load_gitignore_dirs(path)` — NEW: extracts simple dir names from .gitignore for grep --exclude-dir
- `_fd_find(target, base, ...)` — now adds `--ignore-file` when no `.git/`

## Cross-DC awareness
`_get_io_timeout()` uses `lib.cross_dc.get_timeout_multiplier()` to auto-scale timeouts for slow FUSE mounts.

## Philosophy
The timeout message tells users to narrow their search path rather than silently excluding directories. Users should always be able to search anywhere they want.

