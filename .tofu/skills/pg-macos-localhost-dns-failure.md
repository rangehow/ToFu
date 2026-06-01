---
name: pg-macos-localhost-dns-failure
description: macOS 'localhost' DNS resolution fails with certain network configs (iPhone tethering, VPN) — always use 127.0.0.1 for local PG commands
enabled: true
tags: [postgresql, macos, bug-fix, networking]
created: 2026-04-03T03:48:03Z
updated: 2026-04-03T03:48:03Z
---

# macOS localhost DNS Resolution Failure

## Bug Pattern
On macOS, `createdb -h localhost`, `pg_isready -h localhost`, and other PG CLI tools
can fail with:
```
could not translate host name "localhost" to address: nodename nor servname provided, or not known
```

This happens when:
- Connected via iPhone tethering (IP: 172.20.10.x)
- Using certain VPN configurations
- `/etc/hosts` doesn't have `localhost` mapped (rare but happens)
- Network configuration changes during startup

## Fix
**Always use `127.0.0.1` instead of `localhost` for local PG operations:**
- `createdb -h 127.0.0.1`
- `pg_isready -h 127.0.0.1`
- `psql -h 127.0.0.1`
- DSN: `host=127.0.0.1 port=15432 dbname=chatui`
- Default `PG_HOST = '127.0.0.1'` (not `'localhost'`)
- `_find_free_port` uses `socket.connect_ex(('127.0.0.1', port))`

Note: `psycopg2.connect(host='localhost')` may work (it tries multiple resolution methods),
but CLI tools like `createdb` rely on system DNS and fail.

## Related: Orphaned PG After Bootstrap Failure
When `_bootstrap_pg()` starts PG successfully but `createdb` fails:
1. PG is running but no database exists
2. `_bootstrap_pg()` returns `None` → PG_PORT set to 0
3. Next boot finds stale pidfile, deletes it, but PG is still running on the port
4. `pg_ctl start` fails ("could not start server")

Fix: After deleting stale pidfile, check if PG is already responding on our port
with `pg_isready -h 127.0.0.1`, verify data_directory, and reuse it.

## Files
- `lib/database/_bootstrap.py` — all PG CLI calls
- `lib/database/_core.py` — PG_HOST default

