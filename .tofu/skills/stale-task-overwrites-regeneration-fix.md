---
name: stale-task-overwrites-regeneration-fix
description: Fix for stale task's _sync_result_to_conversation overwriting new regenerated task's content on page refresh
enabled: true
tags: [bug-fix, race-condition, regenerate, task-lifecycle, stale-data, sync, critical]
created: 2026-04-14T17:16:12Z
updated: 2026-04-14T17:16:12Z
---

# Stale Task Overwrites Regeneration — Root Cause & Fix

## Problem
When user clicks Stop → Edit → Regenerate:
1. Old task thread is still running (abort is cooperative)
2. New task starts for the same conversation
3. Old task finishes → `_sync_result_to_conversation()` overwrites DB with stale content
4. Page refresh → loads stale content from DB instead of new regeneration

## Three-Pronged Fix

### 1. `abort_running_tasks_for_conv(conv_id, exclude_task_id=None)`
New function in `lib/tasks_pkg/manager.py` that force-aborts ALL running tasks
for a conversation. Called from `_start_task_for_conv()` and `chat_start()`.

### 2. `_conv_latest_task` freshness registry
Module-level dict mapping `conv_id → latest_task_id`. Updated on every 
`create_task()`. Both `_sync_result_to_conversation()` and 
`_sync_partial_to_conversation()` check this — if the task isn't the latest,
its writes are rejected with a warning log.

### 3. Backend-initiated abort before new task
`_start_task_for_conv()` calls `abort_running_tasks_for_conv(conv_id)` BEFORE
creating the new task. This ensures the old task's background thread gets the
abort signal as soon as possible.

## Key Insight
The frontend *does* send `/api/chat/abort`, but the abort is cooperative —
the task checks `task['aborted']` only at specific checkpoints (loop start,
before tool execution, etc.). While the task is mid-LLM-stream or mid-tool,
it keeps running. The freshness guard is the safety net that prevents data
corruption even if the abort takes time to propagate.

## Files Changed
- `lib/tasks_pkg/manager.py`: abort_running_tasks_for_conv(), _conv_latest_task, freshness guards
- `lib/tasks_pkg/__init__.py`: export new function
- `routes/chat.py`: call abort before starting tasks

