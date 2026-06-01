---
name: pg-connection-cascade-failure-in-crawl-loops
description: Bug pattern: single PG connection death cascades into 90+ errors when a long-running loop (intel crawler, backfill) holds one db reference — fix by detecting dead connections and calling get_thread_db() to reconnect mid-loop
enabled: true
tags: [python, postgresql, connection, cascade-failure, crawl-loop, bug-fix, intel]
created: 2026-03-29T04:24:26Z
updated: 2026-03-29T04:24:26Z
---

# PG Connection Cascade Failure in Crawl Loops

## Pattern
Long-running loops that iterate over many queries (intel crawler, backfill) obtain a single `db = get_thread_db()` at the start. If the PG connection dies mid-loop (FUSE hiccup, PG timeout), **every subsequent query** in the loop fails with `InterfaceError: connection already closed`, generating 90+ cascading error entries.

## Root Cause
- `get_thread_db()` caches the connection per-thread — it only health-checks on the **next call** to `get_thread_db()`.
- But the loop uses the cached `db` local variable, never calling `get_thread_db()` again.
- `db_execute_with_retry` tries to reconnect the underlying `_conn`, but the parent loop's `db` variable is already stale.

## Fix Pattern
Detect dead connections in the `except` block and re-obtain `db` from `get_thread_db()`:

```python
# Helper
def _reconnect_if_dead(db, exc):
    import psycopg2
    is_dead = isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
    if not is_dead:
        msg = str(exc).lower()
        is_dead = 'connection already closed' in msg or 'server closed' in msg
    if is_dead:
        from lib.database import get_thread_db, DOMAIN_TRADING
        logger.warning('[Intel] PG dead, reconnecting')
        return get_thread_db(DOMAIN_TRADING)
    return db

# In the loop
for query in queries:
    try:
        n = crawl(db, query)
    except Exception as e:
        logger.warning('Query failed: %s', e)
        db = _reconnect_if_dead(db, e)  # ← key fix
```

## Also Fixed
- `as_completed(futs, timeout=N)` raises `TimeoutError` — must wrap in try/except, not just the individual `f.result()` calls.

