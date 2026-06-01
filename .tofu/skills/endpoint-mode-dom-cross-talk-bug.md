---
name: endpoint-mode-dom-cross-talk-bug
description: Bug fix: Endpoint mode events (endpoint_iteration, endpoint_critic_msg, endpoint_new_turn, endpoint_complete) perform DOM operations (getElementById, insertAdjacentHTML, buildTurnNav) without checking activeConvId === convId, causing cross-talk rendering into wrong conversation when user switches convs during background endpoint task
enabled: true
tags: [javascript, endpoint, dom, cross-talk, bug-fix, activeConvId, streaming]
created: 2026-03-23T08:22:48Z
updated: 2026-03-23T08:22:48Z
---

# Endpoint Mode DOM Cross-Talk Bug

## Problem
All four endpoint SSE event handlers in `ui.js` perform DOM operations using global element IDs (`streaming-msg`, `chatInner`, `ep-iter-banner`) without checking if the streaming conversation is the currently viewed conversation. This causes:

1. **Cross-talk**: Endpoint bubbles (worker + critic) render into the wrong conversation's DOM
2. **Ghost turns**: `buildTurnNav()` overwrites the turn nav with the background conv's turns, showing 🔍 critic dots for the wrong conversation
3. **Flicker on switch**: Switching back and forth eventually "fixes" it because `showStreamingUIForConv` re-renders correctly

## Root Cause
- `endpoint_iteration`, `endpoint_critic_msg`, `endpoint_new_turn`, `endpoint_complete` all use `document.getElementById("streaming-msg")` etc. without `activeConvId === convId` guard
- Normal `delta`/`state`/`tool_*` events are safe because they go through `twUpdate()` which has the guard
- But endpoint events bypass `twUpdate()` and directly manipulate the DOM

## Fix Pattern
For each endpoint event handler, split into:
1. **Data mutations** (unconditional): Update `assistantMsg`, `conv.messages`, `streamBufs`, `_epCriticMsg`, etc.
2. **DOM operations** (guarded): Wrap in `if (activeConvId === convId)` — `getElementById`, `insertAdjacentHTML`, `buildTurnNav`, `_forceScrollToBottom`

## Also Fixed
- `showStreamingUIForConv` now handles endpoint critic phase: if the last message is `_isEndpointReview && !done`, it creates a critic streaming bubble instead of an assistant one
- Uses `streamBufs.get(convId)` for current buffer state when switching to a mid-stream endpoint conv

