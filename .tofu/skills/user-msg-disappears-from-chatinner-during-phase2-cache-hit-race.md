---
name: user-msg-disappears-from-chatinner-during-phase2-cache-hit-race
description: Bug fix: user message vanishes from chatInner after send when sendMessage races with Phase 2 server fetch following Phase 1 IDB cache hit — `localHasUnsynced` was gated by `!cacheHit`, suppressing the optimistic user msg's newer timestamp signal
enabled: true
tags: [javascript, race-condition, bug-fix, loadConversationMessages, sendMessage, Phase2, cacheHit, chatInner, user-message-lost]
created: 2026-05-14T03:59:56Z
updated: 2026-05-14T03:59:56Z
---

# User message disappears from chatInner during Phase 2 cache-hit race

## Symptom
After clicking a conversation, the user types and sends a message. Only the assistant's streamed reply is visible in chatInner; the user's own prompt is missing. A manual refresh restores it (server-persisted copy reloads).

## Race
1. `loadConversation` fires `loadConversationMessages` fire-and-forget.
2. Phase 1 IndexedDB cache hit → `conv.messages = cached`, `_needsLoad=false`. Render. User can interact.
3. Phase 2 server fetch (`GET /api/conversations/:id`) is still in flight.
4. User hits Send → `sendMessage()`:
   - skips load-await (`_needsLoad` is false)
   - `conv.messages.push(userMsg)`, render bubble into DOM
   - `await fetch('/api/chat/send')` — long await (translation, task-start)
5. Phase 2 server response arrives during step 4's await. `loadConversationMessages` post-fetch logic evaluates:
   - `localHasUnsynced = hasLocalData && !cacheHit && localNewest > serverNewest` → **false**, because `!cacheHit` is false.
   - `conv.activeTaskId` is still null (POST hasn't returned yet).
   - Falls into the third branch (`!activeStreams.has(convId) && !conv.activeTaskId`) → TRUE.
   - `cacheIsStale = serverMsgs.length !== conv.messages.length` → TRUE (local has +1 user msg).
   - **`conv.messages = serverMsgs`** wipes the optimistic user message.
   - `renderChat(conv, false)` removes msg-N from DOM.
6. POST returns, assistantMsg pushed, SSE streams. ChatInner shows assistant only.
7. `finishStream` syncs server data; manual refresh later restores the user msg.

## Root Cause
Inside `loadConversationMessages` (`static/js/core.js`), the `localHasUnsynced` gate had `!cacheHit`, intending "don't trust pre-cache local data because cache might be older than server". This silently suppressed the new-write case: when sendMessage pushes an optimistic user msg AFTER cache hit but BEFORE Phase 2 returns, the bumped local timestamp/length cannot signal unsynced state. The earlier "Phase 2 overwrite race" fix removed `cacheHit` from the third branch's guard but `activeTaskId` still appears too late (only after POST returns) — leaving an open window between `userMsg.push` and `conv.activeTaskId = taskId`.

## Fix (`static/js/core.js`)

1. Snapshot `conv.messages.length`, newest timestamp, and `activeTaskId` BEFORE the server fetch starts.
2. After the fetch returns, derive `_hasFreshLocalActivity` from those snapshots:
   - `_localGrewDuringFetch` — array length grew
   - `_localTsMovedDuringFetch` — newest ts advanced
   - `_activeTaskIdAppearedDuringFetch` — `conv.activeTaskId` was set
3. Drop `!cacheHit` from `localHasUnsynced`; OR-in `_hasFreshLocalActivity`.
4. Belt-and-suspenders: gate the third branch's overwrite on `!_hasFreshLocalActivity` too, in case `localHasUnsynced` ever misses.
5. Skip `syncConversationToServer(conv)` in the `localHasUnsynced` branch when `_hasFreshLocalActivity` is the trigger — `/api/chat/send` already owns persistence of the optimistic msg, and a racing PUT could overwrite the freshly-committed `translatedContent`.

## Files
- `static/js/core.js` — `loadConversationMessages` Phase 2 reconciliation (~lines 2143-2360)

## Detection
Look for the `[loadConvMsgs] ⚠️ KEPT local data` warn line: when `freshLocalActivity=true (grew=true ...)`, this race triggered and was prevented.

## Related Memory
`loadConversationMessages-phase2-overwrite-race-condition` — earlier related fix (sidebar-pulses-but-no-Agent-icon variant). Same root file, different but adjacent symptom; both fixes work together.

