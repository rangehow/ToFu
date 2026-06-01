---
name: renderChat-surgical-dom-update-scroll-fix
description: Fix for scroll-flicker-to-top caused by renderChat(conv,false) innerHTML wipe: surgical per-message DOM diffing using data-mfp fingerprint attributes preserves content-visibility:auto size caches and scroll position
enabled: true
tags: [javascript, frontend, scroll, dom, performance, bug-fix, renderChat, content-visibility, surgical-update, activeTaskId]
created: 2026-03-25T09:52:29Z
updated: 2026-03-25T09:52:29Z
---

# renderChat Surgical DOM Update — Scroll Flicker Fix

## Problem
`renderChat(conv, false)` (background sync re-renders) used `inner.innerHTML = html` which:
1. Destroys ALL DOM nodes → browser resets scrollTop to 0
2. Loses `content-visibility: auto` size caches → height estimation errors
3. Restored `scrollTop` maps to wrong visual position with large messages (10K+ chars)
4. Results in visible scroll flicker to top, especially during translation → sync → re-render cycles

## Solution: Surgical Per-Message DOM Diffing
When `forceScroll === false` and the DOM already has rendered messages:

1. **`_msgFingerprint(msg)`** — generates a per-message fingerprint from role, content length, thinking length, error length, finishReason, translatedContent length, etc.
2. Each rendered `<div class="message" id="msg-N">` gets a `data-mfp="..."` attribute
3. On re-render, compare `data-mfp` of existing DOM node vs new fingerprint:
   - Same → skip (don't touch this node at all)
   - Different → `outerHTML` replace (single node, preserves other nodes)
   - Missing → append new node
4. Remove stale nodes (beyond current message count)

### Key Benefits
- **Zero scroll flicker**: unchanged messages are never touched, scroll position is perfectly preserved
- **`content-visibility: auto` caches preserved**: off-screen messages keep their actual measured height
- **Performance**: only changed messages are re-rendered, not the entire conversation

### Guard: Fall back to full innerHTML path when
- `forceScroll !== false` (initial load, explicit scroll-to-bottom)
- Different conversation than active
- No message DOM nodes yet (welcome screen, loading skeleton)
- Empty conversation

## Related Fix: `_activeTaskClearedAt` Race Condition
After `finishStream` sets `conv.activeTaskId = null`, async `syncConversationToServer` may not have completed yet. `loadConversationsFromServer` → `_applySettingsToConv` would restore the stale `activeTaskId` from server. Fix: set `conv._activeTaskClearedAt = Date.now()` on clear, and skip server restore if cleared within 60s.

