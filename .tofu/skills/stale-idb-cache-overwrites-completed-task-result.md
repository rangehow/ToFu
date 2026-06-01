---
name: stale-idb-cache-overwrites-completed-task-result
description: Bug fix: VS Code page reload during done event causes stale IDB cache to overwrite completed task result — added save_conv guard and 409 recovery
enabled: true
tags: [bug-fix, race-condition, idb-cache, sse, vscode]
created: 2026-04-09T19:27:08Z
updated: 2026-04-09T19:27:08Z
---

# Stale IDB Cache Overwrites Completed Task Result

## Bug Pattern
VS Code port forwarding periodically reloads the page. If this coincides with the task's `done` SSE event:
1. `finishStream()` starts but `syncConversationToServer()` fetch is aborted by page unload
2. `ConvCache.put()` may not complete (async IDB write interrupted)
3. IDB cache retains the last streaming checkpoint (partial content, no finishReason/usage)
4. Backend `_sync_result_to_conversation` writes complete data to DB (42691 chars, finishReason=stop)
5. Backend also clears `settings.activeTaskId` → Case B recovery won't trigger on reload
6. Page reloads → IDB cache serves stale data → frontend PUTs stale data back to server → overwrites complete result

## Symptoms
- Conversation shows model tag but no finish bar (no finishReason/usage)
- Content is truncated to the last streaming checkpoint size
- Translation may be based on truncated content

## Fix Applied
1. **Server-side guard** (`routes/conversations.py`): `save_conv` checks if the server has a completed assistant message (finishReason set) but client sends one without finishReason AND with less content → returns 409 `blocked_stale_checkpoint`
2. **Frontend recovery** (`static/js/core.js`): on 409 `blocked_stale_checkpoint`, auto-reload from server and update IDB cache + re-render
3. **Data repair**: recover from `task_results` table which always has complete data

## Key Insight
The `msg_count` guard was insufficient — same message count doesn't prevent content regression within messages. Need content-level check.

## Recovery Script (manual DB repair)
```python
# Get task result data
cur.execute("SELECT content, thinking, metadata, search_rounds FROM task_results WHERE task_id = ?", (task_id,))
# Merge into conversation messages[-1]
# Delete stale translatedContent
```

