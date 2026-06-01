---
name: pg-bootstrap-failure-cross-project-data-leak
description: Critical fix: cross-project PG reuse via _pg_has_database causes data leak AND PID-file duel crashes when exported copies run on same machine
enabled: true
tags: [postgresql, critical-bug, data-leak, cross-project, bootstrap, security, database, pidfile-duel]
created: 2026-04-01T06:36:25Z
updated: 2026-04-14T15:37:37Z
---

# Cross-Project PG Reuse Bug (Data Leak + PID-File Duel)

## The Problem

When two copies of the project (original + export) run on the same machine:

1. **Export has no pgdata** (excluded by export.py) → needs to bootstrap
2. **Step 2b** in `_ensure_pg_running()` scans ports 15432-15439 for ANY PG with database "chatui"
3. **Finds the original's PG** → reuses it instead of bootstrapping its own
4. Both projects now share the same PG → **data leakage** (exported copy reads all conversations)
5. When original PG crashes, exported copy detects crash → bootstraps new PG on same port
6. Original PG restarts → **two PGs fight over postmaster.pid** → both die
7. PG log shows: `lock file "postmaster.pid" contains wrong PID` → immediate shutdown

## Root Cause

`_pg_has_database()` matched by database NAME ("chatui") without verifying `data_directory`.
Multiple projects use the same default database name, so name-only matching is insufficient.

## The Fix (2026-04-14)

1. **Step 2**: When PG on configured port isn't ours (`_verify_pg_data_directory` returns False),
   NEVER fall back to `_pg_has_database`. Only scan for PG via `_scan_for_our_pg` (data_directory verified).

2. **Step 2b**: Replaced `_pg_has_database` port scan with `_scan_for_our_pg` — only matches
   PG instances whose `SHOW data_directory` matches our pgdata path.

3. **`_verify_pg_data_directory`**: Changed fail-open (return True on error) to fail-safe
   (return False on error). If we can't verify, assume NOT ours.

## Key Principle

**Never identify a PG instance by database name alone.** Always verify via `SHOW data_directory`.
The database name "chatui" is shared across all copies. The data_directory is unique per project.

