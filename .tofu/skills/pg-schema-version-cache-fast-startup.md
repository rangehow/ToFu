---
name: pg-schema-version-cache-fast-startup
description: Fix for PostgreSQL 5+ min startup on FUSE: schema version caching in trading_config skips redundant CREATE TABLE/INDEX DDL on subsequent boots — increment _SCHEMA_VERSION when schema changes
enabled: true
tags: [postgresql, startup, performance, fuse, schema, ddl, cache]
created: 2026-03-28T16:12:08Z
updated: 2026-03-28T16:12:08Z
---

# PostgreSQL Schema Version Cache — Fast Startup Fix

## Problem
`init_db()` runs ~80 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` + `ALTER TABLE` migration statements on every server startup. On FUSE-mounted storage (BeeGFS), each DDL statement triggers PostgreSQL catalog lookups and WAL fsync operations, resulting in **5+ minute startup** even when nothing has changed.

## Solution
Store a `_schema_version` integer in the `trading_config` table. On startup:
1. Read `_schema_version` from DB (2 lightweight SELECT queries, ~20ms)
2. If it matches `_SCHEMA_VERSION` constant → **skip all DDL** (fast path)
3. If it doesn't match (or table doesn't exist) → run full DDL, then write the new version

## Key Code (`lib/database.py`)
```python
_SCHEMA_VERSION = 1  # Increment when tables/columns/indexes change

def _get_schema_version(conn):
    # Check if trading_config exists, then SELECT value WHERE key='_schema_version'
    ...

def _set_schema_version(conn, version):
    # INSERT ... ON CONFLICT (key) DO UPDATE SET value = ...
    ...

def init_db():
    current_version = _get_schema_version(conn)
    if current_version == _SCHEMA_VERSION:
        return  # Fast path — skip all DDL
    # ... run full DDL ...
    _set_schema_version(conn, _SCHEMA_VERSION)
```

## When to Increment `_SCHEMA_VERSION`
- Adding a new table
- Adding a new column (ALTER TABLE migration)
- Adding a new index
- Changing column types or constraints

## Performance
- Before: ~5 min 25 sec startup (80+ DDL over FUSE)
- After: ~22 ms startup (2 SELECT queries)
- Speedup: **15,000×**

