---
name: timer-watcher-state-reconnection-fix
description: Fix: Timer watcher poll data lost on page refresh — backend now attaches _timerPolls to searchRounds for state snapshot, frontend deduplicates and recovers from API
enabled: true
tags: [timer, sse, reconnection, bug-fix, streaming]
created: 2026-04-09T16:26:06Z
updated: 2026-04-09T16:26:06Z
---

# Timer Watcher State Reconnection Fix

## Problem
When a user refreshes the page while a `timer_create` tool is actively polling:
1. The state snapshot sent via SSE had `searchRounds` but no `_timerPolls` data
2. The timer_create round showed "initializing…" forever because `_timerPolls` was empty
3. The surgical UI update path didn't handle the transition from "no polls" → "has polls"
4. Orphaned timers from server restarts left conversations stuck

## Root Cause
`_timerPolls` was only accumulated on the frontend from individual `timer_poll_check` SSE events.
The backend never attached poll data to the `task['searchRounds']` entries, so state snapshots
and DB persistence excluded it.

## Changes

### Backend (`lib/scheduler/executor.py`)
- Added `_attach_poll_to_round()` helper that appends each poll entry to the corresponding 
  searchRound in `task['searchRounds']`
- Called after every poll event (started, wait, ready, error)
- Also sets `_timerTimerId` and `_timerTriggered` on the round
- State snapshots now include full timer poll history

### Frontend (`static/js/ui.js`)
- Added `_recoverTimerPolls(round)` async function that fetches poll log from 
  `/api/timer/{id}/status` API when `_timerPolls` is missing
- Added dedup in `timer_poll_check` handler: skips polls already present from state snapshot
- Fixed surgical update path: `round._timerPolls && round._timerPolls.length > 0` instead of
  `round._timerPolls && slot.querySelector('.timer-watcher-block')` — handles initializing→active transition
- Added `_timerOrphaned` display state for timer rounds from dead tasks
- Changed "initializing…" label to "waiting for first poll…"

### Frontend (`static/js/main.js`)
- Case B recovery: cleans up orphaned `timer_create` rounds (marks status='done', tries API recovery)

## Key Architecture
- Normal flow: timer_create blocks the task thread, emits SSE events per poll
- Server restart: `resume_active_timers()` starts independent background poll threads
- State snapshot: `task['searchRounds']` now includes `_timerPolls` array
- DB persistence: `_timerPolls` serialized via `json.dumps(task['searchRounds'])`

