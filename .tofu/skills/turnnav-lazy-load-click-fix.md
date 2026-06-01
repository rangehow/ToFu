---
name: turnnav-lazy-load-click-fix
description: Bug fix: turn-nav dots unclickable — three root causes: (1) scrollToTurn didn't force-load lazy messages, (2) surgical update path destroyed force-loaded messages, (3) showStreamingUIForConv dropped trailing non-assistant msg so its dot pointed at a missing msg-{idx}
enabled: true
tags: [javascript, frontend, bug-fix, lazy-loading]
created: 2026-04-09T03:48:08Z
updated: 2026-04-21T00:00:00Z
---

# Turn Nav Unclickable for Lazy-Loaded Messages

## Bug (Two Root Causes)

### Root Cause 1 (fixed earlier)
`_INITIAL_RENDER = 20` in ui.js means only the last 20 messages are rendered on conversation load. `buildTurnNav()` creates dots for ALL user messages, but `scrollToTurn(idx)` silently failed when the target `msg-{idx}` element wasn't in the DOM.

**Fix:** `scrollToTurn()` now checks if `idx < _lazyRenderedFrom`, and if so, renders all messages from `idx` to `_lazyRenderedFrom`.

### Root Cause 2 (fixed 2026-04-10)
The **surgical update path** in `renderChat()` (the `forceScroll === false` branch) recalculated `startIdx = Math.max(0, total - _INITIAL_RENDER)` independently of `_lazyRenderedFrom`. This meant:

1. User clicks turn dot → `scrollToTurn` force-loads messages down to index N → `_lazyRenderedFrom = N`
2. Background sync fires → surgical update runs → removal step removes all elements where `idx < (total - _INITIAL_RENDER)`, including the force-loaded messages
3. `_lazyRenderedFrom` is NOT updated (stays at N)
4. User clicks same dot again → `getElementById` returns null → `idx < _lazyRenderedFrom` is false (N < N) → **nothing happens**

**Fix:** Surgical update path now uses `Math.min(_lazyRenderedFrom, defaultStart)` as `startIdx`, preserving force-loaded messages in both the update loop and the stale-removal step.

### Root Cause 3 (fixed 2026-04-21) — Last-turn dot unclickable
`showStreamingUIForConv()` called `conv.messages.slice(0, -1)` **unconditionally**,
assuming the last message was always the in-progress streaming assistant. But the
last entry can also be:
- A user message (right after send, before the assistant placeholder appears)
- A critic-done (`_isEndpointReview && done`) message (post-replan waiting state)
- Any transient state where the tail isn't a streaming bubble

In those cases:
- `buildTurnNav()` iterates the full `conv.messages` and creates a dot for the
  last user message at index `N-1`.
- `showStreamingUIForConv` dropped `msg-{N-1}` from the DOM.
- `scrollToTurn(N-1)` returned silently because: (a) `getElementById` null,
  (b) `idx < _lazyRenderedFrom` false (since `N-1 > startIdx`).
- Net effect: the **last** turn-dot does nothing when clicked.

**Fixes:**
1. `showStreamingUIForConv`: only drop the trailing message when it actually
   owns the streaming bubble — `role==='assistant' && !done` OR
   `_isEndpointReview && !done`. Otherwise render all of `conv.messages`.
   Guarded by a `_lastIsStreamingBubble` flag, also used to gate the
   `updateStreamingUI({...})` call at the bottom.
2. `scrollToTurn`: defense-in-depth — when `idx >= _lazyRenderedFrom` and
   the element is missing, log a `console.warn` and force a re-render via
   `showStreamingUIForConv` (if streaming) or `renderChat(conv, true)`.
   Never silently returns.
3. `buildTurnNav`: optional dev assertion behind `window._TOFU_DEV_ASSERT`
   that warns when a produced dot has no corresponding DOM node.

## Key Variables
- `_lazyRenderedFrom` — index of the first currently rendered message
- `_lazyConvId` — ID of the conversation being lazily rendered
- `_INITIAL_RENDER = 20` — number of messages rendered initially

## Files
- `static/js/ui.js` — `scrollToTurn()` (~line 3820), `showStreamingUIForConv()` (~line 4570), `buildTurnNav()` (~line 3795), surgical update path (~line 830)
- `static/js/bundle-*.js` — bundled version (keep in sync)
