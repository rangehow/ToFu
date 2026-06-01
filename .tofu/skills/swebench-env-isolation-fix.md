---
name: swebench-env-isolation-fix
description: SWE-bench conda env isolation bug: shared envs + editable installs cause race conditions, fixed with PYTHONPATH
enabled: true
tags: [swebench, benchmark, conda, environment, debugging]
created: 2026-04-14T03:10:34Z
updated: 2026-04-14T03:10:34Z
---

# SWE-bench Runner: Conda Environment Isolation Bug

## The Problem
When running SWE-bench evals with shared conda envs (one per repo+version), parallel evaluation
instances corrupt each other's package installs:

1. **Editable installs (`pip install -e .`)** create `.egg-link` or `__editable__*.pth` files in
   the shared env's site-packages that point to a specific eval workspace directory
2. When multiple instances of the same repo+version run in parallel (or sequentially), each one
   overwrites the previous install link → the last writer wins
3. Tests then import code from the WRONG workspace (different commit), causing spurious P2P failures

## Root Cause Evidence
- `Django.egg-link` in shared env pointed to `eval/django__django-11551__tofu-opus`
- ALL other Django instances of the same version were importing from that workspace
- 204 instances had identical P2P regression counts across ALL 6 tools → proving environment issue

## The Fix (implemented)
1. **PYTHONPATH injection**: Before running tests, set `PYTHONPATH=<eval_workspace>` so the correct
   workspace code is imported first, regardless of what the shared env's egg-link says
2. **Source layout detection**: Handle `src/` layout repos (pytest, flask) by setting 
   `PYTHONPATH=<workspace>/src`
3. **Force editable install**: Override `setup.py install` (egg-based) with `pip install -e .`
4. **No test output truncation before parsing**: Keep full stdout for log parser, only truncate on save
5. **Cleaned shared envs**: Removed all stale `.egg-link`, `__editable__*.pth`, and `.egg` files

## Additional Issues Found
- Test output truncation (`[-50000:]`) happened BEFORE the log parser ran, causing parser to see
  no test results for large test suites (Django: 3391 tests × 80 chars = 270K chars)
- `--reeval` mode was filtering by backend name (`cc`) instead of model name (`cc-opus`)
- Sequential reeval of 1193 patches took ~50 hours; parallelized with ThreadPoolExecutor (8 workers)

## Files Changed
- `debug/swebench_runner.py`: `evaluate_patch()`, `_install_in_conda()`, reeval mode

