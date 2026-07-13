---
name: audit-logging-script
description: debug/audit_logging.py (5-tier) + debug/check_silent_catches.py (fast per-file, reuses test finders) + tests/test_code_quality.py logging-discipline guard; how to clear drift
enabled: true
tags: [logging, audit, tooling, convention]
created: 2026-05-26T22:21:43Z
updated: 2026-07-08T02:28:03Z
---

# Logging Discipline Audit + Regression Test + Fast Checker

Three layers enforce CLAUDE.md §2.2 logging discipline:

1. **`debug/audit_logging.py`** — diagnostic AST scan (manual, ~10s on FUSE; `os.scandir`, 5-tier A1/A2/B1/B2/C).
2. **`tests/test_code_quality.py`** — CI regression guard (pytest, `-m slow`, 7 tests). **Takes ~9-10 min** (walks+parses all lib/+routes/); needs `-p no:napari` in this env (vispy GL ES import fails). Run withOUT a pipe or use `${PIPESTATUS[0]}` — a pipe to tail masks the real exit code.
3. **`debug/check_silent_catches.py`** (NEW 2026-07) — FAST per-file checker. `python3 debug/check_silent_catches.py <files...>` reuses the EXACT finders + ACCEPTABLE allowlists from test_code_quality.py, so a single-file result agrees byte-for-byte with the slow pytest but parses only the named files. Use this to iterate; run the full pytest only for final sign-off.

## The two test finders (what makes a handler "silent")
- `_SilentCatchFinder`: except body is a SINGLE `pass`/`return`/`continue` with no log/raise/api_* call.
- `_AssignSilentCatchFinder`: except body ONLY assigns/pass/return/continue/break, no log.
- BOTH exempt handlers where EVERY caught type is control-flow/optional-dep (`_EXEMPT_EXC_TYPES`: ImportError/StopIteration/CancelledError/TimeoutError/Empty/Full/BlockingIOError/TimeoutExpired/…). A tuple like `(ImportError, ValueError)` is NOT exempt (ValueError is data).
- **A handler clears the check the moment it contains ANY `logger.*`/`log_exception`/`audit_log`/`api_*` call OR a `raise`.** So the fix is: bind `as e`, insert one `logger.debug('[Module] ... : %s', e)` as the first statement, keep the fallback.

## Clearing a big drift (did this 2026-07: 132 sites → 0)
New code accumulates unlogged narrow config/env/JSON/FS-probe catches. To fix at scale:
1. Get authoritative list: `python3 debug/check_silent_catches.py` (no args = whole tree).
2. Fix stale ACCEPTABLE line-drift in test first (line numbers move as files grow: api_response 296/304→340/348 etc). A false-positive that trips BOTH finders (e.g. `_db_safe` `return _handle(e)`, `_trace_fallback`) must be listed in BOTH `TestSilentCatches.ACCEPTABLE` AND `TestAssignmentSilentCatches.ACCEPTABLE`.
3. Parallelize the mechanical source edits with coder sub-agents grouped by subsystem (no file overlap), each given the convention + `check_silent_catches.py <files>` to self-verify.
4. LEVEL: self-recovering fallback → `logger.debug` (warning+ → error.log = noise). sandbox/exec failure, DB write, retry-exhausted → warning/error. See `log-level-discipline-self-recovering-fallbacks`.

## Legitimately-exempt patterns (allowlist, don't "fix")
- `safe_route.wrapper`/`_db_safe.wrapper`: `except: return _handle(e)` — _handle logs via local indirection (invisible to the AST walker).
- `system_context._trace_fallback`: deliberate last-resort silent swallow of pure instrumentation.
- `entry_points().get(...)` `except TypeError:` — Py<3.10 API-shape control-flow, mirrored in EVERY plugin-discovery seam (tools/providers/schema/flags/blueprints/task_runtimes registries).
- `lib/log.py` `_writable_base_dir` OSError probes: log via `logging.getLogger('lib.log').debug(...)` (it IS the logging module, on RAW_LOGGING_ALLOWLIST, runs at import bootstrap).
- `lib/database/pg_admin.py` `print()`s: it's a `python -m` CLI tool — stdout is user-facing output, NOT diagnostics. The 24 prints are legit; audit flags them but the test does not.

## ⚠️ pytest exit code via pipe
`pytest ... | tail` returns tail's exit (0), MASKING failures. Use `${PIPESTATUS[0]}` or no pipe.

## Baseline after 2026-07 cleanup
- Test suite: 7 passed. check_silent_catches: 0 violations across 479 files.
- Audit still shows A2=6/B1=9/B2=8 — ALL are the intentional/allowlisted patterns above (audit tiering ≠ test finders; the test is the source of truth for CI).

