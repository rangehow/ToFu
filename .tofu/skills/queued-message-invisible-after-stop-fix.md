---
name: queued-message-invisible-after-stop-fix
description: Fix for queued message invisible/delayed after Stop: skip full-conv PUT race, extended retry, optimistic placeholder
enabled: true
tags: javascript, bug-fix, queue, stop-button, race-condition, finishStream, ui.js, main.js
created: 2026-05-05T06:47:11Z
updated: 2026-05-05T06:47:11Z
---

# Queued Message Invisible After Stop — Fix

## Problem
User sends msg A → generation starts → sends msg B → B gets queued.
User clicks Stop. Expectation: B fires immediately. Reality: "invisible" —
sometimes dead air for seconds, sometimes B's user bubble never appears
(the new assistant bubble shows up with no matching user prompt above it).

## Three Root Causes
1. **Slow-abort retry window too short**: `_checkForQueuedTask` in main.js
   retried at `[800, 1500, 3000]` ms = ~5.3s total. If `run_task` was
   mid-tool (long `run_command`, slow LLM abort), the queued message
   got dispatched later, but no one was polling anymore → orphaned.
2. **Race with `syncConversationToServer`**: `finishStream` always did a
   full-conv PUT, racing with backend's `dispatch_next_queued()` which
   appends the queued user_msg to the DB. If frontend PUT landed between
   backend's SELECT and INSERT, backend's write got clobbered → queued
   user msg vanished from DB → `loadConversationMessages` never picked it
   up → invisible queued message.
3. **No visual feedback**: no bubble/indicator during the 0.5–5s gap
   between stop and new stream attaching.

## Fix (2026-05-05)
### A. `static/js/main.js` `_checkForQueuedTask`
- Retry schedule expanded to `[300, 600, 1200, 2400, 4000, 6000]` ms (~15s total).
- On give-up (no queued items / no dispatched task after retries), remove
  the optimistic placeholder `#streaming-msg` so it doesn't stay orphaned.
- When new task IS found, remove the placeholder BEFORE
  `loadConversationMessages` + `renderChat` to prevent a stale ghost.

### B. `static/js/ui.js` `finishStream`
- When `pendingMessageQueue.has(convId) && length > 0`, skip
  `syncConversationToServer(conv)` entirely. Backend owns the next DB
  write via `dispatch_next_queued()`. The backend's
  `_sync_result_to_conversation` already persisted the aborted assistant
  state for us.
- `ConvCache.put(conv)` still runs so IDB cache stays fresh.

### C. `static/js/ui.js` `finishStream` (tail)
- Insert optimistic `_streamingBubbleHTML('worker', 'Dispatching queued message…')`
  when queue has items.
- Change retry delay from fixed `500ms` to `0ms` when queue has items
  (dispatch check fires immediately; keeps 500ms for normal stream end).

## Key Files
- `static/js/ui.js` — `finishStream` (two edits: PUT guard + placeholder + immediate kick)
- `static/js/main.js` — `_checkForQueuedTask` (retry schedule + placeholder cleanup)

## Why Not Touch Backend?
Backend logic (`lib/message_queue.py`, `lib/tasks_pkg/manager.py
._dispatch_queued_message`) is already correct: it dispatches even on abort,
appends user_msg, starts run_task. The invisibility was purely a frontend
race + UX gap.

