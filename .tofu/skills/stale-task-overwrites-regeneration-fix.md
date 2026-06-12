---
name: stale-task-overwrites-regeneration-fix
description: Fix for stale task's _sync_result_to_conversation overwriting new regenerated task's content on page refresh
enabled: true
tags: [bug-fix, race-condition, regenerate, task-lifecycle, stale-data, sync, critical]
created: 2026-04-14T17:16:12Z
updated: 2026-06-01T05:44:41Z
---

# Stale Task Overwrites Regeneration — Root Cause & Fix

## Problem
When user clicks Stop → Edit → Regenerate:
1. Old task thread is still running (abort is cooperative)
2. New task starts for the same conversation
3. Old task finishes → `_sync_result_to_conversation()` overwrites DB with stale content
4. Page refresh → loads stale content from DB instead of new regeneration

## Three-Pronged Fix
1. `abort_running_tasks_for_conv(conv_id, exclude_task_id=None)` force-aborts
   running tasks for a conv. Called from chat_start / _start_task_for_conv /
   autopilot _start_followup_task. Stamps `_abort_reason='superseded_by_new_task'`.
2. `_conv_latest_task` registry (conv_id → latest task_id), updated in create_task().
   `_sync_result_to_conversation` + `_sync_partial_to_conversation` reject writes
   from a task that isn't latest.
3. Backend-initiated abort BEFORE creating the new task.

## Diagnosing which path triggered the guard (READ THE LOG FIRST)
The freshness-guard WARNING is a SYMPTOM, not the cause. Always grep app.log
for the superseding task id to find WHO created the second task:
`grep "<newtaskid>" logs/app.log | grep -iE "Created|Autopilot|Queue|Spawning"`.
There are THREE distinct root scenarios, distinguished by `abort_reason` + creator:

- **abort_reason='superseded_by_new_task'** → real Stop→Regenerate / new send.
  Expected, the abort sweep caught it. DEBUG level.
- **abort_reason='' + `[Autopilot] Spawning follow-up task X (parent=Y)`** →
  AUTOPILOT. The parent task's own end-of-turn hook (`maybe_run_autopilot` in
  lib/tasks_pkg/autopilot.py, fires inside `_finalize_and_emit_done` BEFORE
  persist) ran a virtual-user turn and spawned a follow-up, which became
  `latest`. Then the parent's own persist/sync ran and saw it was superseded —
  by its OWN child. NOT aborted, NOT a leak, NOT a bug. The extra tokens are
  autopilot working as designed; control is the autopilot toggle. The parent
  sets `task['_autopilot_spawned_followup']=<child_id>` — use THAT to classify
  this as expected (DEBUG). No data loss: the follow-up rebuilds messages from
  the DB/message-store including the parent's answer (log: `frontend=N → stored=N+1`).
- **abort_reason='' + creator is NOT autopilot** → genuinely unexpected: a task
  that was never aborted is no longer latest. Possible missing abort path
  (scheduler `inject_and_run_task`/`create_agentic_task` in lib/scheduler/_shared.py
  and queue `dispatch_next_queued` in lib/message_queue.py do NOT call
  abort_running_tasks_for_conv). WARNING level — worth investigating.

## Pending-task TOCTOU window (2026-06-01, separate fix)
create_task() registers the task as status='pending' (via _chat_runtime.create())
THEN flips to 'running'. The abort sweep originally filtered status=='running'
only, so a task caught mid-creation was skipped. Fix: sweep now matches
`status in ('running','pending')`.

## Log-level classification in _sync_result_to_conversation (current)
```
if aborted or _abort_reason: debug   # Stop→Regenerate, expected
elif _autopilot_spawned_followup: debug   # autopilot parent superseded by own child, expected
else: warning   # never-aborted + not-autopilot → unexpected, possible missing abort path
```

## Files Changed
- `lib/tasks_pkg/manager.py`: abort_running_tasks_for_conv() (catches pending too),
  _conv_latest_task, freshness guards (3-way debug/debug/warning classification
  incl. `_autopilot_spawned_followup`)
- `lib/tasks_pkg/__init__.py`: export abort_running_tasks_for_conv
- `routes/chat.py`: call abort before starting tasks

