---
name: startup-race-import-time-threads-vs-init-db
description: Startup race: import-time background threads must gate on schema readiness, not race init_db
enabled: true
tags: [startup, database, scheduler, billing, bug-pattern, postgres]
created: 2026-06-01T03:34:08Z
updated: 2026-06-01T03:34:08Z
---

# Startup Race: Import-Time Background Threads vs init_db()

## The trap

`register_all(app)` (routes/__init__.py, via server.py:617) and
`start_janitor()` (server.py:681) run at **module-import time**. But
`init_db()` runs *later*, inside the `_startup()` async hook
(server.py:819) — and schema DDL takes **~30s on FUSE/BeeGFS**.

So any background thread or synchronous bootstrap kicked off during
import will query tables/columns that don't exist yet → crashes with
psycopg2 `UndefinedTable` / `UndefinedColumn`, spamming `error.log` on
every fresh-DB boot.

Observed symptoms (in logs, all at startup timestamps, gone after init
completes):
- `[Scheduler] Could not auto-register Daily Optimizer: column "target_conv_id" of relation "scheduled_tasks" does not exist`
- `[Janitor] sweep crashed: relation "billing_ledger" does not exist`

## Why it's a REAL bug, not just noise (important distinction)

- **Scheduler optimizer register**: `_ensure_default_optimizer_task()`
  was a **one-shot** call in `mgr.start()`. The 30s scheduler loop never
  re-invokes it. So on a fresh/migrated DB it crashed once and the Daily
  Optimizer task was **never registered** — a permanent functional
  failure, not transient noise.
- **Janitor**: first sweep ran immediately and crashed; it *would* self-heal
  on the next 5-min tick, but only incidentally.

## The fix pattern: schema-readiness gate (root cause)

Defer the work behind a poll that probes the EXACT table/column needed,
respecting the stop event, with a bounded timeout that logs loudly on
expiry:

```python
def _wait_for_ledger_table(timeout=120.0) -> bool:
    from lib.database import db_available
    deadline = time.monotonic() + timeout
    while not _stop.is_set() and time.monotonic() < deadline:
        if db_available:
            try:
                db = get_thread_db(DOMAIN_SYSTEM)
                db.execute('SELECT 1 FROM billing_ledger LIMIT 0')
                return True
            except Exception as e:
                logger.debug('...not ready yet: %s', e)  # aborted txn auto-rolled back by PgCursor
        _stop.wait(2)
    return False
```

For the scheduler, there was already a proven gate: the
`_deferred_resume` thread in `start_scheduler_worker()` that polls for
`timer_watchers`. Reuse it — `timer_watchers` and the `scheduled_tasks`
proactive columns are both created in the same `_init_system_schema()`
pass, so once the gate passes BOTH are guaranteed present. I extended
the probe to also `SELECT target_conv_id FROM scheduled_tasks LIMIT 0`
and moved `_ensure_default_optimizer_task()` into that thread (out of
`mgr.start()`).

## Log-level layer (secondary)

ON TOP of the gate, the residual missing-table exception logs at
**debug** (`UndefinedTable` / "does not exist"), real failures stay
`error` w/ exc_info. Per the self-recovering-fallback convention. This
alone would NOT fix the optimizer-never-registered bug — the gate does.

## Key facts
- `PgCursor.execute` rolls back the aborted txn after a failed probe, so
  each retry uses a clean connection — safe to poll in a loop.
- `lib.database.db_available` is the import-safe flag to check before
  touching the DB.
- Files: `lib/billing/janitor.py` (`_wait_for_ledger_table` + gated
  `_loop`), `lib/scheduler/manager.py` (`start()` no longer registers;
  `start_scheduler_worker._deferred_resume` does, after dual probe).
- Whenever you add a new import-time background thread, gate its first DB
  access on the specific schema object it needs.

