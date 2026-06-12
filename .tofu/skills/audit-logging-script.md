---
name: audit-logging-script
description: debug/audit_logging.py (5-tier A1/A2/B1/B2/C + per-finding context) + tests/test_code_quality.py (cached AST, detailed failures) logging audit
enabled: true
tags: [logging, audit, tooling, convention]
created: 2026-05-26T22:21:43Z
updated: 2026-06-07T09:28:04Z
---

# Logging Discipline Audit Script + Regression Test

Two layers enforce CLAUDE.md §2.2 logging discipline:

1. **`debug/audit_logging.py`** — diagnostic AST scan (manual, ~10s on FUSE; uses `os.scandir`, NOT `Path.rglob`).
2. **`tests/test_code_quality.py`** — CI regression guard (pytest, 7 tests).

## debug/audit_logging.py — four checks across lib/ + routes/

1. **Silent except blocks** (no `logger.*`/`log_exception`/`audit_log`/`api_*` helper/`raise` inside)
2. `print()` calls
3. f-strings as `logger.*` message arg
4. Modules with funcs/classes but no `get_logger(__name__)`

### 5-level silent-catch tiering (severity = exception breadth × body action)
- **A1** — bare `except:` / `except BaseException` (also swallows SystemExit/KeyboardInterrupt). CRITICAL.
- **A2** — broad `except Exception`. HIGH.
- **B1** — narrow data-coercion catch that DISCARDS (pure `pass`) OR runs unlogged non-trivial logic (`action=logic`). MEDIUM — review.
- **B2** — narrow catch with clean unlogged fallback (assign/return/continue/break). LOW — add `logger.debug`.
- **C** — optional-dep/control-flow exc types only (ImportError, queue.Empty, CancelledError, TimeoutExpired, …). LEGITIMATE.

Body action classified by `_classify_body_action`: pass / return / assign / continue / break / logic.
Tier from `_classify_tier(exc_names, action)`.

### Per-finding context (greater detail)
Each finding reports: line, **enclosing function qualname** (via `_ContextVisitor` func-stack), **in-loop** flag (loop-depth), **unbound** flag (no `as e`), **body action**, exc spec, preview. Summary shows per-tier action breakdown `[logic=8, pass=7, ...]`.

### CLI flags
`--limit N` (default 12 per file per tier), `--full` (no truncation), `--tiers A1,A2,B1` (filter detail; summary always all). Unknown tier → exit 2.

## tests/test_code_quality.py — 4 test classes, 7 tests
- **TestSilentCatches** — single `pass`/`return`/`continue` body, no log. Exempts pure control-flow/optional-dep via `_all_exc_exempt` (mirrors TIER C). `ACCEPTABLE` allowlist (file,line) incl. api_response.py:296/304 (`except Exception: return _handle(e)` — _handle→api_internal_error, invisible through local indirection).
- **TestAssignmentSilentCatches** — body ONLY assigns fallback (`body=''`)/pass/return/continue/break, no log. Same `_all_exc_exempt` exemption. Catches the class TestSilentCatches misses. `ACCEPTABLE`: api_response 296/304 + provider_registry.py:171 + tools/registry.py:594 (`entry_points().get()` Py<3.10 control-flow; TypeError NOT exempt so needs allowlist).
- **TestNoFStringInLoggerCalls** / **TestLoggerStandardization** — f-string + raw `logging.getLogger` (RAW_LOGGING_ALLOWLIST).

Shared helpers (module-level): `_handler_has_log_or_raise`, `_exc_type_str`, `_body_action`, `_ContextMixin` (func/loop tracking + `_record`), `_all_exc_exempt`, `_render_violations` (grouped-by-action detailed fail msg), `_EXEMPT_EXC_TYPES`.

### PERF — `_parsed_trees(directory)` lru_cache (CRITICAL on FUSE)
Walk+read+parse each dir ONCE, shared across all tests. WITHOUT it, 7 tests each re-walk+re-parse lib/+routes/ → suite exceeds 600s and times out. WITH it: well under budget.

## ⚠️ pytest exit code via pipe
`pytest ... | tail` returns **tail's** exit (0), MASKING failures. Use `${PIPESTATUS[0]}`/`${PIPESTATUS[1]}` or run without a pipe to see the real verdict. (Bit me: a 2-failure run looked green.)

## Current baseline (2026-06, granular sweep)
- A1: 0, A2: 2 (api_response `_handle`, false-pos), B1: 0, B2: 2 (`entry_points().get()` control-flow), C: 28
- print(): 0, f-string logger: 0, parse errors: 0
- Test suite: 7 passed

## Log-level rule (per `log-level-discipline-self-recovering-fallbacks`)
- debug: recovers/fallback works · warning+exc_info: needs attention · error+exc_info: blocks pipeline/data loss · `api_internal_error(exc,...)` positional → auto error+exc_info

## Common narrow-catch types per pattern
- `urlparse`/`ipaddress.ip_address` → ValueError · optional dep `from lib.X import Y` → ImportError · `int/float(env)` → (ValueError,TypeError) · `os.listdir/getsize/remove` → OSError · `requests.get` → requests.RequestException

