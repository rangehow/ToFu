---
name: timer-watcher-zombie-bug-skipped-poll-not-counted
description: Timer watchers become immortal zombies because skipped polls (unchanged check_command output) don't increment poll_count → never hit max_polls
enabled: true
tags: [bug, timer, scheduler, zombie-task, to-fix]
created: 2026-04-20T05:30:23Z
updated: 2026-04-20T05:30:23Z
---

# Timer Watcher Zombie Bug — Skipped Polls Don't Count

## Symptom
Timer watchers from days/weeks ago still resume on every server restart and keep
polling, consuming LLM calls. Seen on 2026-04-20 with 3 zombies from 2026-04-10
(`tmr_3390573c`, `tmr_a085b915`, `tmr_577c595d`) each watching a `tail ...log`
command for a build/crawl that finished long ago. Users see mysterious `[Task ...]
429 rate-limited` lines in `app.log` seconds after server boot — unrelated to any
user action — because `routes/__init__.py::start_scheduler_worker()` calls
`resume_active_timers()` which re-spawns every timer with `status='active'`.

## Root cause

In `lib/scheduler/timer.py`:

1. `poll_timer()` has an early-exit optimization (lines ~520-535): if the
   `check_command` stdout is identical to the last poll's output, skip the LLM
   call and return `(False, '', 0, skipped=True)`.

2. `start_timer_loop._loop()` handles `skipped=True` with `continue` — **but
   does NOT call `_increment_poll_count()`** (lines ~660-665):
   ```python
   if skipped:
       logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)', ...)
       continue                # ← poll_count is NEVER incremented
   ```

3. Consequence: a timer whose `tail`'d log has stopped changing (because the
   build finished, or the process died) will skip every poll forever. Since
   `poll_count` stops advancing, the `max_polls` ceiling is never reached →
   status stays `'active'` → resumed on every restart.

Example: `tmr_3390573c` had poll_count=40 after ~10 days × 120 s interval
(~7200 theoretical polls). Only 40 were counted — the rest were silent skips.

## Fixes to apply (pick 1+2 at minimum)

### Fix 1 — Count skipped polls (minimal)

In `start_timer_loop._loop()`, move the increment before the skip continue, OR
add a dedicated skipped branch:

```python
if skipped:
    _increment_poll_count(tid, 'skipped', 'output unchanged')
    logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                 tid, poll_count + 1)
    continue
```

This alone guarantees every timer hits `max_polls` within a predictable wall
time and gets marked `exhausted`.

### Fix 2 — Startup sanity sweep

In `resume_active_timers()` (same file), before re-spawning any timer, auto-
cancel ones older than a threshold (24 h is reasonable, or
`poll_interval * max_polls * 1.5`):

```python
# Auto-expire ancient timers
db.execute(
    "UPDATE timer_watchers SET status='expired', updated_at=? "
    "WHERE status='active' AND created_at < ?",
    [now_iso, (datetime.now() - timedelta(hours=24)).isoformat()]
)
```

Adds a new `'expired'` status. Safe: expired timers were already failing to
make progress by definition.

### Fix 3 (nice-to-have) — Stalled-output cap

In `poll_timer()`, count consecutive skips in an in-memory map; if >N (e.g.
20), return a special `'stalled'` decision so the caller marks the timer
`status='stalled'` and stops.

## How to clear zombies now

```python
from lib.database import DOMAIN_SYSTEM, get_thread_db
from datetime import datetime
db = get_thread_db(DOMAIN_SYSTEM)
db.execute("UPDATE timer_watchers SET status='cancelled', updated_at=? WHERE status='active'",
           (datetime.now().isoformat(),))
db.commit()
```

(In-memory threads die on next restart because they re-check status from DB
before each poll.)

## Where the 429 confusion came from

The 429 in app.log was NOT from a zombie timer (timers use `capability='cheap'`
→ Qwen/Gemini/MiniMax). It was from concurrent SWE-bench eval tasks
(`conv_id=swebench-*`) POSTing to `/api/chat/start` via an external harness,
all requesting `aws.claude-opus-4.6-b` (alias of `aws.claude-opus-4.6`, which
has capabilities `['text','vision','thinking']` — NOT cheap). Do not confuse
the two when triaging similar reports.

