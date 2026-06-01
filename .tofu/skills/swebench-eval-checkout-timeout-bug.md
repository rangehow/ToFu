---
name: swebench-eval-checkout-timeout-bug
description: MAJOR: SWE-bench runner evaluate_patch() had 30s git-checkout timeout — under FUSE+concurrency, 60% of 'failures' were false patch_applies=False. Fix: 300s timeout + retry with stale-lock cleanup + env-var overrides
enabled: true
tags: [swebench, bug-fix, critical, git, timeout, fuse, eval, harness]
created: 2026-05-05T03:03:33Z
updated: 2026-05-05T03:03:33Z
---

# SWE-bench Eval: Git Checkout Timeout False Failures (2026-05-05)

## The bug
`debug/swebench_runner.py::evaluate_patch` used a 30-second timeout on
`git checkout --quiet BASE_COMMIT` inside the eval workspace.

Under the rerun's 9 concurrent workers × FUSE + shared conda_envs symlink +
active inference workspaces on the same filesystem, that 30s was too
tight. The checkout would time out → raise TimeoutExpired → be caught
by `except Exception` → logged as "Workspace setup failed" → `patch_applies=False`
→ `resolved=False`.

Worse: the timeout left orphan `.git/index.lock` files behind (44/46 workspaces
still had one at analysis time). Any retry would fail too.

## Impact
First 190 details of the 2026-05-04 rerun showed **46 of 77 failures (60%)** were
this exact harness bug. For many of these instances:
- The model's patch was saved correctly (patches/*.diff had the right content).
- The patch was byte-identical to CC's winning patch on the same instance.
- But resolved=False because our eval harness never got past git checkout.

Resolve rates before vs after recovery (on the 190 shared instances):
- tofu-opus:   **67.0% → 78.5%** (+11.5pp)
- tofu-minimax: **51.2% → 72.7%** (+21.5pp)
- tofu-glm:    **52.6% → 76.9%** (+24.3pp)

After the recovery, tofu is within ~3-5pp of CC on all three model tiers
(was 15-28pp behind).

## Fix applied (in `debug/swebench_runner.py::evaluate_patch`)
1. `git clone` timeout: 300s → **600s**
2. `git checkout` timeout: 30s → **300s** (the critical one)
3. `git clean` timeout: 30s → **120s**, and now NON-FATAL
4. New `_git_run()` helper: retries on `TimeoutExpired`, clears stale
   `.git/index.lock` before each attempt, kills lingering git processes
   via `pkill -f 'git.*<workspace>'` between retries.
5. Env-var overrides: `SWEBENCH_GIT_{CLONE,CHECKOUT,CLEAN}_TIMEOUT`.

## Recovery workflow (run when this bug shows up again)
1. Grep details for failures with `"'git', 'checkout'" in err and 'timed out'`.
2. Confirm patches/*.diff still has the inference output for those rows.
3. Run `python3 debug/swebench_reeval_checkout_timeouts.py --workdir <dir>`.
   This re-runs eval-only using the saved patch; no new inference cost.
4. Backup of results.json saved to `*.before_reeval_checkout.json` automatically.
5. On 46 rows (2026-05-05), 33 flipped to resolved, 0 flipped to failed.

## Why it manifested only this run
- The old `swebench_workdir/` run had 0-4 checkout timeouts total (across 412
  rows × 6 tools = 2472 evals).
- The new run (same code) had 46 in ~190 runs.
- Difference: FUSE latency was worse this time (10.6 ms vs <1 ms on local
  FS per `health.cross_dc.clusters`), and we're running with concurrency=9
  + conda_envs + repos symlinked to a sibling workdir → double FS traffic.

## Also confirmed NOT a cause of the remaining 31 failures
- 429 rate-limit: 177 events in logs, but dispatch layer's key-rotation
  worked perfectly. The 3/75 failed tasks that hit 429 all completed with
  status=done. Zero "retries exhausted" or stream anomaly flags.
- The 31 remaining failures are **real model/orchestration limitations**
  (patch clean but doesn't solve, partial fix missing edge case, tofu wanders
  for 20-40 turns where CC solves in 3-5).

## Files
- `debug/swebench_runner.py` — the bug and the fix
- `debug/swebench_reeval_checkout_timeouts.py` — recovery script

