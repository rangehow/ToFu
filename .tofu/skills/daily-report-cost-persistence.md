---
name: daily-report-cost-persistence
description: Daily-report calendar costs: persisted per-day in daily_cost_cache; deletes MUST scope-invalidate via invalidate_cost_cache_for_messages (NOT whole-table wipe)
enabled: true
tags: [performance, daily-report, database, cache, costs]
created: 2026-04-21T03:48:16Z
updated: 2026-07-01T12:43:44Z
---

# Daily-report calendar cost caching (2026-04, invalidation fixed 2026-07)

## Problem
`/api/daily-report/calendar/<y>/<m>` was slow (~1-5s cold) because
`_get_monthly_costs()` scanned the entire `conversations` table and re-ran
`_calc_msg_cost_cny()` per message on every call.

## Fix (caching)
Table `daily_cost_cache(user_id, date, cost, conversations_json, computed_at)`
(now on Core: `lib/database/_core_schema.py::DAILY_COST_CACHE`, composite PK
`(user_id, date)`).
- Past days (date < today): read from cache; on miss scan that day, persist.
- Today: always scan live (don't persist — still being written).
- SQL bounded on BOTH ends (`ms_start <= ts < ms_end`).
- L1 in-memory `_calendar_cache` (TTL 30s); L2 = the DB table (permanent).

## ⚠️ Invalidation — SCOPED, not whole-table (fixed 2026-07)
**Previous bug:** `_delete_message_blocking` + `_delete_conv_blocking` in
`routes/conversations.py` called `invalidate_day_cost_cache()` with NO arg →
`DELETE FROM daily_cost_cache WHERE user_id=?` = wiped ALL persisted days.
Any single delete nuked the whole month → next calendar open live-rescanned
(~10s) → cost "never persisted" from the user's POV. Proven: 30 rows → 0.

**Fix:** `lib/daily_report/cost.py::invalidate_cost_cache_for_messages(messages,
conv_start, conv_end)` computes the exact `'YYYY-MM-DD'` days the removed
messages contributed cost to (`_cost_days_for_messages` — only `usage`-bearing
msgs; timestamp fallback mirrors `_scan_costs_in_range`) and invalidates ONLY
those days. Both delete handlers snapshot the affected messages BEFORE deletion
(delete_message: `_deleted_originals` before `pop`; delete_conv: SELECT
messages+timestamps before the DELETE) and call the scoped helper.
`invalidate_day_cost_cache(None)` (real bulk clear) retained for genuine bulk ops.
Guardrail test: `tests/test_daily_report.py::TestScopedCostInvalidation` asserts
`None not in calls` (delete path must never trigger the whole-table wipe).

## Gotchas
- `conversations_json`: TEXT-JSON (SQLite) / JSONB (PG).
- Cache invalidation must be SCOPED to what changed — a "clear everything" call
  on a hot write path silently defeats a persistent cache (always-cold anti-pattern).

