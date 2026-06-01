---
name: swebench-git-add-timeout-bug
description: SWE-bench runner _extract_git_diff had 10s timeout on git add -A, causing 79 empty patches on FUSE
enabled: true
tags: [swebench, bug, fuse, git, timeout]
created: 2026-04-16T12:06:35Z
updated: 2026-04-17T12:04:25Z
---

---
name: swebench-git-add-timeout-bug
description: SWE-bench _extract_git_diff: 10s timeout AND silent non-zero exit bug caused false-empty patches
enabled: true
tags: [swebench, bug, fuse, git, timeout]
---

# SWE-bench Runner: `_extract_git_diff` Bugs (Two-Layer)

## Bug 1: Timeout too short (fixed 2026-04-16)
`_extract_git_diff()` used `timeout=10` for `git add -A`. On FUSE/NFS under concurrent
load (6+ parallel workers), `git add -A` routinely took 4-10+ s; on timeout the outer
`except Exception` caught `TimeoutExpired` → returned `''` → empty patch silently recorded.

Fix: raised to `timeout=120` and added unstaged-diff fallback.

## Bug 2: Non-zero exit silently treated as success (fixed 2026-04-17)
Even after Bug 1 was fixed, `subprocess.run(['git', 'add', '-A'], ...)` does NOT raise
on non-zero exit (only on timeout). If an earlier run left a stale `.git/index.lock`,
`git add` returned `rc=128` with no exception — the code silently proceeded to
`git diff --cached` which found nothing staged → returned `''` → false empty.

Fix (2026-04-17):
  - Remove stale `.git/index.lock` at start of `_extract_git_diff`.
  - Use helper `_run()` that never raises on non-zero, logs actual rc.
  - Check `r.returncode == 0` explicitly; fall back to `git diff` (unstaged) then
    `git diff HEAD` if staged diff fails or is empty.
  - Fall back to `git status --porcelain` for file listing if both diff commands fail.
  - Log a warning when "all diff attempts empty" despite changed files (proves we
    tried, so callers know it's not silently dropped).

## Recovery procedure (2026-04-17)
1. `debug/swebench_recover_empty_patches.py` — scan `patches/*.diff` for empty
   placeholders; re-extract from `workspaces/<stem>/` using fixed `_extract_git_diff`.
   Writes `recovery_manifest.json` with recovered/genuinely_empty/failed lists.
2. `debug/swebench_reeval_recovered.py` — read the manifest, re-run
   `evaluate_patch()` on ONLY the recovered ones (parallel, 8 workers). Patches
   `swebench_results.json` in-place and rewrites `details/{stem}.json`.

### Results of 2026-04-17 recovery
- 82 empty patches scanned → 39 recovered, 43 genuinely empty.
- Of the 39 recovered, **13 resolve correctly** when evaluated (33% flip rate).
- 13 fewer false-negatives across 6 models was a ~1-3 pp score bump per model.
- Pathological workspaces with 4000+ "changed" tracked files (from prior retry
  that was interrupted mid-checkout) correctly reported as empty via the
  "All diff attempts empty" log line — not silent.

## Key Lessons
1. On FUSE/NFS, git operations can be 10-100x slower — always use 60s+ timeouts.
2. `subprocess.run()` does NOT raise on non-zero exit by default — check `returncode`.
3. Always preserve per-instance workspaces even after "empty patch" verdicts so
   bugs in extraction can be fixed in post-processing without re-spending inference $.
4. Concurrent git on shared FUSE leaves `index.lock` files — clean them defensively.

