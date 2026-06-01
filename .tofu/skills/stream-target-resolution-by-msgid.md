---
name: stream-target-resolution-by-msgid
description: SSE / autopilot streaming MUST resolve assistantMsg by stable _msgId — never rely on conv.messages[length-1]. Helpers: _resolveAssistantById, _findAutopilotPendingCarrier, _rebindAssistant.
enabled: true
tags: [frontend, streaming, autopilot, convention, race-condition]
created: 2026-05-15T06:49:29Z
updated: 2026-05-15T06:49:29Z
---

# Streaming-target resolution by stable `_msgId`

## The rule

Inside any code path that:
- Closes over an `assistantMsg` reference across an `await`, or
- Reads "the assistant message currently being streamed" *after*
  arbitrary other code may have run,

**resolve via `_msgId`, never via `conv.messages[conv.messages.length - 1]`.**

The tail position is unreliable: `loadConversationMessages` Phase 2,
autopilot's `_attachAutopilotFollowup`, queued-dispatch, and any other
push that re-orders `conv.messages` will leave a closure-captured ref
pointing at a detached object. The renderer reads `conv.messages` /
`streamBufs`, so any deltas accumulated into the detached object are
invisible until a full re-render (page refresh / conv switch) — the
"Autopilot content invisible until stop+refresh" symptom.

## Helpers (added to `static/js/ui.js` ~line 627)

```js
_resolveAssistantById(conv, msgId, fallback)
_findAutopilotPendingCarrier(conv) → {msg, idx} | null
```

Inside `_trySSE`, a closure-local helper does the rebind on every event:

```js
const _pinnedMsgId = assistantMsg && assistantMsg._msgId;
function _rebindAssistant() {
  // Re-resolves and reassigns the outer `assistantMsg` if a different
  // live object now owns the same _msgId. Logs `[StableId]` (and
  // `[Autopilot]` when conv.autopilotEnabled) on recovery.
}
// Called at the top of _processSSELine — covers state/delta/done/phase/tool_*
_rebindAssistant();
```

## Sites already using stable-id resolution (2026-05-15)

- `static/js/ui.js:_processSSELine` — `_rebindAssistant()` at top.
- `static/js/ui.js:connectToTask` — when `activeStreams` already has
  an entry for this conv (reconnect), prefer
  `_resolveAssistantById(conv, stream.assistantMsg._msgId, …)` over
  the array tail.
- `static/js/ui.js:finishStream` — `_findAutopilotPendingCarrier(conv)`
  replaces `conv.messages[length-1]._autopilotPending` for both the
  sync-skip flag and the dispatch decision.

## Sites still on tail-lookup (audit and migrate when touched)

- `static/js/ui.js:_processSSELine` user-abort branch
  (`_abortConv.messages[_abortConv.messages.length - 1]`) — sets
  `finishReason='aborted'`. Lower risk because the abort path runs
  synchronously, but consider rebinding for consistency.
- `static/js/ui.js:5237-5240` finishStream's diagnostic `lastMsg`
  read — diagnostic-only, not load-bearing.
- `static/js/main.js:initActiveTasks` Case A / Case C / Case E — these
  read tail to decide whether to push a placeholder; the placeholder
  flow itself is correct, but if Phase 2 races them they could miss
  the live target. Migrate when refactoring init.
- Endpoint mode's `[...conv.messages].reverse().find(m => m._isEndpointPlanner)`
  pattern is OK (predicate-based, not tail-based).

## Diagnostic logs that prove the fix is needed

- `[StableId][Autopilot] 🔁 Rebinding dangling assistantMsg …` —
  the recovery actually fired; without it, those deltas would have
  been silently lost.
- `[twFlush-skip] …` (in `core.js:_twFlush`) — render guard
  rejected a frame. If this fires AND `[StableId]` doesn't, the
  bug is in the render-guard family instead.
- `[Autopilot][post-connect] …` (in `_attachAutopilotFollowup`) —
  one-line snapshot of streamBufs / activeStreams / streaming-msg
  presence. All five booleans must be true for content to appear.

## Why server-side ids work for client-only messages

`_ensureMsgId` mints `tmp_<uuid>` for unsaved client objects; the
server's `_assign_message_ids` overrides with a real UUID on persist
**only if** the message arrives without an id. So the round-trip
preserves identity for any message that already has a `_msgId`.
This means the stable-id resolution works equally well for
freshly-pushed client placeholders and persisted-then-reloaded
messages.

