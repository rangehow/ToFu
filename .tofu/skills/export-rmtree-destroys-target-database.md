---
name: export-rmtree-destroys-target-database
description: Bug fix: export.py shutil.rmtree(dest) destroys target's independent database — preserve data/ like .git
enabled: true
tags: [bug-fix, export, data-loss]
created: 2026-04-02T23:31:13Z
updated: 2026-04-02T23:31:13Z
---

# Export rmtree destroys target database

## Bug
`export.py` uses `shutil.rmtree(dest)` to clean the destination before re-exporting.
This destroys the target directory's **independent database** (PostgreSQL pgdata/ or SQLite *.db).

The export then excludes `*.db` and `pgdata/` from copying (they're "bulky runtime artifacts"),
so no database arrives at the destination. Result: fresh empty database, all conversation history lost.

## Root Cause
The code already preserved `.git` across re-exports (for incremental push), but didn't
preserve `data/` which contains the database and config.

## Fix
Preserve `dest/data/` to a temp dir before `rmtree`, restore it after, same pattern as `.git`.
This is safe because:
- In `personal` mode: source's `data/config/` overlays on top (desired), but `*.db` and `pgdata/` 
  are excluded so target's database stays intact
- In `internal`/`opensource` mode: `data` is in `ALWAYS_EXCLUDE_DIRS` so nothing from source 
  overwrites; `_create_skeleton()` uses `mkdir(exist_ok=True)` so it doesn't destroy existing files

