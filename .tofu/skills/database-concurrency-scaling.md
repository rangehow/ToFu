---
name: database-concurrency-scaling
description: Database layer concurrency improvements for 1000+ concurrent users
enabled: true
tags: [database, concurrency, performance, configuration]
created: 2026-04-15T03:47:25Z
updated: 2026-04-15T03:47:25Z
---

# Database Concurrency Scaling (April 2026)

## Changes Made

### `lib/database/_core.py`
- **PG semaphore**: `_MAX_TOTAL_CONNS` 40 → 200 (env: `CHATUI_DB_MAX_CONNS`)
- **PG pool max**: `_CONN_POOL_MAX` 16 → 50 (env: `CHATUI_DB_POOL_MAX`)
- **Acquire timeout**: 10s → 30s (env: `CHATUI_DB_ACQUIRE_TIMEOUT`)
- **SQLite busy timeout**: 10s → 30s (env: `CHATUI_SQLITE_BUSY_TIMEOUT_MS`)
- **SQLite connection pooling**: Added `_sqlite_pool` (max 20, env: `CHATUI_SQLITE_POOL_MAX`)
- **SQLite WAL autocheckpoint**: 1000 pages (reduce I/O stalls)
- **Connection reaper**: 60s → 30s interval
- **Pool metrics**: Logged every 5 minutes via `_log_pool_metrics()`
- **close_db() teardown**: Now uses `_pool_put()` for both PG and SQLite
- **shutdown_pool()**: Now drains SQLite pool too
- **Better error diagnostics**: Semaphore timeout error includes pool stats and env var hint

### `lib/database/_bootstrap.py`
- **PG max_connections**: 200 → 500

### Environment Variables (all optional)
```
CHATUI_DB_MAX_CONNS=200        # PG: max app-side connections (semaphore)
CHATUI_DB_POOL_MAX=50          # PG: connection pool size
CHATUI_DB_ACQUIRE_TIMEOUT=30   # PG: semaphore acquire timeout (seconds)
CHATUI_SQLITE_BUSY_TIMEOUT_MS=30000  # SQLite: busy/lock wait (ms)
CHATUI_SQLITE_POOL_MAX=20     # SQLite: connection pool size
```

### Test
`python debug/test_db_concurrency.py --threads 1000 --ops 50` — 50K ops, 0 errors

