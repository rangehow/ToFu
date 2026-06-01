---
name: sse-premature-close-poll-fallback
description: SSE timeout and premature close handling: backend sends sse_timeout event (not done), frontend shows toast and switches to _pollFallback; backend task keeps running and result is picked up via poll; also covers _sync_result_to_conversation() for cross-session recovery
enabled: true
tags: [python, javascript, sse, streaming, debugging, frontend, backend, task-result, conversation-sync]
created: 2026-03-16T08:21:26Z
updated: 2026-03-22T06:45:02Z
---

# SSE Timeout & Premature Close → Poll Fallback

## Problem
SSE connections have a 2-hour hard limit (`_MAX_SSE_DURATION = 7200s`). Long-running tasks (many tool rounds) can exceed this. Previously, the backend would abort the task and send a fake `done` event, killing useful work.

## Architecture (Correct Approach)

### Backend (`routes/chat.py`)
When SSE hits the time limit:
1. Send `{type: 'sse_timeout', message: '...'}` — **NOT** a `done` event
2. Do **NOT** set `task['aborted'] = True` — the task should keep running
3. Close the SSE generator with `return`

```python
if _elapsed > _MAX_SSE_DURATION:
    timeout_notice = {'type': 'sse_timeout',
                      'message': 'SSE connection reached maximum duration. Switching to polling.'}
    yield f'data: {json.dumps(timeout_notice)}\n\n'
    return  # close SSE, task keeps running
```

### Frontend (`static/js/ui.js`)

#### `_processSSELine` — handle `sse_timeout` event
- Show a toast notification to the user
- Return `false` (not done) so `streamDone` remains false
- When `_trySSE` sees `!streamDone` after the for-loop, it returns `false`
- `connectToTask` sees `sseWorked = false` → calls `_pollFallback`

#### `_pollFallback` — polls `/api/chat/poll/<taskId>` every 500ms
- Updates `assistantMsg.content`, `.thinking`, `.searchRounds` from poll response
- Continues until `data.status !== 'running'` (task finished)
- Has its own 2hr budget (14400 iterations × 500ms) for total 4hr coverage (SSE + poll)

### Backend Result Persistence (`_sync_result_to_conversation`)
When the task finishes, `persist_task_result()` writes to `task_results` table AND `_sync_result_to_conversation()` writes the result directly into the conversation's messages JSON in the DB. This ensures the result is available even if:
- The SSE closed and poll wasn't running
- The browser was closed entirely
- The user refreshes the page in a new session

### `activeTaskId` in Settings
The frontend persists `conv.activeTaskId` in localStorage/settings. On page load, if a conversation has an active task, it reconnects via SSE/poll automatically.

## Key Principle
**Never abort a running task just because the SSE transport closed.** The task is still doing useful work. Let it finish, and use polling to retrieve the result.

