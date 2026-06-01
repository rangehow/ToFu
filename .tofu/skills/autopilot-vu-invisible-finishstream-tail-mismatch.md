---
name: autopilot-vu-invisible-finishstream-tail-mismatch
description: Bug fix: autopilot VU user msg invisible until refresh — finishStream's _truncatedAway branch and ConvView.finalizeStreaming both treated trailing VU user as a non-assistant tail; renderChat in _attachAutopilotFollowup didn't bypass fingerprint guard
enabled: true
tags: [javascript, bug-fix, autopilot, streaming, finishStream, ConvView]
created: 2026-05-31T09:59:28Z
updated: 2026-05-31T09:59:28Z
---

# Autopilot VU user msg invisible — three fixes in one

## Symptom
Autopilot fires → backend appends VU user msg + spawns follow-up task →
done event carries `autopilotNextTaskId` + `autopilotVuMessage`. On the
client, `_handleAutopilotVuEvent` may have already streamed the VU msg
into `conv.messages` at the tail BEFORE the parent task's `finishStream`
runs. The user sees the parent assistant + new follow-up assistant, but
the VU user bubble between them is missing. Force-refresh restores it
(server has the VU persisted).

## Three race-related root causes

### 1. `finishStream` `_truncatedAway` mistreats trailing VU
`static/js/ui/streaming_ui.js:finishStream` does:

```js
const _fsLast = conv.messages[conv.messages.length - 1];
const _truncatedAway = sm && _fsLast && _fsLast.role !== 'assistant';
if (_truncatedAway) sm.remove();
else if (sm && conv) ConvView.finalizeStreaming(convId, _fsLast);
```

When autopilot has pushed the VU user at the tail, `_fsLast.role === 'user'`.
- The `_truncatedAway` branch removes `#streaming-msg` without finalizing
  the parent assistant → parent assistant DOM gone.
- OR if it falls to the `else`, finalizeStreaming stamps the VU's HTML
  onto the streaming bubble's slot (msg-N where N is the parent's idx).

Fix: walk back from `conv.messages[length-1]` to the nearest
`role === 'assistant' && !_isVirtualUser` and finalize THAT.

### 2. `ConvView.finalizeStreaming` accepted any msg role
`static/js/conv_view.js:finalizeStreaming` blindly did
`sm.outerHTML = renderMessage(msg, idx)` for whatever msg the caller
passed. Add a defensive guard:

```js
if (msg && msg.role && msg.role !== 'assistant' && !msg._isEndpointReview) {
  console.warn(...); return false;
}
```

`_isEndpointReview` (critic) is a legit user-role caller and must stay
allowed.

### 3. `_attachAutopilotFollowup` renderChat with stale fingerprint
After pushing VU + assistant placeholder, `_attachAutopilotFollowup`
called `renderChat(conv)` (default = surgical path). If finishStream's
ConvView.finalizeStreaming had already corrupted msg-N before this
runs, the surgical path may keep that corrupted node. Pass
`forceScroll=true` to force a full re-render.

### 4. Background-conv autopilot — _needsLoad
When `activeConvId !== convId`, `_attachAutopilotFollowup` skips the
DOM render. On tab-switch, `loadConversation` → `loadConversationMessages`
sees `_needsLoad === false` and short-circuits. Even if it doesn't,
`MERGE_ACTIVE_TASK` only appends server tail when
`serverMsgs.length > conv.messages.length`. Local already has VU +
placeholder so length matches and append is skipped → VU never
re-rendered to DOM.

Fix: set `conv._needsLoad = true` in the else branch so next switch
forces a Phase 2 fetch + full renderChat.

## Files
- `static/js/ui/streaming_ui.js:finishStream` — autopilot tail walk-back
- `static/js/conv_view.js:finalizeStreaming` — non-assistant guard
- `static/js/main/main_send_pipeline.js:_attachAutopilotFollowup` —
  renderChat(conv, true) + background-conv `_needsLoad=true`

## Related
- `merge-active-task-drops-trailing-server-msgs-after-autopilot-vu` —
  earlier fix in MERGE_ACTIVE_TASK that appends server tail when no
  active stream.
- `queued-msg-invisible-loadconv-short-circuit` — same family, queue
  dispatch path.

