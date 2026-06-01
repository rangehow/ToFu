---
name: per-key-daily-success-rate-auto-disable
description: Per-day per-key success/failure tracking with auto-disable below 50% success rate + manual override
enabled: true
tags: [feature, llm-dispatch, settings, key-stats, auto-disable]
created: 2026-04-18T06:57:41Z
updated: 2026-05-05T03:03:11Z
---

---
name: per-key-daily-success-rate-auto-disable
description: Per-day per-key success/failure tracking with auto-disable below 50% success rate + PERSISTENT manual override
enabled: true
tags: [feature, llm-dispatch, settings, key-stats, auto-disable, persistence]
created: 2026-04-18T06:57:41Z
updated: 2026-05-05T00:00:00Z
---

# Per-key Daily Success-rate Tracking & Auto-disable

## Goal
Show today's success rate for each API key in Settings > Providers; auto-disable a key when:
- It returns `MAX_CONSECUTIVE_429` (=100) 429s in a row with **no success** (streak heuristic)
- OR its success rate < 50% after ≥ 5 non-429 attempts
- OR the error body explicitly says `insufficient_quota` / `insufficient balance` / HTTP 402

Users can manually toggle any key on/off; override wins over auto-logic.

## Persistence model (IMPORTANT)

`data/config/key_stats.json` stores two kinds of state with DIFFERENT lifetimes:

- **`stats`** (per-key `success/failure/rate_limited/consecutive_429/exhausted/last_error`):
  reset daily at calendar-day rollover. These are "today's" counters.
- **`overrides`** (`{pair_key: bool}`): **PERSIST across day rollovers AND process restarts**.
  A key the user manually disabled stays disabled until they explicitly toggle it back
  to "auto" (calling `clear_key_override`).

Both `_load_unlocked()` and `_ensure_fresh_unlocked()` must preserve
`_cache['overrides']` when advancing `day`. Only `stats` is wiped.

Regression test: `debug/test_key_stats_override_persistence.py` covers:
1. In-memory day rollover preserves overrides, resets stats.
2. Process restart on a new day preserves overrides (disk → cache path).
3. `clear_key_override` removes from cache AND disk.
4. `exhausted` flag resets on rollover while override survives.

## Why streak counter (NOT text detection)
Provider 429 bodies are ambiguous (same text for "RPM overrun" vs "balance out"),
so we rely on a streak: 100 consecutive 429s with no success → exhausted for today.
Any success or non-429 error resets `consecutive_429` to 0.

## Files
- `lib/key_stats.py`
  - Entry fields: `success, failure, rate_limited, consecutive_429, last_error, exhausted`
  - Public: `record_outcome`, `record_rate_limit` (streak+auto-exhaust), `mark_key_exhausted`,
    `is_key_enabled`, `get_today_stats`, `get_all_stats`, `set_key_override`, `clear_key_override`
  - Constants: `MIN_ATTEMPTS=5`, `MIN_SUCCESS_RATE=0.5`, `MAX_CONSECUTIVE_429=100`
  - `set_key_override(enabled=True)` also clears `exhausted` and `consecutive_429`
- `lib/llm_client.py` — `RateLimitError(is_quota, reason)`
- `lib/llm_dispatch/slot.py` — `record_error(is_rate_limit, error, is_quota_exhausted)`
- `lib/llm_dispatch/dispatcher.py::_pick` — filters via `is_key_enabled(provider_id, key_name)`
- `lib/llm_dispatch/api.py` — after every 429, check `is_key_enabled`; if streak tripped, add to `exclude_keys`
- `routes/common.py` — `GET /api/dispatch/key-stats`, `POST /api/dispatch/key-override`
- `static/js/settings.js` — `_renderKeyStatsBlock` + `_onKeyToggle`

## Override precedence (in `is_key_enabled`)
1. Manual override → wins (user re-enable also resets streak + exhausted)
2. `exhausted=True` → disabled
3. Auto-disable (attempts ≥ 5 AND sr < 50%) → disabled
4. Last-resort guard (`_pick_last_resort_unlocked`) — keep one key alive if ALL siblings raw-disabled
5. Default: enabled

## Export sanitization
`data/config/` is in `ALWAYS_EXCLUDE_DIRS`, so `key_stats.json` never leaks.

