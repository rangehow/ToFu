---
name: translation-abort-race-condition
description: Client-side fetch abort during translation doesn't stop server-side processing; need abort-by-convId
enabled: true
tags: [bug-fix, translation, abort, race-condition, frontend, backend]
created: 2026-04-17T03:27:38Z
updated: 2026-04-17T03:27:38Z
---

# Translation Abort Race Condition

## Bug Pattern
When user aborts during auto-translation (clicks Stop while "translating" shows):
1. Frontend's `AbortController.abort()` only cancels reading the HTTP response
2. Server has already received the full POST body and may have:
   - Completed translation
   - Persisted the user message to DB  
   - Started a background task
3. Frontend never receives the `taskId`, so can't call `/api/chat/abort/<taskId>`

## Fix (April 2026)
1. **Keep user message on abort** — don't pop from `conv.messages` or remove from DOM
   - User wants to edit a typo, not delete the message
2. **New endpoint**: `POST /api/chat/abort-conv/<conv_id>` — aborts all running tasks by convId
   - Uses existing `abort_running_tasks_for_conv()` from `lib/tasks_pkg/manager.py`
3. **Sync to server** after abort — `syncConversationToServer(conv)` to overwrite any 
   server-persisted translated version
4. Both `sendMessage` and `saveEditAndResend` abort handlers updated

## Files Changed
- `routes/chat.py` — new `/api/chat/abort-conv/<conv_id>` endpoint
- `static/js/main.js` — `sendMessage` catch block keeps message + calls abort-conv
- `static/js/ui.js` — `saveEditAndResend` catch block calls abort-conv + server sync

