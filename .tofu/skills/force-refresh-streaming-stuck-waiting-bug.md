---
name: force-refresh-streaming-stuck-waiting-bug
description: Bug fix: force refresh during streaming shows stuck 'Waiting…' — caused by SSE state event twUpdate rAF dropped when activeConvId is null, stale IDB cache without server checkpoint data, and empty array truthy bug in searchRounds fallback
enabled: true
tags: [javascript, streaming, race-condition, force-refresh, bug-fix, SSE, activeConvId, twUpdate, IDB-cache, checkpoint, init-sequence]
created: 2026-03-31T17:37:36Z
updated: 2026-03-31T17:37:36Z
---

# Force Refresh "Stuck Waiting" During Streaming

## Root Cause (3 contributing factors)

### 1. `twUpdate` rAF guard drops SSE events during init
During page init after force refresh, `activeConvId` is null (set by `newChat()`).
`connectToTask` is fire-and-forget (not awaited). If the SSE `state` event arrives
and `twUpdate(convId)` schedules a rAF, the callback checks `activeConvId === cid`
→ null !== convId → **update silently dropped**. When `showStreamingUIForConv` later
creates the DOM and reads the buf, the buf HAS data → renders correctly. But if
the SSE data arrives AFTER the initial render and the rAF was already consumed,
the UI stays "Waiting…" until the next SSE event.

**Fix:** Also render when `activeConvId` is null but `streaming-body` DOM exists:
```js
if (activeConvId === cid || (!activeConvId && document.getElementById('streaming-body')))
```

### 2. Phase 2 skips checkpoint merge for active tasks
`loadConversationMessages` Phase 2 has guard:
```js
!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId)
```
For convs with `activeTaskId` + IDB cache hit, Phase 2 completely skips server data.
The cache may be stale (from before the task started), missing checkpoint content.

**Fix:** New branch merges server checkpoint data INTO existing assistant message
(without replacing `conv.messages` array which would orphan `connectToTask`'s ref):
```js
if (conv.activeTaskId && hasLocalData) {
    // merge lastServer content/thinking/searchRounds into lastLocal
}
```

### 3. Empty array truthy in searchRounds fallback
`buf?.searchRounds || getSearchRoundsFromMsg(lastMsg)` — `[]` is truthy in JS,
so empty buf searchRounds prevent fallback to loaded message data.

**Fix:** `(buf?.searchRounds?.length ? buf.searchRounds : null) || getSearchRoundsFromMsg(lastMsg)`

### 4. Deferred re-render for SSE connection latency
Added 300ms deferred `updateStreamingUI` call in `showStreamingUIForConv` to catch
SSE data arriving during the connection setup window.

## Init Sequence (for reference)
1. `newChat()` → `activeConvId = null`
2. `initActiveTasks()` → `loadConversationsFromServer` + `loadConversationMessages`
3. `connectToTask(convId, taskId)` — fire-and-forget, sets `activeStreams`, `twStart`
4. SSE `fetch()` yields → `_ensureNewest()` → `initActiveTasks` resolves
5. `.then()`: `loadConversation(_restoredConvId)` → sets `activeConvId` → `showStreamingUIForConv`
6. SSE data arrives (async) → `twUpdate` → rAF → render

