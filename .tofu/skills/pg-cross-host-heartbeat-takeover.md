---
name: pg-cross-host-heartbeat-takeover
description: Cross-host PG ownership uses .chatui_heartbeat (TTL 120s, refresh 30s); auto-heals after unclean exits on shared FUSE pgdata, eliminates "connection ... timeout expired" to a stale remote owner
enabled: true
tags: [database, postgres, fuse, bootstrap, heartbeat]
created: 2026-05-08T08:12:20Z
updated: 2026-05-08T08:12:20Z
---

# Cross-Host PG Ownership via Chatui Heartbeat

## Problem
Single-server deployment with shared FUSE pgdata across machines: when the previous server didn't exit cleanly, its postmaster still answered TCP, so a new chatui on a different host happily routed every DB call across the network. When that orphaned remote PG eventually flapped, every request failed with:
```
psycopg2.OperationalError: connection to server at "<old-host>", port <port> failed: timeout expired
```

The earlier `_pg_real_connect_ok` probe was insufficient — TCP-alive ≠ chatui-alive.

## Fix (lib/database/_bootstrap.py)
Introduced a chatui-level heartbeat file `pgdata/.chatui_heartbeat` containing `{host, pid, ts}`. The owner-process refreshes its mtime every 30s; peers consider the previous owner dead after 120s of staleness.

Constants:
- `_HEARTBEAT_FILE = '.chatui_heartbeat'`
- `_HEARTBEAT_TTL_S = 120`
- `_HEARTBEAT_REFRESH_S = 30`

API:
- `_heartbeat_is_fresh(pgdata) -> (fresh, info_dict)` — TTL-checks mtime; reads payload for logging.
- `_write_heartbeat(pgdata)` — atomic write via `os.replace`.
- `_clear_heartbeat(pgdata)` — best-effort delete.
- `_start_heartbeat_thread(pgdata)` — idempotent daemon thread.
- `stop_heartbeat(pgdata)` — joins thread + clears file. Called from `_stop_pg` and from `_core.stop_local_pg_if_owned` even when `CHATUI_STOP_PG_ON_EXIT=0`.

Wired in: `_mark_pg_owned_locally(pgdata)` now starts the heartbeat thread. All 6 call sites updated to pass `pgdata`.

## Decision points changed
1. **Step 3 (`_pg_already_running_on_another_machine` → defer to remote)**: now requires `_heartbeat_is_fresh(pgdata) AND _pg_real_connect_ok(...)`. Stale or missing heartbeat → take over.
2. **Step 4 safety net (`postmaster.pid` belongs to remote)**: same combined gate. On takeover, `.pg_owner_host` AND `.chatui_heartbeat` are both cleared.

## Why heartbeat instead of just TCP
A stale PG on another host can answer TCP for hours after the chatui process there is dead. The heartbeat is bound to *chatui*'s liveness, not the postmaster's, so it accurately distinguishes "a peer is using PG right now" from "an abandoned PG is coincidentally still up".

## Verified behaviours (smoke-tested 2026-05-08)
- Missing heartbeat → take over.
- Heartbeat present and fresh (within TTL) → defer to remote.
- Heartbeat present but mtime > TTL → take over.
- `_stop_pg` and `stop_local_pg_if_owned` (incl. the `CHATUI_STOP_PG_ON_EXIT=0` branch) clear heartbeat → next host can take over without delay.

## File locations
- `lib/database/_bootstrap.py` — heartbeat helpers + Step-3/4 gating + `_mark_pg_owned_locally(pgdata)`.
- `lib/database/_core.py` `stop_local_pg_if_owned()` — calls `stop_heartbeat(_PGDATA)` even when leaving PG running.

