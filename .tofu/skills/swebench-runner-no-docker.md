---
name: swebench-runner-no-docker
description: SWE-bench Verified evaluation: full 500-instance support, conda envs from official specs, no timeout, save-per-instance resume
enabled: true
tags: [benchmark, swe-bench, testing, evaluation]
created: 2026-04-08T01:31:05Z
updated: 2026-04-08T02:42:10Z
---

# SWE-bench Evaluation Without Docker

## Architecture
1. Download dataset from HuggingFace (`princeton-nlp/SWE-bench_Verified`, 500 instances)
2. Clone repos to cache dir, `git clone --local` per instance at `base_commit`
3. Create conda environments per repo+version using official `MAP_REPO_VERSION_TO_SPECS`
4. Let agent (Tofu/CC) read issue + codebase, generate patch
5. Extract patch via `git diff --cached` (excluding `.chatui/`, `__pycache__/`, `.claude/`)
6. Apply model_patch + gold test_patch to fresh checkout
7. Run FAIL_TO_PASS tests using official test commands (runtests.py for Django, bin/test for sympy, pytest for rest)
8. Save results after each instance for robust resume

## Key Design
- **No artificial timeout** — agents run as long as needed. Only 4-hour safety net for truly stuck processes.
- **Conda environments** — `_conda_run()` uses direct PATH manipulation (not `conda run --no-banner` which varies by conda version)
- **Save-per-instance** — JSON results saved after each run, `--resume` skips completed
- **Official test specs** — `MAP_REPO_VERSION_TO_SPECS` from swebench package for Python version, packages, install cmd, test cmd

## Key Gotchas
- **`conda run --no-banner`**: Not available in all conda versions. Use PATH manipulation instead.
- **Trailing newline**: `git apply` needs trailing `\n` — don't `.strip()` the diff
- **Whitespace**: Use `git apply --whitespace=fix` for lenient application
- **Test ID formats**: sympy = bare `test_name`, django = `test_method (module.Class)`, pytest = `path/to/test.py::test_func`
- **pytest must be pip-installed** in each conda env (not always in the specs)
- **.chatui/ & .claude/ skills**: Both tools create artifacts in workspaces — must exclude from diff extraction
- **astropy tests**: Often need C extensions compiled (`pip install -e .[test]`)

## Usage
```bash
# Full 500 instances
python debug/swebench_runner.py --all

# Resume after interruption
python debug/swebench_runner.py --all --resume --output /tmp/swebench_full/swebench_results.json

# Single tool
python debug/swebench_runner.py --all --tool tofu

# Filter
python debug/swebench_runner.py --repo django/django --all
python debug/swebench_runner.py --difficulty "<15 min fix" --all

# Setup conda envs only (pre-warm)
python debug/swebench_runner.py --all --setup-envs-only
```

## Cost Estimate
- ~$250/tool for all 500 (avg ~$0.50/instance with Opus)
- ~33 hours runtime for both tools (avg ~2 min/instance × 500 × 2)

## Script
`debug/swebench_runner.py`

