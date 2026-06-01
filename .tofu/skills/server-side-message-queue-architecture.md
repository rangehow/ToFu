---
name: server-side-message-queue-architecture
description: Server-side message queue: persists queued messages in DB, auto-dispatches after task completion, survives page refresh
enabled: true
tags: [architecture, queue, backend, frontend, persistence]
created: 2026-04-11T16:39:38Z
updated: 2026-04-17T03:47:54Z
---

# Server-Side Message Queue Architecture

## Problem
Frontend-only `pendingMessageQueue` (JS Map) was lost on page refresh.
Also, frontend making queue-vs-send decisions led to bugs when its state diverged from backend.

## Solution (Redesigned 2026-04-17)
**Backend-driven queue decision**: Frontend always POSTs to `/api/chat/send`.
Backend checks if a task is running → either starts immediately or enqueues.
Frontend never decides whether to queue or send.

**Key change (2026-04-17)**: Queued messages are NOT persisted to the conversation DB.
They only live in the `message_queue` table. The user message is appended to the
conversation only when `dispatch_next_queued()` runs (after the current task finishes).
This prevents queued messages from appearing in chatInner during streaming and
from disappearing on page refresh.

## Key Files
- `lib/message_queue.py` — Queue CRUD, dispatch logic, auto-translate
- `lib/tasks_pkg/manager.py` — `_dispatch_queued_message()` hook in `persist_task_result()`
- `routes/chat.py` — `/api/chat/send` (auto-queues), GET/DELETE `/api/chat/queue`
- `lib/database/_schema.py` — `message_queue` table definition
- `static/js/main.js` — Frontend: `sendMessage()` always POSTs to `/api/chat/send`, handles `{queued: true}` response. `_checkForQueuedTask()` discovers dispatched tasks. `_refreshServerQueue()` syncs UI state.
- `static/js/ui.js` — `finishStream()` always calls `_checkForQueuedTask()` (no frontend gate)

## DB Table
```sql
CREATE TABLE message_queue (
    id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',   -- JSON: text, images, pdfTexts, _user_msg, etc.
    config TEXT NOT NULL DEFAULT '{}',    -- JSON: model, tools, etc.
    position INTEGER NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL
)
```

## Flow (Redesigned 2026-04-17)
1. Frontend `sendMessage()` → POST `/api/chat/send` (always, no queue/send decision)
2. Frontend optimistically renders user message in chatInner
3. Backend checks `tasks` dict for running task on this conversation:
   - **No task** (or all running tasks are `aborted`): persist user msg → start task → return `{taskId}`
   - **Non-aborted task running**: enqueue to `message_queue` (with `_user_msg` pre-built) → return `{queued: true, queueId, position}`
4. Frontend handles `{queued: true}`:
   - **Removes** the optimistic user message from `conv.messages` and DOM
   - Calls `_refreshServerQueue()` → shows queue bar UI
5. Task completes → `persist_task_result` → `_dispatch_queued_message(task)`
6. `dispatch_next_queued()`: dequeue → append pre-built user_msg to conversation DB → build API msgs → create_task → run_task
7. Frontend `finishStream()` → `_checkForQueuedTask()` (always, unconditionally) → polls `/api/chat/active` → discovers new task → `connectToTask()`
8. On page load: `_refreshServerQueue(activeConvId)` restores queue bar from server

## `_user_msg` in Queue Payload
When `/api/chat/send` enqueues a message, it stores the already-built (and translated)
`user_msg` dict in the queue payload as `_user_msg`. When `dispatch_next_queued()` runs,
it uses this pre-built message directly — no need to re-translate.

## Stop→Send Race Fix
**Problem**: User clicks Stop then immediately sends new message. Old task has `status='running'`
but `aborted=True`. Backend's `has_running_task` check now excludes aborted tasks.

**Fix (3 layers)**:
1. **Backend**: `has_running_task` excludes `aborted` tasks
2. **Frontend→Backend race**: Stop records `conv._lastAbortedTaskId`, passed as `abortTaskId` in send
3. **`/api/chat/active`**: Includes `aborted` flag, `_checkForQueuedTask()` skips aborted
4. **`_checkForQueuedTask` guard**: Skips if conv already has `activeTaskId` or active stream

## Queue Cancel
Since queued messages are NOT in the conversation DB, cancelling simply deletes from
`message_queue` table. No need to touch the conversation messages.

## Abort + Queue Dispatch
Dispatch runs even after abort. Only errors skip dispatch.

