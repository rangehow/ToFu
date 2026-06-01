---
name: audit-logging-script
description: debug/audit_logging.py — AST tiered audit for silent catches / print leaks / f-string logger / missing get_logger
enabled: true
tags: [logging, audit, tooling, convention]
created: 2026-05-26T22:21:43Z
updated: 2026-05-26T22:37:25Z
---

# Logging Discipline Audit Script

`debug/audit_logging.py` runs four AST-based checks across `lib/` + `routes/`:

1. **Silent except blocks** (no `logger.*` / `log_exception` / `audit_log` / `api_*` helper / `raise` inside)
2. `print()` calls
3. f-strings passed as message arg to `logger.*` (CLAUDE.md §2.6 forbids — lazy formatting only)
4. Modules with funcs/classes but no `get_logger(__name__)`

## Tiered output (silent catches)

- **Tier A** = `except Exception` / `except BaseException` / bare `except:` — real §2.2 bugs.
- **Tier B** = narrow data-coercion catches (`ValueError`, `TypeError`, `json.JSONDecodeError`, `OSError`, `KeyError`, `IndexError`, `AttributeError`, `LookupError`, `FileNotFoundError`, `PermissionError`, `ProcessLookupError`, `UnicodeDecodeError`) — should still get `logger.debug` per §2.2 example C.
- **Tier C** = optional-dep / control-flow catches (`ImportError`, `ModuleNotFoundError`, `NameError`, `StopIteration`, `KeyboardInterrupt`, `CancelledError`, `asyncio.TimeoutError`, `queue.Empty/Full`, `BlockingIOError`, `subprocess.TimeoutExpired`) — legitimate.

## "Logging happened" callees recognized

`debug, info, warning, warn, error, critical, exception, log_exception, audit_log, log_context, logger, get_logger`. Plus the api_response error helpers, which communicate failure outward to the client (not silent): `api_internal_error` (also auto-logs at ERROR per §4.6.2), `api_error`, `api_bad_request`, `api_not_found`, `api_unauthorized`, `api_forbidden`, `api_conflict`, `api_payload_too_large`, `api_method_not_allowed`. Plus any `raise` (caller logs).

## Skipped

- `tests/` subdirs (CLI test harnesses use `print()` and broad-catch by design).

## Run

```bash
python3 -u debug/audit_logging.py > /tmp/audit_report.txt
```

~10 s on FUSE mount. Uses `os.scandir` (NOT `Path.rglob` — that hangs on FUSE).

## Baseline

After Batch 3 (full Tier A clean):
- Tier A: **0** in 0 files (was 81 at start of effort)
- Tier B: 143 in 68 files (low-priority debug-log candidates)
- Tier C: 27 in 19 files (legitimate)
- print() leaks: 0
- f-string logger calls: 0
- Missing get_logger: 33 files (~all pure-data modules — false positives)

## Batch summaries (chronological)

- **Batch 1** (silent broad catches in long-running paths, 24 fixes):
  `lib/llm/stream.py`, `lib/llm/astream.py`, `lib/database/_core.py`,
  `lib/tasks_pkg/manager.py`, `lib/llm_dispatch/api.py`, `lib/push.py`,
  `lib/task_runtime.py`, `lib/http_client.py`, `lib/message_queue.py`,
  `lib/api_response.py`, `lib/idempotency.py`, `lib/tasks_pkg/event_log.py`.
- **Batch 2** (route-handler `except Exception: api_bad_request(str(e))` patterns, 10 sites):
  `routes/api_v1/{logs,agents,conversations,webhooks}.py`. Narrowed to `BadRequest` from `lib.request_parser`; `from routes.X import Y` failures narrowed to `ImportError` and the exception is now passed positionally to `api_internal_error` so it auto-logs traceback.
- **Batch 3** (helper modules, 47 sites across 27 files):
  `lib/tasks_pkg/tool_display.py`, `lib/compat/_platform.py`, `lib/mcp/client.py`,
  `routes/paper.py`, `routes/compat_openai.py`, `lib/llm_dispatch/dispatcher.py`,
  `lib/tasks_pkg/streaming_tool_executor.py`, `routes/chat.py`, `routes/metrics.py`,
  `routes/translate.py`, `lib/compat/openai.py`, `lib/database/_bootstrap.py`,
  `lib/database/_schema_{pg,sqlite}.py`, `lib/doc_parser.py`,
  `lib/file_history/api.py`, `lib/memory/prefetch.py`, `lib/pdf_parser/docling.py`,
  `lib/pptx_translator.py`, `lib/project_mod/tools.py`, `lib/search/vertical.py`,
  `lib/tasks_pkg/compaction/_reactive.py`, `routes/api_v1/{auth,capabilities}.py`,
  `routes/{artifacts,common,oauth}.py`.

## Log-level rule (per memory `log-level-discipline-self-recovering-fallbacks`)

- **debug**: system recovers, fallback works (close-on-shutdown, push fan-out, optional dep, body-read sync fallback, urlparse on display-only path)
- **warning + exc_info**: needs attention (e.g. `_ensure_table` failing means the queue is unusable; outbound HTTP failure)
- **error + exc_info**: blocks pipeline / data loss
- **`api_internal_error(exc, ...)`** (positional exc, NOT f-string): auto-logs at error+exc_info per §4.6.2

## Common narrow-catch types per pattern

- `urlparse(url)` → `ValueError`
- `from lib.X import Y` (optional dep) → `ImportError` (NOT `Exception`)
- `os.path.abspath(p)` → `(OSError, ValueError)`
- `int(s)` / `data.decode(errors='replace')` → already infallible; if catch is needed pick `(ValueError, LookupError, AttributeError)`
- Flask `g.x` outside context → `RuntimeError`
- Flask `get_db()` outside Flask → `RuntimeError`
- `requests.get(...)` → `requests.RequestException`
- subprocess / Win32 ctypes → broad `Exception` is justified at OS-syscall boundaries; just add a debug log
- xlrd date convert → `(ValueError, TypeError, OverflowError)`

