---
name: unified-task-runtime
description: All 5 task registries now backed by TaskRuntime; chat migrated last with tasks/tasks_lock alias compat
enabled: true
tags: [architecture, task-runtime, modularization, refactor]
created: 2026-05-22T09:54:49Z
updated: 2026-05-22T10:34:36Z
---

# Unified TaskRuntime (2026-05-22)

## What it is
`lib/task_runtime.py` — single source of truth for background async tasks.

## Migration status (5/5 ✅ COMPLETE)
1. ✅ `routes/trading_simulator.py::_tasks` — done
2. ✅ `routes/translate.py::_translate_tasks` — done
3. ✅ `routes/paper.py::_report_tasks` — done (with dedup index)
4. ✅ `routes/paper.py::_translate_tasks` — done (with dedup index)
5. ✅ `lib/tasks_pkg/manager.py` (chat) — **done 2026-05-22**

## Standard task shape
```python
{
    'id': str, 'kind': str,
    'status': 'pending' | 'running' | 'done' | 'error' | 'aborted',
    'events': [...], 'events_lock': Lock, 'abort_event': Event,
    'result': Any, 'error': dict | None,  # error envelope
    'created_at': float, 'finished_at': float | None,
    'meta': {...},  # caller-supplied custom fields
}
```

## Chat manager specifics (lib/tasks_pkg/manager.py)
- ``_chat_runtime = TaskRuntime('chat', ttl=3600, push_channel='chat')``
- ``tasks`` and ``tasks_lock`` module-level names ALIAS the runtime's
  internal storage so the 47 import sites (routes/, lib/, tests/) work
  without modification.
- ``create_task()`` calls ``_chat_runtime.create()`` then augments with
  21 chat-specific fields (convId, messages, config, content_lock,
  finishReason, lastUserQuery, _initial_msg_count, etc.). Chat tasks
  start with status='running' (override the runtime's 'pending' default).
- ``append_event()`` routes through ``_chat_runtime.append_event`` then
  layers chat-specific behaviour: phase tracking, persistent event_log
  for Last-Event-ID replay. Falls back to direct dict append for legacy
  test patterns that insert into ``tasks`` directly.
- ``cleanup_old_tasks()`` uses ``_chat_runtime.cleanup_stale()`` and also
  prunes the ``_conv_latest_task`` freshness-guard index.
- ``abort_running_tasks_for_conv`` and cross-talk-detection iteration both
  iterate ``tasks`` under ``tasks_lock`` — work unchanged via the alias.

## API
- `TaskRuntime(kind, ttl=3600, push_channel=, error_source=)`
- `runtime.create(meta=)` → task dict
- `runtime.append_event(task_id, event)` — auto-pushes via `lib.push`
  (tolerant of legacy dicts missing 'status' key)
- `runtime.finish(task_id, result=, error=)` — emits terminal event
- `runtime.abort(task_id)` — sets task['abort_event']
- `runtime.poll(task_id, cursor=N)` → standard shape
- `runtime.spawn(task_id, fn, *args)` — `asyncio.to_thread` if loop running
- `runtime.cleanup_stale()` / `.stats()` / `.list_running()`

## Migration recipe (battle-tested across 5 modules)
1. Replace local registry with `TaskRuntime(...)` instance.
2. Keep wrapper functions (`_create_task`, `_append_event`, etc.) and
   add module-level aliases (`tasks = _runtime._tasks`,
   `tasks_lock = _runtime._lock`) so existing call sites work unchanged.
3. For dedup-by-key registries (paper.py keys by (phash, lang) tuples):
   add a separate dedup index dict mapping the tuple → task_id.
4. Augment task dict with legacy field names after `runtime.create()`.
5. Workers that mutate `task['status']`/`task['error']` directly continue
   to work; do NOT also call `runtime.finish()` or you'll double-emit
   terminal events.
6. Cleanup must clear BOTH the runtime task AND any dedup-index entries.
7. Tests: `sys.modules['flask'] = quart` BEFORE importing routes.

## Tests (81 total, all passing)
- `tests/test_task_runtime.py` — 28 unit tests
- `tests/test_trading_simulator_migration.py` — 12
- `tests/test_translate_migration.py` — 10
- `tests/test_paper_migration.py` — 14 (report + translate)
- `tests/test_chat_manager_migration.py` — 17 (largest blast-radius test suite)

## Bug found and fixed during chat migration
`TaskRuntime.append_event` originally did `if task['status'] == 'pending'`
which crashed on legacy dicts inserted directly into ``_tasks`` without
a 'status' key. Hardened to `task.get('status') == 'pending'` so dicts
that bypass `runtime.create()` still get their events appended cleanly.

