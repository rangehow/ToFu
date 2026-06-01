---
name: swebench-results-json-zeroing-artifact
description: Don't analyze failure modes from results.json — use details/ files; test-count metadata in results.json got zeroed
enabled: true
tags: [swebench, analysis, gotcha]
created: 2026-04-19T08:32:12Z
updated: 2026-04-19T08:32:12Z
---

## TL;DR
Never categorize SWE-bench failures using `swebench_results.json` `fail_to_pass_total`/`pass_to_pass_total` fields alone. They may have been silently zeroed. ALWAYS cross-check against `swebench_workdir/details/{inst}__{tool}.json` which is ground truth (has `eval.fail_to_pass` and `eval.pass_to_pass` dicts with per-test results).

## Symptom observed 2026-04-19
In `swebench_workdir/swebench_results.json`, 2248 rows showed `fail_to_pass_total=0, pass_to_pass_total=0` after the overnight pipeline restart at Apr 19 11:07. This caused me to falsely classify 102 tofu-opus failures as "tests never ran (env issue)". Actually:
- 1677 of those zeroed rows were **resolved=True** (score preserved, only metadata lost)
- 571 were `resolved=False` legitimate model misses (original f2p/p2p data was in rebuild2 backup)

The actual failure breakdown (from `swebench_results.before_rebuild2.json` + current) is:
- `f2p_fail_only` (genuine model miss): tofu 45-67 vs cc 53-58 — tofu slightly worse
- `minor_p2p_regression` (model introduced small bugs): tofu 22-27 vs cc 17-20 — tofu slightly worse
- Tofu's lower scores vs CC are **real model failures, not env/harness issues**

## Root cause (still unconfirmed, but likely)
When the pipeline starts with `--resume`, `_load_completed` loads rows from results.json into BenchmarkResult dataclass. If any previous run ever wrote `fail_to_pass_total=0` (e.g., from an instance where eval failed), that zero propagates forward in subsequent saves because `_save_results` just serializes `all_results` as-is.

BUT rebuild2 (Apr 19 11:06) was correct. After pipeline started at 11:07, _load_completed reads rebuild2's good data... and next save zeros things out. The pipeline must be replacing rows after loading somewhere, OR loading wrong source, OR... investigate more.

## How to analyze correctly
```python
# CORRECT: use details/ ground truth
from pathlib import Path
import json
for p in Path('swebench_workdir/details').glob('*.json'):
    d = json.loads(p.read_text())
    ev = d.get('eval') or {}
    f2p = ev.get('fail_to_pass') or {}  # dict of {test_id: bool}
    p2p = ev.get('pass_to_pass') or {}
    f2p_pass = sum(1 for v in f2p.values() if v); f2p_total = len(f2p)
    # categorize from here
```

## Backup chronology (project-specific)
- `swebench_results.before_rebuild.json` (Apr 18 10:58) — smart-resume stripped state
- `swebench_results.before_env_fix.20260418_1131.json`
- `swebench_results.before_env_reeval.json` (Apr 18 12:05)
- `swebench_results.before_tofu_recovery.json` (Apr 18 22:52)
- `swebench_results.before_rebuild2.json` (Apr 19 11:06) — **HAS CORRECT F2P/P2P DATA**
- `swebench_results.json` (current, Apr 19 16:27) — metadata zeroed for 2248 rows

## Pipeline investigation todo
Find where BenchmarkResult.fail_to_pass_total gets reset to 0 after loading. Possibly in `_load_completed` truncation, in `_save_results` serialization, in how `all_results` interacts with concurrent updates, or in a subtle dataclass default_factory interaction.

