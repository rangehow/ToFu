---
name: pg-too-many-clients-fix
description: Fix for PostgreSQL 'too many clients' deadlock: semaphore, reaper, idle_in_transaction_session_timeout, close_db rollback
enabled: true
tags: [database, postgresql, connection-pool, deadlock]
created: 2026-04-11T01:25:53Z
updated: 2026-04-11T01:25:53Z
---

# PostgreSQL "Too Many Clients" Fix (2026-04-11)

## Root Causes
1. `max_connections = 50` was too low for 34+ daemon thread spawn points
2. Thread-local connections (`get_thread_db()`) leaked — threads die but connections stay open
3. "idle in transaction" zombie connections — SELECT queries leave PG in transaction state; `close_db()` only committed dirty writes, never rolled back clean reads
4. No application-side connection limit — any thread could create a new connection without cap

## Fix Components

### A: Application-side governance (`_core.py`)
- **BoundedSemaphore** `_MAX_TOTAL_CONNS = 40` — caps total connections; 10s timeout with clear error
- **Thread connection registry** — weakrefs to threads + PgConnections for tracking
- **Reaper daemon thread** — every 60s, closes connections from dead threads
- **`close_db()` always rollbacks** — even clean reads get `db.rollback()` to prevent "idle in transaction"
- **`_CONN_POOL_MAX`** increased from 8 → 16

### B: PgConnection.close() (`_wrappers.py`)
- Releases semaphore slot on close
- Decrements `_conn_count` for monitoring

### C: PostgreSQL config
- `max_connections = 200` (was 50, requires PG restart)
- `idle_in_transaction_session_timeout = 300s` at server level (backup for per-session SET failures)
- Both also added to `_bootstrap_pg()` for new installs

