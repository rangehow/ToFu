---
name: daily-report-cost-persistence
description: Daily-report calendar costs: persisted per-day aggregate in daily_cost_cache table (past days cached forever, today live-computed)
enabled: true
tags: [performance, daily-report, database, cache, costs]
created: 2026-04-21T03:48:16Z
updated: 2026-04-21T03:48:16Z
---

# Daily-report calendar cost caching (2026-04)

## Problem
`/api/daily-report/calendar/<y>/<m>` was slow (~1-5s cold) because
`_get_monthly_costs()` scanned the entire `conversations` table, parsed
every `messages` JSON blob, and re-ran `_calc_msg_cost_cny()` per message
on every call. Only a 30s in-process TTL cache.

## Fix
New table `daily_cost_cache(user_id, date, cost, conversations_json, computed_at)` — schema version bumped (SQLite 11→12, PG 12→13). PK in `lib/database/_sql_translate.py` `_get_pk_columns()`.

**Read path** (`routes/daily_report.py::_get_monthly_costs`):
- Past days (date < today): read from `daily_cost_cache`. On miss, scan that single day's range, persist, return.
- Today: always scan today's range live (don't persist — conversations still being written).
- SQL filter bounded on BOTH ends (`ms_start <= ... < ms_end`) — previously only lower bound → scanned whole future history.

**Invalidation hooks**:
- `routes/conversations.py::delete_conv` — calls `invalidate_day_cost_cache()` (clears all, since we don't know which dates were affected).
- `routes/conversations.py::delete_message` — same bulk clear.
- Normal message edits DON'T change `usage` (set once when LLM responds), so no hook needed.

**Functions exposed**:
- `_scan_costs_in_range(ms_start, ms_end, year=None, month=None)` — pure scan helper.
- `_load_cached_day_costs(year, month)` — bulk read for a month.
- `_persist_day_cost(date_str, day_data)` — INSERT OR REPLACE single day.
- `invalidate_day_cost_cache(date_str=None)` — public invalidator (None = clear all + `_calendar_cache.clear()`).

## Gotchas
- `conversations_json` stores the per-conv breakdown (for sidebar drill-down). In SQLite it's TEXT-JSON; in PG it's JSONB.
- `INSERT OR REPLACE` works on PG via the translator because `daily_cost_cache` is in `_PK_MAP`.
- The in-memory `_calendar_cache` (TTL=30s) is kept as L1; DB table is L2 permanent.

