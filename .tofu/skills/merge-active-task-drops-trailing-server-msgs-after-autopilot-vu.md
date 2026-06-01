---
name: merge-active-task-drops-trailing-server-msgs-after-autopilot-vu
description: Bug fix: MERGE_ACTIVE_TASK reconcile branch silently dropped autopilot VU + follow-up messages because it only merged overlapping indices and never appended server tail
enabled: true
tags: [frontend, autopilot, idb-cache, loadConvMsgs, reconcile]
created: 2026-05-16T12:39:08Z
updated: 2026-05-16T12:39:08Z
---

# Bug: autopilot VU user message disappears from chatInner after page reload

## Symptom
Conversation `mp86txcdbuaaly`: DB has 4 msgs `[user, assistant, user(VU,_isVirtualUser=true), assistant]`. After a page reload, only the first 2 are rendered — the autopilot synthetic user message and its follow-up assistant reply are silently missing.

## Root Cause — `static/js/core.js:loadConversationMessages` MERGE_ACTIVE_TASK branch

Reconcile-branch dispatch logic (Phase-2 server fetch):
- `localHasUnsynced` → KEEP_LOCAL
- `conv.activeTaskId && hasLocalData` → **MERGE_ACTIVE_TASK**
- otherwise → OVERWRITE / NOOP

After autopilot finishes its follow-up task, the server settings still have `activeTaskId` set to the (now-finished) follow-up task. `_applySettingsToConv` restores it onto `conv`. On reload:
1. IDB cache hit returns the pre-VU snapshot (only 2 msgs — `finishStream` of the *parent* task wrote the cache before `_attachAutopilotFollowup` pushed the VU msg, and the autopilot follow-up's `finishStream` didn't run on this client because the task already completed before the SSE was opened).
2. Phase-2 fetch returns 4 msgs.
3. Branch dispatch picks **MERGE_ACTIVE_TASK** (because `activeTaskId` is truthy).
4. The branch only mutates `lastLocal` (last assistant) from `lastServer` and merges metadata at `min(local.length, server.length)` overlapping indices. **It never appends the missing tail** → server[2..3] discarded.

## Fix
Two-part:

**1. `static/js/core.js` — append trailing server msgs in MERGE_ACTIVE_TASK** when no stream is actually live (`!activeStreams.has(convId)`). Active-stream check preserves the assistantMsg ref held by `connectToTask`; without an active stream the local tail is just stale cache.
```js
if (!activeStreams.has(convId) && serverMsgs.length > conv.messages.length) {
  const _appendStart = conv.messages.length;
  conv.messages.push(...serverMsgs.slice(_appendStart));
  ConvCache.put(conv);
  if (convId === activeConvId) renderChat(conv, false);
}
```

**2. `static/js/main.js:_attachAutopilotFollowup` — `ConvCache.put(conv)` after pushing the VU msg.**  `saveConversations()` doesn't touch IDB; only `ConvCache.put` does. Without this the cache is stuck on the pre-VU snapshot the entire time the follow-up streams.

## Why MERGE_ACTIVE_TASK trips even for finished tasks
`settings.activeTaskId` persists in conv settings on the server until something explicitly clears it. When a task finishes, the frontend clears `conv.activeTaskId` locally and bumps `_activeTaskClearedAt`, but on a fresh browser session that ephemeral marker is gone — `_applySettingsToConv` happily restores the stale `activeTaskId` from server settings. Case B recovery in `initActiveTasks` would later detect the task is finished and clear it, but by that time `loadConversationMessages` has already run and chosen the wrong reconcile branch.

## General principle
The "preserve assistantMsg ref" justification for keeping local messages only applies when a stream is genuinely live (`activeStreams.has(convId)`). For any other branch dispatch with a stale `activeTaskId`, the server tail must be allowed to win over the cache tail.

## Files
- `static/js/core.js` — append trailing server msgs in MERGE_ACTIVE_TASK
- `static/js/main.js` — ConvCache.put after VU push in `_attachAutopilotFollowup`

