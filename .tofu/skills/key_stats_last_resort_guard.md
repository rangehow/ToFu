---
name: key_stats_last_resort_guard
description: lib/key_stats.py last-resort guard: auto-disable never zeros a provider
enabled: true
tags: [key_stats, dispatcher, auto_disable]
created: 2026-04-21T15:15:47Z
updated: 2026-04-22T11:17:20Z
---

# key_stats.py last-resort guard

`is_key_enabled(provider_id, key_name)` applies a **read-time** last-resort
guard: if raw-check (manual override > exhausted flag > success-rate) says
disable but EVERY sibling pair-key under the same `provider_id` is also
raw-disabled, promote **exactly ONE** of them (the healthiest, per
`_rank_for_last_resort_unlocked`) and return True only for that pk.
All other siblings stay disabled. Exception: explicit `override=False`
from `set_key_override` still force-disables and removes that key from
last-resort eligibility (users retain control).

**Ranking tuple** (higher = healthier): `(not exhausted, success_rate,
success_count, -consecutive_429, -failure_count)`, then the `siblings`
index (later wins) as final tie-break — matches the user intuition "keep
the LAST key" when all rankers tie.

Key points for future edits:
- Siblings lookup (`_list_siblings`) reads `server_config.json` and caches
  for 30s under a dedicated `_siblings_lock` — MUST be called OUTSIDE
  the hot-path `_lock` to avoid contention.
- Legacy env-var setups (no `providers` in config) enumerate
  `LLM_API_KEYS` under `provider_id='default'` with names `key_<i>`
  (dispatcher's `_build_slots_from_env` convention). Multi-provider setups
  use `{provider_id}_key_{i}` (see `_build_slots_from_providers`).
- Stats entries (`exhausted=True` etc.) are still WRITTEN normally in
  `record_rate_limit` / `mark_key_exhausted` — the guard lives at read
  time only, so UI surfaces the "auto-stopped" badge correctly while
  dispatch keeps the one chosen key usable.
- `get_today_stats` / `get_all_stats` return a `last_resort: bool` field
  (True ONLY on the chosen winner, not every sibling); `enabled` reflects
  the override (last_resort=True ⇒ enabled=True when there's no explicit
  user override).
- Log "[KeyStats] Keeping %s enabled as last-resort ..." fires at most
  once per (day, pk) via `_last_resort_logged` set, cleared on day rollover.
- Test: `python3 debug/test_key_stats_last_resort.py` covers 9+ scenarios
  (a, b, b2, b3, b4, c, d, e) including the reported screenshot case
  where a sticky-exhausted but high-success key must beat a truly-broken
  sibling. Patches `ks._list_siblings` and `ks._STATS_PATH` to a tempdir.

