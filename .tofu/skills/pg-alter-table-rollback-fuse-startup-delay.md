---
name: pg-alter-table-rollback-fuse-startup-delay
description: Bug pattern: try-ALTER-TABLE-except-rollback for column migration causes ~30s FUSE WAL fsync per failed DDL statement on BeeGFS/FUSE — use _column_exists() check-before-ALTER instead
enabled: true
tags: [postgresql, fuse, startup, performance, alter-table, rollback, migration, bug-pattern]
created: 2026-03-31T22:01:08Z
updated: 2026-03-31T22:01:08Z
---

# PostgreSQL ALTER TABLE Rollback Causes Minutes-Long FUSE Startup Delay

## Pattern
Migration code that uses try-ALTER TABLE-except-rollback to add columns:

```python
# ❌ BAD — each failed ALTER triggers a PostgreSQL transaction abort + ROLLBACK
# On FUSE, each ROLLBACK requires a WAL fsync taking ~30 seconds
for col_name, col_def in columns:
    try:
        cur.execute(f'ALTER TABLE t ADD COLUMN {col_name} {col_def}')
    except Exception:
        conn.rollback()  # column already exists → 30s FUSE fsync penalty
```

With 13 columns, this caused **390 seconds (6.5 minutes)** startup delay.

## Fix
Check column existence first via `information_schema.columns` (read-only, no rollback):

```python
# ✅ GOOD — check before ALTER, no failed DDL, no rollback, instant on FUSE
for col_name, col_def in columns:
    if not _column_exists(conn, 'table_name', col_name):
        cur.execute(f'ALTER TABLE table_name ADD COLUMN {col_name} {col_def}')
        logger.info('Migration: added column %s', col_name)
```

## Root Cause
- PostgreSQL on FUSE (BeeGFS) has extremely slow WAL fsync operations (~30s each)
- Each failed DDL statement aborts the transaction, requiring a ROLLBACK
- ROLLBACK triggers WAL fsync even though nothing was actually written
- This compounds linearly: N columns × 30s = N×30s startup delay

## Where Fixed
- `lib/database.py` `_init_system_schema()` — 13 proactive agent columns on `scheduled_tasks`
- `lib/trading/historical_data.py` `_ensure_sim_tables()` — 6 OHLCV columns on `trading_sim_prices`

