---
name: translateThenRespond-missing-allowTruncate-edit-regen-bug
description: Bug fix: _translateThenRespond called without allowTruncate from saveEditAndResend/regenerateFromUser — server blocks truncated sync, edited messages never persist, old messages reappear on reload
enabled: true
tags: [frontend, race-condition, data-loss, sync, translate, edit]
created: 2026-04-06T15:44:50Z
updated: 2026-04-06T15:44:50Z
---

# _translateThenRespond Missing allowTruncate — Edit/Regen Data Loss

## Symptom
User edits a message (edit the second-to-last conversation turn), clicks "Save & Regenerate".
The edit works locally, new assistant response streams in. But "after a while" (on page reload,
conversation switch, or when `loadConversationMessages` fetches from server), the old pre-edit
messages reappear and overwrite the edited version.

## Root Cause
`saveEditAndResend()` and `regenerateFromUser()` truncate `conv.messages` before calling
`_translateThenRespond()`. But `_translateThenRespond()` called `syncConversationToServer(conv)`
**without `{ allowTruncate: true }`**.

The server-side guard in `routes/conversations.py` `save_conv()` rejects PUTs where
`msg_count < existing_count` unless `allowTruncate=true`. So the truncated edit NEVER
reaches the server → server keeps old messages → page reload fetches old data.

## Log Forensics
Repeating pattern of 409 rejections:
```
[save_conv] ⚠️ BLOCKED regression of conv mnnbl4... — server has 10 msgs but client sent 7
[save_conv] ⚠️ BLOCKED regression of conv mnnbl4... — server has 10 msgs but client sent 7
[save_conv] ⚠️ BLOCKED regression of conv mnnbl4... — server has 10 msgs but client sent 8
```
The client sends 7 (truncated), then 8 (truncated + empty assistant from startAssistantResponse).
All rejected. Server-side `_sync_result_to_conversation` writes new content into old 10 messages.

## Fix
1. Added `{ allowTruncate = false }` options parameter to `_translateThenRespond()`
2. `saveEditAndResend()` passes `{ allowTruncate: true }` (ui.js)
3. `regenerateFromUser()` passes `{ allowTruncate: true }` (main.js)
4. The initial sync in `_translateThenRespond` uses `allowTruncate` option
5. Non-truncating callers (`sendMessage`, `_dispatchQueuedMessage`) use default `false`

## Affected Files
- `static/js/main.js` — `_translateThenRespond` signature + `regenerateFromUser` call site
- `static/js/ui.js` — `saveEditAndResend` call site

