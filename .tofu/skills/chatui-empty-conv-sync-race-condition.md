---
name: chatui-empty-conv-sync-race-condition
description: Bug fix: sendMessage() creates conv with messages:[] then _saveConvToolState() syncs empty messages to server before user msg is pushed, causing conv to flicker/disappear on reload
enabled: true
tags: [javascript, python, debugging, race-condition, sync, conversation, frontend]
created: 2026-03-19T05:39:22Z
updated: 2026-03-19T05:39:22Z
---

# Empty Conv Sync Race Condition

## Bug Pattern
In `sendMessage()`, a new conversation is created with `messages: []`, then `_saveConvToolState()` is called immediately which triggers `syncConversationToServer(conv)` — this sends `messages: []` to the server BEFORE the user message is pushed into the array.

### Consequence Chain
1. Server stores `messages: []` (msg_count: 0)
2. On page reload, `loadConversationsFromServer()` returns `messageCount: 0`
3. `loadConversationMessages()` returns empty → clears `_needsLoad`
4. `_purgeEmptyConvs()` sees: `messages.length==0, _serverMsgCount==0, _needsLoad==false` → PURGES
5. Next server fetch brings it back → sidebar flickers

### Fix (3-layer defense)
1. **`_saveConvToolState()`**: Skip `syncConversationToServer` when `conv.messages.length === 0`
2. **`syncConversationToServer()`**: Guard at entry — never sync conv with 0 messages
3. **Server `save_conv()` endpoint**: Reject PUT with `msg_count=0` if conv already has messages (409 Conflict)

### Key Files
- `static/js/main.js` — `_saveConvToolState()`, `sendMessage()`, `_purgeEmptyConvs()`
- `static/js/core.js` — `syncConversationToServer()`, `loadConversationMessages()`
- `routes/common.py` — `save_conv()` PUT endpoint

