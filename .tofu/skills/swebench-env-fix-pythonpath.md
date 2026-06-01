---
name: swebench-env-fix-pythonpath
description: SWE-bench runner: PYTHONPATH isolation fixes shared conda env corruption
enabled: true
tags: [swebench, testing, environment, conda]
created: 2026-04-14T08:09:08Z
updated: 2026-04-14T08:09:08Z
---

## SWE-bench Runner Environment Isolation

### Problem
Shared conda envs get corrupted when multiple eval instances of the same repo run in parallel.
`pip install -e .` writes `.egg-link` to the shared env, pointing to whichever workspace ran last.
This caused ~195 false P2P regressions (39% of instances).

### Solution: PYTHONPATH + pip install -e .
1. **ALWAYS set PYTHONPATH** to the eval workspace before running tests — this ensures the correct
   source code is imported regardless of stale editable installs
2. **Still run `pip install -e .`** to install dependencies (pytz for Django, etc.) — but the
   PYTHONPATH override means the .egg-link corruption doesn't affect which source code is used
3. **Do NOT use venvs** — they introduce subtle version mismatches (e.g. xarray datetime64 tests
   fail in venv but pass without venv, due to setuptools differences)

### Key issues found and fixed:
- **xarray**: environment.yml deps (cftime, bottleneck, sparse) weren't being installed
- **requests**: httpbin.org is down (returns 503), tests that call it always fail — unfixable without local httpbin
- **Django**: needs `pip install -e .` for dependencies like pytz — can't skip the install
- **Django 10097**: 2/1386 P2P regressions are SQLite version issue — unfixable without Docker

### Smart resume
`--smart-resume` flag: keeps resolved results, re-runs all non-resolved instances.

