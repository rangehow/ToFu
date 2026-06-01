---
name: sync-mechanism-review-emit-content-poll-bug
description: Bug fix: _emitContent/_emitToolName missing from poll fallback and Case B recovery paths
enabled: true
tags: [bug-fix, sync, poll-fallback, emit-to-user, sse, streaming]
created: 2026-04-14T15:30:43Z
updated: 2026-04-14T15:30:43Z
---

# Sync Review: emit_to_user Data Loss on Poll Fallback

## Bug
`_emitContent` and `_emitToolName` (from `emit_to_user` tool) were:
- ✅ Sent via SSE `emit_ref` event
- ✅ Persisted to conversation by `_sync_result_to_conversation` (manager.py)
- ❌ **Missing** from `chat_poll` response (routes/chat.py) — both in-memory and DB paths
- ❌ **Missing** from `_pollFallback` handler (ui.js)
- ❌ **Missing** from `initActiveTasks` Case B recovery (main.js)

## Fix Applied
1. `routes/chat.py` `chat_poll`: Added `emitContent`/`emitToolName` to in-memory poll response
2. `static/js/ui.js` `_pollFallback`: Handle `data.emitContent`/`data.emitToolName`
3. `static/js/main.js` Case B: Handle `td.emitContent`/`td.emitToolName`

## Note on DB-sourced polls
When the task has been evicted from memory (DB-only poll), `_emitContent` is NOT in the task_results metadata — it's persisted directly into conversation messages. The frontend recovers it from `loadConversationMessages` on page load. This is by design.

## Other Findings
- `_sync_partial_to_conversation` lacks optimistic locking (minor race window)
- `_mergeFromStorage` and `_writeToLocalStorage` are dead no-ops (cleaned up)
- Endpoint mode `_pollFallback` reassigns `assistantMsg` but subsequent metadata updates run unconditionally (minor)

