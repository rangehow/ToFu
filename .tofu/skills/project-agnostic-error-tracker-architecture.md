---
name: project-agnostic-error-tracker-architecture
description: Unified error tracker architecture: single lib/project_error_tracker.py handles ALL projects (including self-diagnostics) with auto-discovery, multi-format parsing, portable .chatui/error_resolutions.json storage, enrichment, and CSV/Markdown/JSON export — lib/error_tracker.py deleted, no DB dependency
enabled: true
tags: [python, error-tracking, logging, project-agnostic, unified, architecture, portable]
created: 2026-03-28T05:08:20Z
updated: 2026-03-28T15:32:46Z
---

# Unified Error Tracker Architecture

## Single Module: `lib/project_error_tracker.py`

**There is ONE error tracking module** — `lib/project_error_tracker.py`.  It handles:
- Any external project the user opens via the project tool
- The application itself (self-diagnostics) — same code path, app root passed as project_path

**Deleted**: `lib/error_tracker.py` (was a chatui-specific duplicate with PostgreSQL storage).

## Key Design Decisions

1. **No app-specific fallback** — When `project_path` is None, the executor computes the app's own root:
   ```python
   # In executor.py (at lib/tasks_pkg/executor.py — 3 dirname levels)
   if not project_path:
       project_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
   ```

2. **File-based resolution storage** — `.chatui/error_resolutions.json` per project (no PostgreSQL dependency). Portable, committable.

3. **Multi-format parser** — Python logging, Java/logback, Node.js/Winston/Pino (JSON lines), Go slog, Rails, syslog, generic timestamp-level.

## Public API (all take `project_path: str` as first arg)

| Function | Purpose |
|---|---|
| `discover_log_files(path)` | Find log files (3-tier: known dirs → known names → recursive walk) |
| `parse_log_line(line)` | Parse one log line from any format |
| `compute_fingerprint(logger, msg)` | Stable 8-char hex hash (strips volatile parts) |
| `scan_project_errors(path, n)` | Full scan → list of error dicts |
| `get_unresolved_grouped(path, n)` | Group by fingerprint, exclude resolved |
| `error_stats(path, n)` | Summary statistics |
| `enrich_errors(path, errors)` | Add fingerprint + resolved status in-place |
| `get_resolutions_list(path)` | All resolutions as sorted list |
| `daily_digest(path, hours)` | Time-windowed summary |
| `export_for_pm(path, n, format)` | Export as JSON/Markdown/CSV |
| `mark_resolved(path, fp, ...)` | Mark fingerprint fixed |
| `mark_unresolved(path, fp)` | Re-open a resolved error |
| `resolve_by_logger(path, prefix)` | Bulk resolve by logger prefix |
| `resolve_by_message_pattern(path, regex)` | Bulk resolve by message regex |
| `get_log_summary(path)` | Human-readable log file listing |

## File Map

| File | Role |
|---|---|
| `lib/project_error_tracker.py` | **The single universal module** — scanning, parsing, fingerprinting, resolution storage, enrichment, export |
| `lib/tools/error_tracker.py` | Tool schema definitions (CHECK_ERROR_LOGS_TOOL, RESOLVE_ERROR_TOOL) — unchanged |
| `lib/tasks_pkg/executor.py` | Dispatch: `_exec_check_error_logs` and `_exec_resolve_error` — unified, no if/else branching |
| `routes/common.py` | REST endpoints (`/api/errors/*`) — all use `_app_root()` + project_error_tracker |
| `lib/log.py` | Logging setup ONLY — no analysis utilities (removed `get_recent_errors`, `daily_error_digest`, `_scan_file_for_errors`, `_parse_log_line`) |

## Critical: dirname Count for Fallback

`executor.py` is at `lib/tasks_pkg/executor.py` → needs **3** dirname calls to reach project root.
`routes/common.py` is at `routes/common.py` → needs **2** dirname calls.

