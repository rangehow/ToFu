---
name: chatui-dual-backend-database
description: Tofu/chatui uses dual-backend DB: PostgreSQL primary, SQLite fallback — not SQLite-only
enabled: true
tags: [database, postgresql, sqlite, architecture]
created: 2026-04-18T14:43:38Z
updated: 2026-04-18T14:43:38Z
---

# Tofu/chatui — Dual-Backend Database Architecture

**Do NOT assume this project is SQLite-only.** Despite older CLAUDE.md copies
saying so, the actual architecture is:

- **Primary: PostgreSQL 18+** (auto-bootstrapped local userspace process, no sudo)
- **Fallback: SQLite** (`data/chatui.db`), used when PG is unavailable or
  `CHATUI_DB_BACKEND=sqlite` is set

## Layout (lib/database/)
- `_core.py` — Connection factory, pool, config constants (`PG_HOST`, `PG_PORT=15432`,
  `PG_DBNAME='chatui'`, `DB_PATH`). Env vars: `CHATUI_PG_HOST/PORT/DBNAME/USER/PASSWORD`,
  `CHATUI_DB_PATH`, `CHATUI_DB_BACKEND`, `CHATUI_DB_MAX_CONNS`, `CHATUI_DB_ACQUIRE_TIMEOUT`.
- `_bootstrap.py` — Auto-bootstrap local PG; tries `conda install -c conda-forge postgresql>=18`
  via `bootstrap.py` when `initdb`/`pg_ctl` missing.
- `_schema_pg.py` — PG DDL + migrations (JSONB, tsvector).
- `_schema_sqlite.py` — SQLite DDL + migrations (mirror of PG).
- `_sql_translate.py` — SQLite-flavored SQL → PG translation at wrapper layer.
- `_wrappers.py` — Uniform `execute/fetchone/fetchall` API over both backends.

## Schema changes — MUST update BOTH backends
Any `ALTER TABLE`, new table, new index must be added to **both** `_schema_pg.py`
and `_schema_sqlite.py`. May also need `_sql_translate.py` tweaks.

## Docs
- `README.md` / `README_CN.md` correctly describe dual-backend.
- `CLAUDE.md` was out of date (SQLite-only wording) — fixed 2026-04-18 to match reality.
- When user debugs "database" issues, check `CHATUI_DB_BACKEND` and which backend
  is actually running — don't assume SQLite.
