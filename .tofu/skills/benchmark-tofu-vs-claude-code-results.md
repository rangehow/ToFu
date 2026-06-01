---
name: benchmark-tofu-vs-claude-code-results
description: SWE-bench runner: official log parsers, fixed Django docstring parsing, conda env fixes, comprehensive logging
enabled: true
tags: [benchmark, claude-code, performance, cost, caching]
created: 2026-04-07T15:03:20Z
updated: 2026-04-09T02:34:02Z
---

# SWE-bench Runner — Architecture & Known Issues

## Key evaluation approach (v3)
- Uses **official SWE-bench log parsers** (`MAP_REPO_TO_PARSER`) to parse test output
- Runs the full test command **once** with all test directives (not per-test-ID)
- Grades with `get_eval_tests_report` + `get_resolution_status` — identical to Docker harness
- Custom `_parse_django_log_fixed()` for Django (fixes multi-line docstring bug)

## Critical fixes applied
1. **Django docstring bug**: Official `parse_log_django` misparses tests with docstrings (output spans 2 lines). Custom parser tracks `prev_test` to correctly attribute status.
2. **Empty test directives**: When test_patch only modifies non-.py files (.txt fixtures), extract directives from F2P+P2P test IDs as fallback.
3. **Conda env Python versions**: Django 3.0-3.2 specs require Python 3.6 (corrupted in conda cache) → use Python 3.9 (within Django 3.x support range).
4. **TEST_TIMEOUT**: Increased to 1800s (30 min) for instances with 100+ test modules.
5. **git clone --shared**: Replaced `--no-local` (120s+) with `--shared` (22s) for eval workspace setup.
6. **Crash resilience**: try/except in pipeline loop, stderr → log file, global exception handler.

## Test environment limitations (vs Docker)
- Some P2P tests fail due to **Pygments/sqlite version differences** (e.g. sphinx linenos format)
- This affects **both tools equally** → fair for comparative benchmarks
- For absolute accuracy, patches should be verified via official Docker harness

## Data persistence
```
workdir/
  swebench_results.json           # Summary with per-instance metrics
  swebench_runner.log             # DEBUG+ structured log
  swebench_stderr.log             # Python stderr capture
  patches/{id}__{tool}.diff       # Model-generated patches
  details/{id}__{tool}.json       # Full inference + eval details with test output
```

