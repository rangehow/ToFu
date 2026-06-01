---
name: endpoint-ghost-assistant-placeholder-bug
description: Bug fix: endpoint_complete ghost cleanup was too aggressive — removed ALL non-endpoint-marked assistants from entire history, destroying user↔assistant alternation for multi-turn conversations"
enabled: true
tags: [python, javascript, endpoint, ghost-message, race-condition, bug-fix, sync, placeholder]
created: 2026-03-29T03:38:52Z
updated: 2026-04-03T00:16:36Z
---

# Bug Fix: Endpoint mode ghost assistant cleanup destroys conversation history

## Problem
In endpoint mode, the `endpoint_complete` SSE handler had an overly aggressive ghost cleanup
that removed ALL assistant messages without endpoint markers (`_isEndpointPlanner`, `_epIteration`,
`_isEndpointReview`) from the ENTIRE conversation history. This destroyed legitimate historical
assistant messages from previous non-endpoint turns.

### Symptoms
- Debug panel shows consecutive user messages and consecutive assistant messages
- User↔assistant alternation broken in `buildApiMessages` output
- Only occurs when endpoint mode is used on a non-first turn (conversation already has history)
- DB data is correct (backend `_sync_endpoint_turns_to_conversation` uses safe trailing-only cleanup)
- Damage persists in IDB cache via `ConvCache.put(conv)` in `finishStream`

### Root Cause
The ghost cleanup in `endpoint_complete` handler (`ui.js`) scanned ALL messages:
```js
// ❌ OLD — removes ALL non-endpoint-marked assistants from entire history
for (let i = conv.messages.length - 1; i >= 0; i--) {
    if (m.role === "assistant" && !m._isEndpointPlanner && !m._epIteration && !m._isEndpointReview) {
        conv.messages.splice(i, 1);  // DESTROYS historical assistant messages!
    }
}
```

### Fix
Only scan messages AFTER the last base user message (the user message that triggered
the endpoint task). Ghost assistants from `startAssistantResponse()` can only appear
between this user message and the first endpoint-marked message (planner):
```js
// ✅ NEW — only scan after last base user message
let lastBaseUserIdx = -1;
for (let i = conv.messages.length - 1; i >= 0; i--) {
    if (conv.messages[i].role === 'user' && !conv.messages[i]._isEndpointReview) {
        lastBaseUserIdx = i;
        break;
    }
}
for (let i = conv.messages.length - 1; i > lastBaseUserIdx && i >= 0; i--) {
    // only remove ghosts in this scoped range
}
```

### Contrast with backend
The backend's `_sync_endpoint_turns_to_conversation` correctly uses a `while...pop()` loop
that only strips TRAILING ghost assistants from `base_messages`. This is safe.

## Files Changed
- `static/js/ui.js` — `endpoint_complete` handler ghost cleanup scoped to after last base user

