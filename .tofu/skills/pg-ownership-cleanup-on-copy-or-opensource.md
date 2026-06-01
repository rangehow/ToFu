---
name: pg-ownership-cleanup-on-copy-or-opensource
description: MANDATORY cleanup when copying chatui to new path, new machine, or open-sourcing: delete data/pgdata/ ownership files (.pg_owner_host, postmaster.pid) to prevent new instance silently connecting to the original machine's PostgreSQL via FUSE cross-machine routing
enabled: true
tags: [postgresql, fuse, copy, open-source, cleanup, mandatory, pgdata, cross-machine, deployment]
created: 2026-03-30T06:12:47Z
updated: 2026-03-30T06:12:47Z
---

# PostgreSQL Ownership Cleanup on Copy / Fork / Open-Source

## Problem

When the chatui project is **copied to a new path**, **deployed to another machine**, or **open-sourced**,
the `data/pgdata/` directory contains ownership markers from the original machine. Due to the
FUSE-aware multi-machine design in `lib/database.py` `_ensure_pg_running()`, the new instance will
**silently connect to the original machine's PostgreSQL** instead of starting its own — causing:

1. **Shared data** — new instance reads/writes the original's database
2. **Privacy leak** — open-source users could inherit private conversation data
3. **Silent failure** — no error is shown; everything "works" but on the wrong DB

## Root Cause

`_ensure_pg_running()` Step 3 reads `data/pgdata/.pg_owner_host` and `data/pgdata/postmaster.pid`.
If they contain another machine's IP, the code treats that machine as the PG owner and connects remotely:

```python
# lib/database.py ~line 1810-1827
if is_remote_owner:
    return True, owner_host  # ← connects to ORIGINAL machine's PG!
```

## Files That MUST Be Cleaned Up

### Before copying / distributing / open-sourcing:

```bash
# MANDATORY: Remove these files/directories
rm -rf data/pgdata/            # Entire PG data directory (contains all DB data + ownership markers)

# Or at minimum, remove just the routing files (keeps data but disconnects):
rm -f data/pgdata/.pg_owner_host
rm -f data/pgdata/postmaster.pid
```

### Full cleanup checklist for open-source / distribution:

```bash
# 1. PostgreSQL data (private conversations, API keys in DB, etc.)
rm -rf data/pgdata/

# 2. SQLite databases (if any legacy ones exist)
rm -f data/*.db

# 3. Log files (may contain sensitive info)
rm -rf logs/*.log

# 4. Server config with learned model limits / API keys
rm -f data/server_config.json

# 5. Skill resolutions (project-specific)
rm -rf .chatui/

# 6. Any cached/temp files
rm -rf __pycache__/ lib/__pycache__/ routes/__pycache__/
```

### Add to .gitignore (for open-source):

```gitignore
data/pgdata/
data/*.db
logs/
data/server_config.json
.chatui/error_resolutions.json
```

## When This Applies

- **Copying code to a new machine** (especially on shared FUSE storage)
- **Open-sourcing the repository**
- **Giving the code to a colleague**
- **Creating a dev/staging fork**
- **Docker image builds** (don't bake in pgdata!)

## Recovery If Forgotten

If someone already started a copied instance and it's connected to the wrong DB:

```bash
# 1. Stop the server on the new machine
# 2. Clean up
rm -rf data/pgdata/
# 3. Restart — it will auto-create a fresh PG via initdb
python server.py
```

