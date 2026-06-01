---
name: loadConversationMessages-phase2-overwrite-race-condition
description: Bug fix: loadConversationMessages Phase 2 server response overwrites conv.messages during startAssistantResponse race — cacheHit flag bypassed activeTaskId guard, causing connectToTask to see user message as last msg → no SSE → sidebar dot but no Agent icon; also Case E orphan detection races with user sendMessage on page refresh
enabled: true
tags: javascript,race-condition,bug-fix,loadConversationMessages,connectToTask,phase2,overwrite,cacheHit,Case-E,orphan,streaming,SSE
created: 2026-03-31T08:35:30Z
updated: 2026-03-31T08:35:30Z
---

# loadConversationMessages Phase 2 Overwrite Race Condition

## Bug Pattern
After page refresh, user quickly clicks a conversation and sends a message. The sidebar shows a purple pulsing dot (task exists) but the chat area shows no Agent icon (no streaming UI).

## Root Cause
Three interacting race conditions:

### Race 1: Phase 2 Overwrite
`loadConversation()` fires `loadConversationMessages()` as fire-and-forget (`.then()`, no `await`). `sendMessage()` → `startAssistantResponse()` pushes an empty assistant message to `conv.messages` and `await fetch(POST /api/chat/start)`. During this `await`, the Phase 2 `GET /api/conversations/:id` response arrives and overwrites `conv.messages` with old server data (no assistant msg at end).

The overwrite condition was:
```js
} else if (!hasLocalData || cacheHit || (!isStreaming && !conv.activeTaskId)) {
```
The `cacheHit` flag (from Phase 1 IndexedDB cache hit) **bypassed** the `activeTaskId` guard, so even if `conv.activeTaskId` was set, the overwrite still happened.

After overwrite, `connectToTask()` reads `conv.messages[last]` → user message → `if (assistantMsg.role !== "assistant") return` → **bails out silently**. No SSE connection made.

### Race 2: Case E Orphan False Positive
`initActiveTasks` Case E detects orphaned user messages (last msg is user, no active task) from metadata. If the user's last action before page refresh was sending a message whose task already completed, Case E falsely detects it as an orphan and fires `startAssistantResponse()` — racing with the user's own `sendMessage()`.

### Race 3: Sidebar vs Chat Area Inconsistency
`conv.activeTaskId` is set by `startAssistantResponse()` after POST returns, making the sidebar show a pulsing dot. But `connectToTask()` bailed out, so `activeStreams` doesn't have the convId → no streaming UI rendered.

## Fix (3 parts)

### 1. Remove `cacheHit` bypass in Phase 2 overwrite guard (core.js)
```js
// Before (buggy):
} else if (!hasLocalData || cacheHit || (!isStreaming && !conv.activeTaskId)) {

// After (fixed):
} else if (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId)) {
```

### 2. Defensive recovery in connectToTask (ui.js)
Instead of silently returning when last msg isn't assistant, push a recovery assistant message:
```js
if (!assistantMsg || assistantMsg.role !== "assistant") {
    console.warn(`[connectToTask] Last msg is ${assistantMsg?.role}, pushing recovery...`);
    assistantMsg = { role: "assistant", content: "", thinking: "", ... };
    conv.messages.push(assistantMsg);
}
```

### 3. Delay Case E orphan recovery by 3s (main.js)
```js
setTimeout(() => {
    for (const conv of caseEConvs) {
        if (conv.activeTaskId || activeStreams.has(conv.id)) continue; // user already started
        startAssistantResponse(conv.id);
    }
}, 3000);
```

## Symptom Fingerprint
- Sidebar: purple pulsing dot ✓
- Chat area: no Agent icon, no streaming UI ✗
- Backend task: running normally, accumulating content
- Server logs: no `GET /api/chat/stream/:taskId` request
- Usually triggered by: page refresh → quick click on conv → send message

