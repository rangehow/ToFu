---
name: regen-needsLoad-stale-message-resurrection-bug
description: Bug fix: regenerateFromUser/saveEditAndResend truncation undone by _needsLoad DB reload — old assistant messages resurrected because startAssistantResponse calls loadConversationMessages when _needsLoad=true, overwriting the in-memory truncation with stale server data. Also caused secondary HTTP 400 'assistant message prefill' error.
enabled: true
tags: [javascript, bug-fix, regen, needsLoad, race-condition, message-resurrection, truncation, syncConversationToServer]
created: 2026-03-30T09:28:58Z
updated: 2026-03-30T09:28:58Z
---

# Regen _needsLoad Stale Message Resurrection Bug

## Root Cause
When user clicks Regenerate or Edit+Resend:
1. `regenerateFromUser` / `saveEditAndResend` truncates `conv.messages` in-memory
2. But `conv._needsLoad` may still be `true` (set by `loadConversationsFromServer` during tab focus/reconnect)
3. `startAssistantResponse` checks `if (conv._needsLoad)` → calls `loadConversationMessages`
4. `loadConversationMessages` fetches from DB → **overwrites the truncated messages with stale data** containing the old assistant response
5. Empty assistant is pushed → conversation now has `[user, OLD_assistant, new_empty_assistant]` = 3 msgs
6. Secondary bug: `buildApiMessages` does `slice(0, -1)` → sends `[user, OLD_assistant]` → API rejects with "must end with user message"

## Two-Part Fix
1. **Clear `_needsLoad` and reset `_serverMsgCount`** immediately after truncation/pop:
   ```javascript
   conv.messages = conv.messages.slice(0, idx + 1);
   conv._needsLoad = false;
   conv._serverMsgCount = conv.messages.length;
   ```
   - `_needsLoad = false` prevents reload from DB
   - `_serverMsgCount` reset prevents `syncConversationToServer` guard (`local < server → SKIP`) from blocking the sync

2. **Force `await syncConversationToServer(conv)`** BEFORE `startAssistantResponse`:
   - Persists truncated messages to DB
   - Closes race window where backend's `_sync_result_to_conversation` from a previous task could read stale messages

## Affected Functions
- `regenerateFromUser()` in `main.js`
- `saveEditAndResend()` in `ui.js`
- `continueAssistant()` in `main.js` (two pop paths)

## Evidence (conv mncyfgckbfwsr2)
- Task 73994672: aborted at 16:57:59, synced `[user, assistant_291chars]` to DB
- User edited → saveEditAndResend truncated to `[edited_user]` in memory
- GET at 16:59:46 (from `loadConversationMessages` due to `_needsLoad=true`) reloaded stale `[user, old_assistant]`
- Task 33e6bcff: started with 3 msgs, immediately HTTP 400 "assistant message prefill"

