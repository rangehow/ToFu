---
name: swebench-env-fix-recipe
description: Env fixes for pylint/sphinx conda envs that caused false-negative SWE-bench failures
enabled: true
tags: [swebench, conda-envs, false-negatives]
created: 2026-04-18T04:10:45Z
updated: 2026-04-18T04:10:45Z
---

# SWE-bench conda env hotfixes (Apr 18 2026)

The stock `swebench_workdir/conda_envs/` installations have several broken package states
that cause ALL patches to fail with identical collection errors across all 3 model arms —
smoking-gun evidence of env bugs (not model bugs).

## Fixes applied

### pylint-dev envs (2_9, 2_10, 2_14, 2_15, 3_0)
1. `pip install 'py>=1.11.0'` — the stock env has a single-file `py.py` stub that
   shadows and lacks `py._path` submodule. pytest needs `py.path.local`.
2. **Must delete** `<env>/lib/python3.9/site-packages/py.py` after installing — the stub
   file shadows the `py` package directory.
3. `pip install toml` — missing but required by pylint.config.option_manager_mixin

### sphinx-doc envs (3_0, 3_1, ..., 5_2)
1. `pip install 'markupsafe<2.1'` — `markupsafe>=2.1` removed `soft_unicode`; old sphinx
   test fixtures at `sphinx/testing/fixtures.py:21` still import it.
2. `pip install roman` — sphinx.writers.latex imports `from roman import toRoman`

## Internal pip mirror
Envs are preconfigured with `pip.sankuai.com` mirror — all installs work offline.

## Verification snippet
```
for env in swe_pylint-dev_pylint_2_14 swe_sphinx-doc_sphinx_3_0; do
  PY="swebench_workdir/conda_envs/$env/bin/python"
  $PY -c "import py._path; from py.path import local" 2>&1 | tail -1
  $PY -c "from markupsafe import soft_unicode" 2>&1 | tail -1
done
```

## Impact
After applying these fixes and reevaluating 139 env-suspect CC patches:
- **73 of 139 flipped from ❌ to ✅** (zero lost)
- cc-glm +23, cc-minimax +24, cc-opus +26
- New totals: cc-glm 309/412 (75.0%), cc-minimax 311/412 (75.5%), cc-opus 318/412 (77.2%)
- All prior CC resolved rows unchanged (no regressions)

## Scripts
- `debug/swebench_reeval_env_suspect.py` — targeted reeval using stored patches
- `swebench_workdir/cc_env_suspect.json` — 139 curated rows for env-fault reeval
- `swebench_workdir/swebench_results.before_env_reeval.json` — backup

