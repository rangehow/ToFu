---
name: queued-msg-invisible-loadconv-short-circuit
description: Bug fix: queued user message invisible until force-refresh — _checkForQueuedTask called loadConversationMessages on a non-stale conv → short-circuit → server-persisted queued user msg never reaches conv.messages
enabled: true
tags: [javascript, bug-fix, queue, loadConversationMessages, race-condition]
created: 2026-05-31T09:24:02Z
updated: 2026-05-31T09:24:02Z
---

# Queued user message invisible until force-refresh — short-circuit fix

## Symptom
User types message A (already streaming) → types message B in input box → B
gets queued by backend. When current task finishes and backend dispatches B,
the user sees the new assistant streaming bubble but **B's user bubble is
missing** in chatInner. Only manual page reload makes B appear (loaded from
server DB).

## Root Cause
`static/js/main/main_send_pipeline.js:_checkForQueuedTask` does:

```js
await loadConversationMessages(convId);
```

But `loadConversationMessages` (`static/js/core/conversations.js:533`) starts:

```js
if (!conv._needsLoad && conv.messages.length > 0) return conv;
```

For an active conv that hasn't been reloaded since the user clicked into it,
`_needsLoad` is `false`. The function **short-circuits without fetching**.
The queued user message that the backend persisted via `dispatch_next_queued()`
never reaches `conv.messages`. Then:

```js
conv.messages.push(assistantMsg);  // empty placeholder
renderChat(conv);                   // shows: ...prior msgs..., new assistant placeholder
```

No queued user bubble is rendered.

## Fix
Force-refresh by setting `_needsLoad=true` immediately before the call:

```js
conv._needsLoad = true;
await loadConversationMessages(convId);
```

This makes `loadConversationMessages` skip the short-circuit, run Phase 1
(IDB cache) + Phase 2 (server fetch), and the queued user msg ends up in
`conv.messages` before the assistant placeholder is pushed.

## Why force-refresh "fixes" it as a workaround
Page reload reconstructs `conversations[]` from server `loadConversationsFromServer`
which sets `_needsLoad=true` on every conv. The next `loadConversation` then
actually fetches.

## Related
- `queued-message-invisible-after-stop-fix` — earlier fix for the same bug
  family (skip full-conv PUT race + retry schedule + optimistic placeholder).
  That fix made the queued user msg appear ON THE SERVER but didn't address
  the frontend's loadConversationMessages short-circuit; the bubble was
  still missing in chatInner until reload.
- `autopilot-mode` — autopilot VU user msg has a similar "invisible until
  reload" symptom; root cause may differ (lazy bubble creation via
  `_handleAutopilotVuEvent` instead of server queue dispatch).

## Files
- `static/js/main/main_send_pipeline.js:_checkForQueuedTask`

