---
name: pg-auto-stop-on-server-exit
description: server.py now auto-stops locally-owned PG on exit via _PG_STARTED_BY_US flag + shutdown_pool hook; controlled by CHATUI_STOP_PG_ON_EXIT (default on)
enabled: true
tags: [postgresql, shutdown, atexit, container-switch, fuse, ownership]
created: 2026-04-23T14:42:02Z
updated: 2026-04-24T07:41:42Z
---

# PG Auto-Stop on server.py Exit

## Motivation

`pg_ctl start` detaches PG from `server.py`'s process tree. Ctrl+C stopped Flask but left PG running. On shared FUSE `pgdata/`, switching hosts then made `.pg_owner_host` point to a still-live (but unreachable-from-new-host) PG → `psycopg2.connect()` timeouts.

## Critical bootstrap trap (DON'T put shutdown in _core.py atexit)

**First attempt** registered `_stop_pg` via an `atexit` hook inside `lib/database/_core.py`. This broke catastrophically because:

- The agent's `run_command` tool frequently spawns `python3 -c "..."` subprocesses.
- Those subprocesses import `lib.database._core` at module load, which auto-bootstraps PG and marks `_PG_STARTED_BY_US = True` (if PG is already running and matches our pgdata, we "take over").
- When the subprocess exits (e.g. after printing a number), its atexit → `shutdown_pool` → `_stop_pg` → **kills the PG that the parent server.py is using**.
- Observed in logs: 5 PG start/stop cycles in 60 seconds, each triggered by a different `python3 -c` run_command invocation. Server threw "Connection refused" on every DB call in between.

## Correct design

1. **`shutdown_pool()`** in `_core.py` ONLY drains connection pool. No PG shutdown. Still registered via `atexit` so it runs in every process.
2. **`stop_local_pg_if_owned()`** is a separate public function in `_core.py`. NOT registered globally.
3. **`server.py`** registers `stop_local_pg_if_owned` via `atexit.register()` AFTER instance lock acquisition — so ONLY the server.py process runs it.

## Ownership flag (`_PG_STARTED_BY_US`) in `_bootstrap.py`

Set to True at every local-PG return path (bootstrap, Step 2 reuse, Step 2 port-scan, Step 2b port-scan, Step 4 same-port reuse, Step 4 start). NEVER set for remote paths (explicit external, Step 3 defer-to-remote, Step 4 live-remote handoff).

The flag is still per-process (module-level), so subprocesses mark themselves as owners — that's fine because only server.py's atexit calls `stop_local_pg_if_owned`.

## Env var

`CHATUI_STOP_PG_ON_EXIT` (default `1`):
- `1`/unset: server.py stops local PG on exit
- `0`/`false`/`no`/`off`: leave PG running (faster dev-restart cycles)

## Exit-path coverage

| Signal           | Flow                                                             | Covered |
|------------------|------------------------------------------------------------------|---------|
| Ctrl+C (SIGINT)  | KeyboardInterrupt → unwinds `app.run()` → normal exit → atexit   | ✓       |
| SIGTERM          | `_sigterm_handler` → `sys.exit(0)` → atexit                      | ✓       |
| SIGKILL          | process dies instantly                                           | ✗ (Step 4 auto-heal on next start handles it) |

## Files changed

- `lib/database/_bootstrap.py`: module-level `_PG_STARTED_BY_US` flag, `_mark_pg_owned_locally()`, `is_pg_owned_locally()`
- `lib/database/_core.py`: `shutdown_pool()` trimmed to pool-only; new `stop_local_pg_if_owned()`
- `server.py`: register `stop_local_pg_if_owned` via atexit after instance-lock acquired

## Related

- `pg-container-switch-auto-heal` — partial startup-side cover
- `pg-ownership-cleanup-on-copy-or-opensource` — manual cleanup
- `pg-pidfile-deletion-cross-machine-bug` — cross-machine safety net

