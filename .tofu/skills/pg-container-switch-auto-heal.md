---
name: pg-container-switch-auto-heal
description: Fix for DB address errors after container switch (same shared pgdata, new host IP): Step 4 safety net now probes old owner_host reachability and auto-heals when unreachable
enabled: true
tags: [postgresql, container, vscode, cross-host, auto-heal, bug-fix, fuse, ownership]
created: 2026-04-18T07:31:05Z
updated: 2026-04-18T08:20:33Z
---

# PG Container-Switch Auto-Heal

## Problem

User uses web-based VS Code, switches between containers. Shared FUSE `pgdata/` carries ownership files (`.pg_owner_host`, `postmaster.pid`) from the *previous* container's IP. New container on startup sees stale markers and tries to connect to a dead IP → `psycopg2.OperationalError: connection to server at "10.x.x.x" ... failed: timeout expired`.

## Root Causes Fixed

### 1. Step 4 safety net refused to take over even when remote was dead
Originally checked only `owner_host != local_ip` → unconditionally connected to stale IP. Fix: probe reachability first, auto-heal when unreachable.

### 2. `pg_isready` gave false positives on "half-alive" containers
When a container loses network but postmaster process is still alive in some half-state, `pg_isready` accepts the TCP connection (returns OK) but real `psycopg2.connect()` hangs until timeout. So Steps 2/3/4 all believed the dead remote was "reachable" and locked onto the stale DSN.

## Fix Applied (in `lib/database/_bootstrap.py`)

### New helper: `_pg_real_connect_ok(host, port, pg_user, pg_dbname, timeout_s=5)`
- Uses actual `psycopg2.connect()` + `SELECT 1`, not `pg_isready`.
- Returns False if the backend is half-alive (accepts TCP but can't serve queries).
- Always close connection via try/finally.

### All three reachability checks now use real-connect probe:
1. `_pg_already_running_on_another_machine` — the "deferring to remote" log.
2. Step 3 in `_ensure_pg_running` — decides whether to connect remote vs. fall to Step 4.
3. Step 4 safety net — decides whether to auto-heal and take over locally.

## Decision Matrix (per-startup)

```
.pg_owner_host has remote IP?
├─ real-connect succeeds  → defer to remote (concurrent multi-host, intended)
└─ real-connect fails     → treat as stale:
                            rm .pg_owner_host
                            rm postmaster.pid
                            start PG locally
                            (data files UNTOUCHED)
```

## Recovery Command (if a running server already locked onto stale DSN in memory)

Code fix only triggers at startup. If the server already cached a bad DSN (imported `_bootstrap.py` before the old container died), restart isn't enough — the running process still has the dead IP frozen. Do:

```bash
# Remove stale ownership markers BEFORE restart
rm -f data/pgdata/.pg_owner_host data/pgdata/postmaster.pid
# Then restart the server (user does this)
```

With markers gone, bootstrap falls through Step 3 cleanly (no pidfile → `is_remote=False`) and starts PG locally.

## Key Lesson

**`pg_isready` is a liveness indicator, not a "can serve real queries" indicator.** For ownership takeover decisions, always use a real `psycopg2.connect()` probe.

## Related Memories

- `pg-pidfile-deletion-cross-machine-bug` — original cross-machine bug (different scenario: two live hosts)
- `pg-ownership-cleanup-on-copy-or-opensource` — cleanup when copying project to another machine
- `pg-bootstrap-failure-cross-project-data-leak` — data_directory-based ownership (never database name)

