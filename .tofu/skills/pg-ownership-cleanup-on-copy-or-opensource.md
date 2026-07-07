---
name: pg-ownership-cleanup-on-copy-or-opensource
description: pgdata copy self-heals via .pg_instance_id path-stamp (different abs-path); SAME-abs-path-on-shared-FUSE now self-heals via TOFU_PG_STANDALONE=1 (seeded into every export .env) clearing inherited remote-owner marker. Manual reset-ownership only as fallback.
enabled: true
tags: [postgresql, fuse, copy, open-source, cleanup, mandatory, pgdata, cross-machine, deployment]
created: 2026-03-30T06:12:47Z
updated: 2026-06-20T15:17:57Z
---

# PostgreSQL Ownership on Copy / Fork / Open-Source

## ✅ AUTOMATIC self-heal (two complementary mechanisms)

### 1. Different absolute path → `.pg_instance_id` copy-detect (2026-06)
Copying `data/pgdata/` to a **different absolute path** (colleague's home, `tofu-meituan2`, OSS clone) self-heals.
- `.pg_instance_id` JSON stamp (`{path,id,created}`) written by `_write_instance_stamp()` via `_mark_pg_owned_locally()`.
- `_pgdata_was_copied()` = stamp canonical path != current realpath.
- `_heal_if_copied()` clears `.pg_owner_host` + `.tofu_heartbeat` (NOT pidfile), wired FIRST in `_pg_already_running_on_another_machine()`.

### 2. SAME absolute path on shared FUSE → `TOFU_PG_STANDALONE=1` (2026-06-20)
**The gap mechanism #1 can't cover**: on shared FUSE storage every container/host sees the copy at the SAME abs-path as the source, so the stamp matches → copy-detect returns False → the inherited `.pg_owner_host` (pointing at the source machine or a dead old container) makes Step 3 "defer to remote" → every DB call crashes with `connection to server at "127.0.0.1", port 15432 failed: timeout expired`. (Seen on tofu-personal: inherited owner `10.20.49.98`, a defunct codelab container; scheduler + mcp keepalive were just the first callers to hit it.)

Fix (`lib/database/_bootstrap.py`):
- `_standalone_mode()` reads `TOFU_PG_STANDALONE` (1/true/yes/on).
- `_heal_if_standalone_remote_owner(pgdata)`: if standalone AND owner_host is remote AND pidfile PID is NOT a live local postgres → clear inherited owner + heartbeat, own PG locally. No-ops if flag unset / owner is local / our own live postmaster (IP-flap guard). Audit: `pg_standalone_heal_remote_owner`.
- Wired in `_pg_already_running_on_another_machine()` right AFTER `_heal_if_copied()`.
- **`export.py` seeds `TOFU_PG_STANDALONE=1` into every exported `.env`** (`_create_skeleton`, internal+opensource; personal mode keeps the user's real `.env`). → exported copies self-heal with zero manual steps.
- **Disables same-path multi-host failover** (the `pg-cross-host-heartbeat-takeover` feature). That's intentional: standalone deployments don't use failover. Leave the flag UNSET to keep failover.

## Discoverable CLI — `lib/database/pg_admin.py`
```bash
python -m lib.database.pg_admin status            # stamp + owner_host + heartbeat; prints COPIED if mismatched
python -m lib.database.pg_admin reset-ownership [--yes]   # clears markers, keeps data; REFUSES (rc=2) if pidfile is a live local postgres
```

## When you STILL act manually
1. Legacy copy without the standalone flag at the exact same abs-path on a different machine → `reset-ownership`, or just add `TOFU_PG_STANDALONE=1` to `.env`.
2. **Open-sourcing / distributing**: want DATA gone → `rm -rf data/pgdata/ data/*.db logs/*.log data/server_config.json`.

## Always prefer `pg_dump` over raw-copy
PG forces `0700` on pgdata. Hot raw-copy across FUSE corrupts TOAST. `export.py` uses `pg_dumpall`.

## ⚠️ Testing pitfall (boot verification)
A `python server.py` boot test launched from inside a running tofu server INHERITS that parent's `TOFU_PG_PORT`/`TOFU_PG_HOST` env → hits the "explicit PG target from env" short-circuit BEFORE owner/standalone logic, so the boot connects to the PARENT's port (e.g. 15439) and never exercises the heal. To test the heal against a real pgdata, use a CLEAN env: `env -i PATH=$PATH HOME=$HOME TOFU_PG_STANDALONE=1 python3 -c "from lib.database import _bootstrap as b; b._pg_already_running_on_another_machine('data/pgdata', 15432)"`. The unit tests (`test_pg_copy_self_heal.py`) are the authoritative verification.

## Tests
`tests/test_pg_copy_self_heal.py` — now 25 (was 18, +7 standalone: flag parsing, heal clears remote owner, no-op when flag-unset/owner-local/live-local-pg, Step-3 no-defer in standalone, Step-3 still-defers when not standalone). `test_pg_ip_flap_takeover.py` (5) unaffected. Full PG/db suite: 63 passed.

