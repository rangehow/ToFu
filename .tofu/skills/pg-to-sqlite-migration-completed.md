---
name: dual-backend-pg-primary-sqlite-fallback
description: Database layer is dual-backend: PostgreSQL primary with SQLite fallback when PG unavailable
enabled: true
tags: [database, postgresql, sqlite, dual-backend, architecture, fallback]
created: 2026-04-14T18:06:27Z
updated: 2026-04-15T03:00:54Z
---

# Dual-Backend Database Architecture (2026-04-15)

## Architecture
The database layer (`lib/database/`) supports two backends:
- **PostgreSQL** (primary) — full concurrency, JSONB, tsvector search, connection pool
- **SQLite** (fallback) — used when PG is unavailable (no binary, no psycopg2, bootstrap failure)

## Files
| File | Purpose |
|---|---|
| `_core.py` | Backend detection, connection factory, pool, Flask/thread helpers |
| `_bootstrap.py` | PG server management (start/stop/discover) |
| `_sql_translate.py` | SQL compatibility translation for PG (?, PRAGMA, etc.) |
| `_wrappers.py` | DictRow, PgCursor, PgConnection for PG |
| `_schema_pg.py` | PostgreSQL DDL (SERIAL, JSONB, tsvector, pg_trgm) |
| `_schema_sqlite.py` | SQLite DDL (AUTOINCREMENT, FTS5) |
| `__init__.py` | Re-exports all public symbols from _core |

## Backend Detection (in `_core.py` at import time)
1. If `CHATUI_DB_BACKEND=sqlite` env var → force SQLite
2. Otherwise, try PG bootstrap → if success + psycopg2 available → use PG
3. If PG fails (no binary, bootstrap error, no psycopg2) → fall back to SQLite

## Key Variable
`_BACKEND` in `_core.py` is either `'pg'` or `'sqlite'`. All connection/pool/teardown
logic dispatches based on this.

## Consumer Code Unchanged
All 70+ consumer files (`routes/*.py`, `lib/*.py`) use the same API:
```python
from lib.database import get_db, get_thread_db, DOMAIN_CHAT, db_execute_with_retry
```
No consumer code needs to know which backend is active.

## Cross-Project Fix
Steps 2 and 2b in `_ensure_pg_running` use `_verify_pg_data_directory` (not `_pg_has_database`)
to prevent exported copies from hijacking each other's PG instance. `_verify_pg_data_directory`
returns `False` on any error (fail-safe).

