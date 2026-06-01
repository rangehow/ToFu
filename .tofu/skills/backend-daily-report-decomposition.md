---
name: backend-daily-report-decomposition
description: routes/daily_report.py decomposed: thin route layer (705 LOC) + lib/daily_report/ package (9 modules)
enabled: true
tags: [refactor, backend, routes, daily_report, convention]
created: 2026-05-28T06:10:47Z
updated: 2026-05-28T06:10:47Z
---

# `routes/daily_report.py` Decomposition (2026-05-28)

Second backend hot-file decomposition, applying the pattern locked in by
`backend-translate-decomposition`. Same approach, same structure.

## Before

Single 2405-LOC `routes/daily_report.py` with:
- 13 route handlers (Blueprint + endpoints)
- Per-message + per-day + per-month cost calculation
- LLM analysis system prompt (~80-line work-journal prompt)
- 269-line `_analyse_conversations` orchestrator
- TODO carryover + fuzzy match + cross-day done/close logic
- Conversation extraction from DB (DB-bound day filter)
- Async background generator (extract → analyse → save staging)
- Daemon scheduler (auto-backfill yesterday at boot + every 6 h)
- Active-job state (`_active_jobs`, `_jobs_lock`)
- Calendar TTL cache (`_calendar_cache`)

3 callers: `routes/conversations.py` (×2 — `invalidate_day_cost_cache`)
and `debug/test_daily_cost_cache.py`.

## After

```
routes/daily_report.py        705 LOC — Blueprint + 13 route handlers
                                       + back-compat re-exports (44 symbols)
lib/daily_report/
  __init__.py                 113 LOC — package facade
  storage.py                  118 LOC — DEFAULT_USER_ID, _REPORTS_DIR,
                                       _save/_load_report, _active_jobs,
                                       _update/_get/_clear_job
  prompts.py                  124 LOC — _ANALYSIS_SYSTEM, _TODO_TOOL_*, _QUOTES
  cost.py                     475 LOC — _qwen_cny, _calc_msg_cost_cny,
                                       _scan_costs_in_range,
                                       _load_cached_day_costs,
                                       _persist_day_cost,
                                       invalidate_day_cost_cache,
                                       _get_monthly_costs, _calendar_cache
  todos.py                    309 LOC — _normalize/_fuzzy/_carryover/
                                       _today_inherited/_accountability/
                                       _mark_done/_close_remaining
  conversations.py            522 LOC — _safe_int_ts, _build_transcript,
                                       _extract_convs_for_date,
                                       _count_convs_for_date,
                                       _analyse_conversations
  llm.py                      180 LOC — _extract_json_result,
                                       _run_llm_analysis, _pick_persona
  generator.py                103 LOC — _generate_in_background
  scheduler.py                 76 LOC — _backfill_yesterday_if_missing,
                                       _scheduler_loop, start_report_scheduler
```

Total LOC: 2725 (vs 2405 — increase from per-module docstrings + facade).

## Shared-state subtleties (worth noting)

Three pieces of mutable module-level state are referenced from multiple
places. Re-exports preserve identity (verified at import time):

- ``_calendar_cache`` (TTL cache) — referenced from cost.py (owner),
  storage.py (`_save_report` invalidates on save), and routes/daily_report.py
  (`get_calendar_month` reads/writes). All three see the same dict object.
- ``_active_jobs`` + ``_jobs_lock`` — owner storage.py, also accessed from
  routes/daily_report.py for the `start_generation` / `get_generation_status`
  handlers. Same dict / same lock through re-export.
- ``DEFAULT_USER_ID = 1`` — defined in storage.py (avoids the
  routes.common → lib import that would create a cycle). Re-exported
  through the facade.

`storage._save_report` does ``from .cost import _calendar_cache`` lazily
inside the function body — at module-load time, cost.py would try to
import storage.py back, creating a cycle. Lazy import breaks it cleanly.

## Back-compat strategy

`routes/daily_report.py` re-exports 44 legacy symbols:
- 9 storage names (`_REPORTS_DIR`, `_active_jobs`, `_save_report`, …)
- 4 prompt constants
- 11 cost names (incl. `invalidate_day_cost_cache` used by routes.conversations)
- 7 todos names
- 5 conversation names
- 3 LLM names
- 1 generator + 3 scheduler names + Blueprint + DEFAULT_USER_ID

External callers (`routes/conversations.py`, `debug/test_daily_cost_cache.py`)
need no changes — `from routes.daily_report import invalidate_day_cost_cache`
continues to work.

## Verification

- All 10 standalone migration tests pass (translate suite, run for sanity).
- 95/96 pytest pass (1 pre-existing flake on `test_api_bad_request` test-ordering).
- `tests/test_frontend_api_isolation.py` 4/4 pass.
- All 13 routes registered in dummy Quart app verified.
- Cross-module imports: `routes.conversations` and `debug/test_daily_cost_cache.py`
  load cleanly.
- Shared-state identity verified: `cost._calendar_cache is
  routes.daily_report._calendar_cache is lib.daily_report._calendar_cache`.

## Pattern divergences from translate

- **No TaskRuntime needed** — daily_report has its own `_active_jobs` /
  `_jobs_lock` registry, simpler than the TaskRuntime model. Kept it.
- **No Commit/CAS layer** — daily_report writes JSON files (one file per
  date), not the conversations DB row. The race-safety story is per-day
  file-locking via the OS, plus `_calendar_cache.pop` invalidation.
- **Scheduler module is its own file** — translate didn't have one.
- **Lazy import inside `_save_report`** — needed to break a storage→cost
  circular import that didn't appear in translate. Comment in the code
  explains the rationale.

## Pattern still to apply

`routes/paper.py` (3089 LOC) is next. Expected modules:
`storage.py` (paper library DB), `prompts.py`, `engine.py` (report
engine), `runtime.py` (TaskRuntime), `arxiv.py`, `chat.py` (paper-QA
streaming).

