---
name: streaming-msg-destroyed-by-renderChat-full-rerender
description: Bug fix: renderChat(conv) full re-render during streaming destroys #streaming-msg → premature finish bar with only model tag while sidebar still pulsing
enabled: true
tags: [bug-fix, streaming, renderChat, frontend]
created: 2026-04-09T16:20:05Z
updated: 2026-04-09T16:20:05Z
---

# Streaming Bubble Destroyed by Full renderChat

## Bug
During active streaming, `renderChat(conv)` (without `forceScroll=false`) does a full `innerHTML` wipe of the chat area, destroying the `#streaming-msg` element. The streaming assistant message (which has `msg.model` set from the SSE state/preset event) gets rendered as a static message via `renderMessage()`, producing a finish bar with **only the model tag** (no ✓/tokens/cost because `finishReason` and `usage` aren't set yet).

**Symptom**: sidebar shows breathing light, send button is pausable, but a "message finish" area appears at the bottom with only the model preset tag inside — as if the message is complete.

## Root Cause
`renderFinishInfo()` at line ~1447 returns non-empty HTML when ANY of `finishReason`, `usage`, `model`, `preset`, or `effort` is truthy. Since `model` is set early via SSE `state` or `preset` events, the finish bar renders with just the model tag.

The `#streaming-msg` element uses `id="streaming-msg"` (not `id="msg-N"`), so the surgical render loop's else branch treats it as a "new message" and appends a static `renderMessage()` output, then step 3 removes the `#streaming-msg`.

## Triggers
- `translateMessage()` toggle (user clicks Translate/Original on a previous message during streaming)
- `translateMessage()` poll callback completing
- KaTeX lazy loading (`_ensureKatex()` callback)
- Endpoint mode `_pollFallback` new turns arriving
- Any other code calling `renderChat(conv)` without checking `activeStreams`

## Fix (Guard 1c in renderChat)
```js
if (conv.id === activeConvId && activeStreams.has(conv.id) && document.getElementById('streaming-msg')) {
    if (typeof showStreamingUIForConv === 'function') showStreamingUIForConv(conv.id);
    return;
}
```
Also fixed surgical path to skip the streaming message index, and changed `translateMessage` to use surgical single-element replacement (`el.outerHTML = renderMessage(msg, idx)`) instead of `renderChat(conv)`.

