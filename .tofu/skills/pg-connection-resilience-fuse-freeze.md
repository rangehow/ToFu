---
name: pg-connection-resilience-fuse-freeze
description: PostgreSQL connection resilience against FUSE/network freezes: TCP keepalives (30+3×10=60s detection), connect_timeout, statement_timeout, 4-tier health check, connection age recycling; plus unified database access pattern (all 50+ files use get_db/get_thread_db, scheduler migrated from raw _new_pg_connection)
enabled: true
tags: [postgresql, tcp-keepalive, fuse, connection-resilience, timeout, health-check, performance, unified-interface, scheduler, tmux]
created: 2026-03-28T03:51:40Z
updated: 2026-03-28T04:22:33Z
---

# PostgreSQL Connection Resilience for DolphinFS FUSE Environment

## Problem
DolphinFS FUSE mount freezes (e.g., laptop lid close, network disruption) cause ALL Python threads to hang indefinitely in D-state (uninterruptible sleep) on any I/O — including database operations. PostgreSQL pgdata lives on FUSE, so PG itself freezes.

## Solution: Multi-Layer Resilience in `lib/database.py`

### Layer 1: TCP Keepalives (kernel-level dead connection detection)
```python
conn = psycopg2.connect(
    PG_DSN,
    connect_timeout=CONN_TIMEOUT,        # 5s initial connect timeout
    keepalives=1,                         # enable TCP keepalives
    keepalives_idle=TCP_KEEPALIVE_IDLE,   # 30s start probing
    keepalives_interval=TCP_KEEPALIVE_INTERVAL,  # 10s probe interval
    keepalives_count=TCP_KEEPALIVE_COUNT, # 3 failures = dead
    application_name='chatui',            # visible in pg_stat_activity
)
# Dead detection: 30 + 3×10 = 60s (vs infinite before)
```

### Layer 2: Session-Level Timeouts
```python
cur.execute("SET SESSION statement_timeout = '%dms'" % STATEMENT_TIMEOUT_MS)        # 120s
cur.execute("SET SESSION idle_in_transaction_session_timeout = '%dms'" % IDLE_TXN_TIMEOUT_MS)  # 300s
```

### Layer 3: 4-Tier Connection Health Check (`_test_connection`)
```
Tier 1: Python-level closed flag check (0ms, no I/O)
Tier 2: Connection age > MAX_CONN_AGE (600s) → recycle (0ms, no I/O)
Tier 3: Last used < IDLE_CHECK_INTERVAL (30s) → trust (0ms, no I/O)
Tier 4: ROLLBACK + SELECT 1 for genuinely idle connections (~0.15ms)
```

### Layer 4: `db_execute_with_retry` enhancements
- Catches `psycopg2.InterfaceError` (connection already closed)
- Swaps underlying connection on reconnect and resets `_created_at`/`_last_used`
- Logs error type name for diagnostics

## Unified Database Access Pattern

**All 50+ production files** use the same interface:
- `get_db(domain)` — Flask request scope (18 route files)
- `get_thread_db(domain)` — thread-local, background threads (33 files)
- `db_execute_with_retry()` — retried writes (11 files)
- `json_dumps_pg()` — JSONB-safe serialization (5 files)

**Domains**: `DOMAIN_CHAT`, `DOMAIN_TRADING`, `DOMAIN_SYSTEM`

**Scheduler fix (2026-03-28)**: `lib/scheduler/manager.py` was using raw `_new_pg_connection()` (new connection per call, no reuse). Migrated to `get_thread_db(DOMAIN_SYSTEM)` — eliminated 13 `db.close()` calls, connection now reused across scheduler loop iterations.

**Only exceptions** (acceptable):
- `lib/swarm/artifact_store.py` `SQLiteBackend` — docstring-only, never instantiated in production
- `scripts/migrate_to_pg.py` — one-time migration script
- `tests/test_db_bug_regressions.py` — unit tests with temp databases

## Constants (in `lib/database.py`)
| Constant | Value | Purpose |
|---|---|---|
| `CONN_TIMEOUT` | 5s | Initial connect timeout |
| `TCP_KEEPALIVE_IDLE` | 30s | Start probing after idle |
| `TCP_KEEPALIVE_INTERVAL` | 10s | Probe interval |
| `TCP_KEEPALIVE_COUNT` | 3 | Failures before dead |
| `STATEMENT_TIMEOUT_MS` | 120000 | Per-query timeout |
| `IDLE_TXN_TIMEOUT_MS` | 300000 | Abandoned transaction timeout |
| `IDLE_CHECK_INTERVAL` | 30s | Skip health check if used recently |
| `MAX_CONN_AGE` | 600s | Recycle old connections |

## Environment Notes
- Project runs on `/mnt/dolphinfs/...` (BeeGFS FUSE mount, 19PB)
- `/home` is local SSD (252G) — good for logs symlink
- `/tmp` is local SSD (5.9T) — **not persistent** in Codelab
- Server runs in VS Code terminal → **use tmux** to survive SSH disconnects
- tmux binary is on FUSE but socket is `/tmp/tmux-*/` (local) — safe once loaded

