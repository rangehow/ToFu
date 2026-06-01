---
name: pg-silent-crash-no-auto-restart-bug
description: FIXED 2026-05-07: PG self-heal extended to handle zombie postmaster (TCP-alive but /dev/shm segments wiped) — adds force-stop before re-ensure + pool drain
enabled: true
tags: [database, postgres, reliability, bug, fixed, crash-recovery, auto-heal]
created: 2026-04-20T05:54:41Z
updated: 2026-05-07T05:11:16Z
---

# PostgreSQL Silent Crash — Auto-Restart  (✅ FIXED 2026-04-24, EXTENDED 2026-05-07)

## Status

**FIXED** in `lib/database/_core.py`. Two failure modes covered:

1. **Postmaster dead** (`Connection refused`) — fixed 2026-04-24.
2. **Zombie postmaster** (`could not open shared memory segment "/PostgreSQL.<N>"`) — fixed 2026-05-07.

## Failure mode 1: Postmaster fully dead

Symptom: `psycopg2.OperationalError: ... Connection refused`.
Cause: PG crashed (OOM, host reboot, etc.). Socket dead.
Recovery (already in place): `_maybe_reboot_pg()` → `_ensure_pg_running()`.

## Failure mode 2: Zombie postmaster (NEW 2026-05-07)

Symptom:
```
psycopg2.OperationalError: connection to server at "127.0.0.1", port 15439
failed: FATAL: could not open shared memory segment "/PostgreSQL.3135188980":
No such file or directory
```

Cause: Postmaster process is **alive** and TCP-accepting, but every fresh
backend child FATALs at startup because `/dev/shm/PostgreSQL.*` segments
have been wiped. Common in containerized deployments where the
container's `/dev/shm` gets cleaned, or after pause/resume/checkpoint.

Why the 2026-04-24 fix didn't catch it:
- Self-heal only matched `'Connection refused'`.
- Even if it had triggered, `_ensure_pg_running()` calls `pg_isready`
  which sees the zombie postmaster as healthy → silently no-ops.

## Applied extension (2026-05-07)

In `lib/database/_core.py`:

1. **Signature lists**:
   ```python
   _PG_DEAD_SIGNATURES = (
       'Connection refused',
       'could not open shared memory segment',
       'server closed the connection unexpectedly',
   )
   _PG_ZOMBIE_SIGNATURES = ('could not open shared memory segment',)
   ```
   `_pg_error_is_dead()` and `_pg_error_is_zombie()` helpers wrap them.

2. **`_force_stop_zombie_pg()`** — kills the live-but-broken postmaster
   so a fresh start can take over. Tries:
   - `pg_ctl stop -m immediate -w -t 10`
   - Read `postmaster.pid` → SIGQUIT → SIGKILL
   - Remove stale `postmaster.pid`

3. **`_maybe_reboot_pg(reason, force_stop_first=False)`** — gained
   `force_stop_first` flag. When True, calls `_force_stop_zombie_pg()`
   before `_ensure_pg_running()`.

4. **`_drain_pg_pool()`** — closes all pooled connections after a
   successful reboot (they all point at the dead postmaster).

5. **`_new_pg_connection`** updated:
   ```python
   if not _pg_error_is_dead(err_txt):
       raise
   is_zombie = _pg_error_is_zombie(err_txt)
   attempted = _maybe_reboot_pg(err_txt[:200], force_stop_first=is_zombie)
   ```

Cooldown (`CHATUI_PG_REBOOT_COOLDOWN_S`, default 60s) and audit-logging
behavior unchanged. Auth/host/missing-DB errors still re-raise without
attempting reboot.

## Quick diagnostics if it recurs

```bash
ls -la /dev/shm/PostgreSQL.* 2>/dev/null   # missing = zombie symptom
ps -p $(head -1 data/pgdata/postmaster.pid) -o pid,cmd  # postmaster alive?
grep "could not open shared memory" logs/error.log | tail
grep "force_stop=True" logs/audit.log     # zombie-recovery attempts
grep "pg_auto_restart" logs/audit.log     # all self-heal attempts
```

## Smoke test (manual repro)

```bash
# Confirm /dev/shm wipe -> zombie
sudo rm /dev/shm/PostgreSQL.*
# any API call now triggers self-heal:
curl localhost:5000/api/conversations
# Expect in logs/app.log:
#   [DB] PG appears dead (... could not open shared memory segment ...)
#       — attempting re-bootstrap once (... force_stop=True)
#   [DB] Force-stopped zombie PG via pg_ctl -m immediate
#   [DB] PG re-bootstrap succeeded
#   [DB] Drained N stale pooled connections after PG reboot
#   [DB] Retrying psycopg2.connect after PG re-bootstrap
```

