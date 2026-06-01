---
name: surgical-dom-truncation-scroll-fix
description: Fix for scroll-jump-to-top when regenerating/editing messages: use surgical DOM removal instead of innerHTML wipe in renderChat, preserving scroll position entirely
enabled: true
tags: [javascript, frontend, scroll, dom, performance, bug-fix, renderChat]
created: 2026-03-25T04:34:09Z
updated: 2026-03-25T04:34:09Z
---

# Surgical DOM Truncation — Scroll Jump Fix

## Problem
When user clicks Regen or Edit+Resend, `renderChat(conv)` is called after truncating `conv.messages`. `renderChat` does `inner.innerHTML = html` which:
1. Nukes all chat DOM in one shot → browser scroll resets to 0 (top)
2. `_forceScrollToBottom()` fires asynchronously (double-rAF + setTimeout)
3. Between the innerHTML wipe and the first rAF callback, the browser renders at least one frame at scrollTop=0 → visible flash to top

## Fix Pattern
Instead of calling `renderChat(conv)` after message truncation, surgically remove only the DOM nodes for messages beyond the truncation point:

```javascript
let usedSurgical = false;
if (activeConvId === conv.id) {
  const inner = document.getElementById("chatInner");
  if (inner) {
    const toRemove = [];
    inner.querySelectorAll('.message[id^="msg-"]').forEach(el => {
      const m = el.id.match(/^msg-(\d+)$/);
      if (m && parseInt(m[1], 10) > idx) toRemove.push(el);
    });
    const oldStreaming = document.getElementById("streaming-msg");
    if (oldStreaming) toRemove.push(oldStreaming);
    
    if (toRemove.length > 0 || inner.querySelector('.message[id^="msg-"]')) {
      for (const el of toRemove) el.remove();
      usedSurgical = true;
      _lastRenderedFingerprint = _convRenderFingerprint(conv);
      buildTurnNav(conv);
    }
  }
}
if (!usedSurgical) renderChat(conv);  // fallback
```

## Key Details
- Message elements use `id="msg-{index}"` — parse the index to determine which are beyond the truncation point
- The lazy-load sentinel (`#_lazyLoadSentinel`) is NOT matched by `[id^="msg-"]`, so it stays intact
- For edit+resend, also re-render the edited message itself with `editedEl.outerHTML = renderMessage(msg, idx)`
- Always update `_lastRenderedFingerprint` to prevent stale fingerprint from triggering an unwanted full re-render later
- Falls back to `renderChat(conv)` if DOM state is unexpected (e.g. viewing a different conversation)

## Applied In
- `regenerateFromUser()` in `main.js`
- `saveEditAndResend()` in `ui.js`

