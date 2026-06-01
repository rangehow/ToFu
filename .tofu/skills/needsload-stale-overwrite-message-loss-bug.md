---
name: needsLoad-stale-overwrite-message-loss-bug
description: Bug fix: _needsLoad flag not cleared after local message push → loadConversationsFromServer() on reconnect overwrites unsaved messages with stale server data, causing permanent message loss in DB-first architecture (no localStorage fallback)
enabled: true
tags: [javascript, frontend, bug-fix, _needsLoad, message-loss, server-restart, DB-first, stale-overwrite, race-condition]
created: 2026-03-25T03:36:21Z
updated: 2026-03-25T03:36:21Z
---

# _needsLoad Stale Overwrite Bug — Message Loss on Server Restart

## Scenario
1. Server goes down
2. User sends message in frontend → `loadConversationMessages()` fails, `_needsLoad` stays `true`
3. Message pushed to `conv.messages` in JS memory (only copy — no localStorage in DB-first arch)
4. `startAssistantResponse()` fails → `syncConversationToServer()` also fails
5. Server restarts → `loadConversationsFromServer()` sees `_needsLoad=true`
6. Calls `loadConversationMessages()` → gets OLD server data → **overwrites** local message
7. Message permanently lost

## Root Cause
`_needsLoad` is only cleared inside `loadConversationMessages()` on successful server response.
When `loadConversationMessages()` fails (server down), the flag stays `true`.
Later, `loadConversationsFromServer()` uses this flag to trigger a reload that overwrites local mutations.

## Fix (Two Layers)

### Layer 1: Clear `_needsLoad` before local mutation (main.js)
```javascript
// Before conv.messages.push(userMsg):
conv._needsLoad = false;
conv.messages.push(userMsg);
```

### Layer 2: Timestamp guard in loadConversationMessages (core.js)
```javascript
const localNewest = hasLocalData ? Math.max(...conv.messages.map(m => m.timestamp || 0)) : 0;
const serverNewest = serverMsgs.length > 0 ? Math.max(...serverMsgs.map(m => m.timestamp || 0)) : 0;
const localHasUnsynced = hasLocalData && localNewest > serverNewest;
if (localHasUnsynced) {
  // Keep local, re-sync to server
  syncConversationToServer(conv);
} else if (!hasLocalData || (!isStreaming && !conv.activeTaskId)) {
  conv.messages = serverMsgs;  // safe to overwrite
}
```

## Key Insight
In a DB-first architecture with no localStorage fallback, the in-memory array is the ONLY copy.
Any code path that overwrites it must check for unsaved local mutations first.

