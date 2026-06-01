---
name: pg-auto-stop-on-server-exit
description: PG auto-stops when server.py exits via shutdown_pool → _stop_pg; gated by CHATUI_STOP_PG_ON_EXIT (default 1), only stops local PG we own
enabled: true
tags: [postgresql, shutdown, atexit, container-switch, fuse, bootstrap]
created: 2026-04-23T14:39:50Z
updated: 2026-04-23T14:39:50Z
---

# PG Auto-Stop on Server.py Exit

## Problem solved

Shared FUSE `pgdata/` + multi-host dev (e.g. VS Code container switch): user Ctrl+C's `server.py` on host A, moves to host B, runs `server.py` on B. Bootstrap reads `.pg_owner_host` = host A's IP, probes it, **succeeds** (host A's PG is still running — only Flask died, not PG!), and returns that DSN. Runtime queries from host B's network then all time out. User's workaround was to return to host A, which is painful.

## Fix (2026-04-23)

`lib/database/_bootstrap.py` now tracks ownership via a module-level `_PG_STARTED_BY_US` flag:
- Set to True in 4 return paths where we start or take over a local PG:
  1. `_bootstrap_pg` (brand-new initdb path)
  2. Step 2 "PostgreSQL already running on localhost (verified ours)"
  3. Step 2b scan recovery (`_scan_for_our_pg`)
  4. The local `pg_ctl start` success path at end of `_ensure_pg_running`
  5. Port-conflict reuse branch (existing PG on conf_port passes data_directory check)
- **Never** set for remote (Step 1 explicit external, Step 3 defer-to-remote).
- Exposed as `is_pg_owned_locally()`.

`lib/database/_core.py` `shutdown_pool()` (runs via `atexit`) now:
- Drains app-side connection pool (existing behavior).
- If `is_pg_owned_locally()` AND env var `CHATUI_STOP_PG_ON_EXIT` != `0/false/no/off`, calls `_stop_pg(_PGDATA)` → `pg_ctl stop -m fast`.
- Default behavior = **stop on exit**. Opt-out: `CHATUI_STOP_PG_ON_EXIT=0` for rapid dev-restart cycles where you don't want PG startup latency.

## Triggers atexit on which signals?

- Ctrl+C (SIGINT → KeyboardInterrupt) ✅ — atexit fires
- SIGTERM ✅ — server.py has a SIGTERM handler that calls `sys.exit(0)`
- `sys.exit(0)` ✅
- `kill -9` / segfault / `os._exit()` ❌ — PG stays running (same as before; no way around this)

## User-facing contract

- Normal stop (`Ctrl+C` or `kill <pid>`): PG stops cleanly, ownership markers removed by `pg_ctl stop`. Next `server.py` on ANY host starts clean.
- `kill -9`: ownership markers remain. Existing Step 4 auto-heal in `_ensure_pg_running` probes the old IP with real psycopg2 connect; if unreachable it rewrites markers and takes over. So worst case = one silent auto-heal on next startup, no user action.

## Edge cases handled

- REMOTE PG: never stopped. `_PG_STARTED_BY_US` stays False.
- SQLite backend: no-op in `shutdown_pool` (new code is in the `if _BACKEND == 'pg'` block).
- Import-time test sets the flag (expected) — confirmed via `python -c "from lib.database import _bootstrap; print(_bootstrap.is_pg_owned_locally())"` prints True after a normal import.

## Related memories

- `pg-container-switch-auto-heal` — startup-time auto-heal path (Step 4)
- `pg-ownership-cleanup-on-copy-or-opensource` — manual cleanup for copy/export
- `pg-pidfile-deletion-cross-machine-bug` — earlier cross-machine safety work
- `db-robustness-multi-instance-protection` — instance lock + SIGTERM graceful shutdown context

