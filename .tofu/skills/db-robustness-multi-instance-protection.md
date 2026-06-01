---
name: db-robustness-multi-instance-protection
description: Database robustness: instance locking, connection budget auto-tuning, graceful shutdown, PG safety nets
enabled: true
tags: [database, robustness, multi-instance, connection-pool, graceful-shutdown, locking]
created: 2026-04-14T16:01:40Z
updated: 2026-04-14T16:01:40Z
---

# Database Robustness & Multi-Instance Protection

## Changes Made (2026-04-14)

### 1. Instance Locking (server.py)
- File-based lock (`data/.server.lock`) using `fcntl.flock()` (Unix) / `msvcrt.locking()` (Windows)
- Prevents multiple `python server.py` on the same project directory
- Clear error message explaining why and how to fix
- Fail-open: if lock file creation fails, startup continues (don't block on minor issues)

### 2. SIGTERM Graceful Shutdown (server.py)
- Signal handler converts SIGTERM → `sys.exit(0)` so `atexit` handlers run
- Without this, `kill <pid>` / Docker stop / systemd stop would skip connection cleanup

### 3. Connection Pool Shutdown (lib/database/_core.py)
- `shutdown_pool()` drains all pooled connections + all thread-local connections on exit
- Registered via `atexit.register()` — runs on normal exit, sys.exit(), SIGINT
- Prevents "too many clients" on rapid server restart

### 4. Connection Budget Auto-Tuning (lib/database/_core.py → `_post_connect_setup()`)
- Queries PG `max_connections` and counts other chatui connections at startup
- Auto-reduces semaphore limit so combined connections never exceed PG capacity
- Formula: `budget = pg_max - 20 (reserved) - other_chatui_conns`
- Uses non-blocking semaphore acquire to permanently reduce capacity

### 5. PG Server-Level Safety Nets
- **In _bootstrap.py**: New PG instances get `tcp_keepalives_*`, `idle_session_timeout=1800s`,
  `log_disconnections=on` in `postgresql.conf`
- **In _core.py**: `_post_connect_setup()` applies these via `ALTER SYSTEM` + `pg_reload_conf()`
  for existing PG instances that lack them
- `idle_session_timeout` (PG 14+) kills connections idle >30 min — catches leaked connections
  from crashed app processes that bypass session-level timeouts

### 6. Enhanced Health Check (routes/common.py)
- `/api/health` now returns connection pool stats: `active_connections`, `max_connections`,
  `pool_idle`, `thread_tracked`
- Quick DB connectivity test with `SELECT 1`
- Returns `ok: false` + `db_error` if DB is unresponsive

### Key Parameters
- `_MAX_TOTAL_CONNS = 160` (initial, auto-reduced based on PG capacity)
- `max_connections = 200` in postgresql.conf
- `_PG_RESERVED = 20` slots for PG internals
- `idle_session_timeout = 1800s` (30 min)
- Instance lock file: `data/.server.lock`

