---
name: rate-limit-persistent-store
description: Pluggable memory/db rate-limit counter store; required for multi-worker WSGI (PR3c / C7 step 2, 2026-05-20)
enabled: true
tags: [rate-limit, database, §10.3, schema, ddos]
created: 2026-05-20T09:38:23Z
updated: 2026-05-20T09:38:23Z
---


# Rate-limit persistent counter store (2026-05-20)

`lib/rate_limiter.py` is now a thin wrapper around `lib/rate_limit_store.py`.
The store has two interchangeable backends behind `record_and_check(endpoint, ip, limit, per_seconds) → (allowed, count)`:

- **`MemoryRateLimitStore`** (default; `TOFU_RATE_LIMIT_BACKEND=memory`) — in-process dict, identical semantics to legacy.
- **`DatabaseRateLimitStore`** (`TOFU_RATE_LIMIT_BACKEND=db`) — INSERT into `rate_limit_events` then SELECT COUNT in the sliding window. Survives restarts; correct for gunicorn/uWSGI N>1 workers.

## Schema

Bumped `_SCHEMA_VERSION = 18 → 19` in **both** `_schema_pg.py` and `_schema_sqlite.py` (mandated by §10.3).

Table lives in the **system** domain:
```sql
CREATE TABLE rate_limit_events (
  id       BIGSERIAL/INTEGER AUTOINCREMENT PK,
  endpoint TEXT NOT NULL,
  ip       TEXT NOT NULL,
  ts_ms    BIGINT/INTEGER NOT NULL
);
CREATE INDEX idx_rate_limit_lookup ON rate_limit_events(endpoint, ip, ts_ms);
CREATE INDEX idx_rate_limit_ts ON rate_limit_events(ts_ms);
```

**Important: PK is auto-increment id, NOT (endpoint, ip, ts_ms).** First draft used the composite PK and immediately tripped `UNIQUE constraint failed` under burst traffic when two requests landed in the same millisecond. Caught by the test suite — keep the auto-increment PK.

## Failure mode: fail-open

If the table is missing (schema lag), or a SQL error fires, the store returns `(True, 0)` after a one-time WARN. A rate limiter must never take down the whole server. Marked permanently unavailable on "no such table" / "does not exist" so subsequent requests skip the broken DB attempt.

## Cleanup

Opportunistic per-call: each successful INSERT also DELETEs rows older than `per_seconds * 2` for the same `(endpoint, ip)`. No daemon thread.

## Backend factory

`get_store()` in `rate_limit_store.py` reads `TOFU_RATE_LIMIT_BACKEND` (legacy `CHATUI_RATE_LIMIT_BACKEND`) at call time. Memoizes per-backend; rebuilds on env change. Use `reset_for_test()` in tests to force re-creation.

## Tests
- `tests/test_rate_limit_store.py` (17 tests):
  - `TestMemoryStore` — within/at-limit, distinct IPs, distinct endpoints, window slide.
  - `TestDatabaseStore` — same semantics via DB; missing-table fail-open verified by monkeypatched `get_thread_db`.
  - `TestBackendSelection` — env var precedence, legacy alias, unknown→memory, memoization, swap rebuilds.
  - `TestDecoratorIntegration` — `@rate_limit` decorator → store → counter increment.

## Config rollout
Default `TOFU_RATE_LIMIT_BACKEND=memory` preserves today's behaviour. Docker compose / gunicorn deployments must set `=db` explicitly.

`docs/RATE_LIMITING_DOS_AUDIT_REPORT.md` §Rec 1 marked resolved.

