---
name: swebench-cc-failure-classification
description: How to classify CC SWE-bench failures into env vs. model faults with evidence patterns
enabled: true
tags: [swebench, evaluation, debugging]
created: 2026-04-18T03:15:52Z
updated: 2026-04-18T03:15:52Z
---

# SWE-bench failure classification (CC & Tofu arms)

When analyzing `swebench_workdir/details/*__TOOL.json` to separate real model failures from harness/env issues:

## Fields
- `inference.error` — API/proxy error during inference
- `inference.patch_size` — 0 means model produced no patch
- `eval.patch_applies` / `eval.test_patch_applies` — False = patch rejected by git
- `eval.fail_to_pass`, `eval.pass_to_pass` — dict of {test_name: pass_bool}
- `eval.test_run_output` — list of `{command, return_code, stdout, stderr, duration_s}` (NOT a string! concat stdout+stderr to scan)
- `eval.install_stderr` — pip/conda setup errors

## Environment-issue signals (NOT model's fault)
Scan test_run_output blob for any of:
- `"ModuleNotFoundError: No module named 'py._path'"` — conda env missing `py` lib (pylint-dev arms)
- `"cannot import name 'soft_unicode' from 'markupsafe'"` — markupsafe ≥2.1 incompat with old jinja2/sphinx (sphinx 3.x arms)
- `"ImportError while importing test module"` — test collection import break
- `"Interrupted: 1 error during collection"` — pytest aborted before running
- `"error in conftest.py"` / `"conftest.py' could not be imported"` — fixture infra broken
- `"ERROR: InvocationError"` — tox failure at env level

## Heuristic: "mass p2p regression"
If len(p2p)≥10 and p2p_fail/len(p2p) > 0.3 across ≥2 of 3 tools on the same instance, it's almost always environment (model can't plausibly break half of unrelated tests across all 3 variants).

## Observed on CC run (Apr 17-18, 2026, 371 failures)
| category | cc-glm | cc-minimax | cc-opus | total |
|---|---:|---:|---:|---:|
| environment_issue (known bad signals) | 41 | 40 | 43 | **124** |
| environment_suspect (mass p2p regression) | 4 | 4 | 5 | **13** |
| model_fix_incorrect (f2p fail only) | 45 | 43 | 44 | 132 |
| model_fix_incorrect (both f2p+p2p fail) | 12 | 10 | 11 | 33 |
| model_caused_regression (real) | 17 | 16 | 13 | 46 |
| empty_patch | 4 | 10 | 2 | 16 |
| patch_failed_to_apply | 0 | 2 | 2 | 4 |
| test_patch_failed_to_apply | 3 | 0 | 0 | 3 |

→ **~37% of CC failures are environment/harness, not model fault.** Same pattern affects all three CC variants nearly identically → strong proof it's environmental (50 unique instance_ids).

## Root causes identified
1. **pylint-dev conda envs** (`swe_pylint-dev_pylint_2_14/_2_15`): missing `py` package — modern pytest dropped `py._path`. Fix: `pip install py` in those conda envs.
2. **sphinx-doc 3.0/3.1 conda envs** (`swe_sphinx-doc_sphinx_3_0/_3_1`): markupsafe too new, missing `soft_unicode`. Fix: `pip install markupsafe<2.1` in those envs.

Fixing those two envs alone would likely flip ~100 of 124 "environment_issue" failures.

## Saved artifact
`swebench_workdir/cc_env_suspect.json` — 139 rows of (instance, tool, env_hits, p2p_fail_ratio) for targeted re-eval after fixing envs.

## Workflow for targeted env fix + reeval (when user approves)
1. Activate each affected conda env, pip-install the missing/pinned dep
2. Run a targeted reeval script (like `swebench_reeval_recovered.py`) against just the env_suspect rows
3. Merge results into `swebench_results.json` and re-rebuild details

