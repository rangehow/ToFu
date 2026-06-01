---
name: connect-to-task-stale-tail-guard
description: Why connectToTask must reject a finished/different-task trailing assistant before binding SSE
enabled: true
tags: [frontend, sse, bug-pattern, ui.js, main.js]
created: 2026-05-08T13:29:42Z
updated: 2026-05-08T13:29:42Z
---

# `connectToTask` stale-tail guard

## Symptom
After a network blip or page refresh on a running task, the **previous (already-completed) turn's content re-streams into the new turn's bubble**. Switching/refreshing a few times often "fixes" it, because eventually `_pollFallback` rebuilds from `/api/chat/poll` (which doesn't pre-populate from a stale msg ref).

## Root cause
`connectToTask` (`static/js/ui.js:5709`) sets:
```js
let assistantMsg = conv.messages[conv.messages.length - 1];
```
and only pushes a fresh placeholder when the last message **isn't** assistant. After a refresh the most common state is:
- `loadConversationsFromServer` / Phase-2 message loads rebuilt `conv.messages` from DB,
- the last message is therefore the previous (completed) assistant turn,
- `_sync_result_to_conversation` persisted `finishReason` on it.

The pre-populate block at `ui.js:5826-5853` then renders the completed turn's full markdown into the new streaming bubble with status "Resuming…". The SSE state-snapshot (sent on every reconnect without `Last-Event-ID`) does `assistantMsg.content = ev.content` on the same dangling ref, eventually correcting it — but the user sees the old answer "re-stream" first.

## Guard (gate before reusing the trailing assistant)
1. **`static/js/ui.js`** inside `connectToTask`, before the endpoint-critic block: if `conv.messages[-1].role === 'assistant'` AND it isn't an endpoint turn AND (`_taskId !== taskId` OR `finishReason` is set), push a fresh empty placeholder and rebind `assistantMsg` to it.
2. **`static/js/main.js`** Case A in `initActiveTasks`: same check before calling `connectToTask` for a still-running `activeTaskId`. Mirrors Case C's existing placeholder logic.

## Why both backends are needed
- Page-reload path → DB-loaded messages have `finishReason` (persisted) but no `_taskId` (never persisted in `_sync_result_to_conversation`). The `finishReason` check catches it.
- In-memory race path → `assistantMsg._taskId` was set by a prior done event. The `_taskId !== taskId` check catches it.

## Verification logs
- `[connectToTask] 🆕 Last assistant belongs to a prior turn (…)` → ui.js guard fired
- `[initActiveTasks CaseA] Pushing fresh assistant placeholder …` → main.js guard fired

## Endpoint mode is excluded
The block immediately after the new guard already handles `_isEndpointReview` / `_epIteration` transitions; reusing the same trailing-message logic there would double-push placeholders.

