---
name: renderChat-surgical-dom-update-scroll-fix
description: Scroll flicker/jump fixes in renderChat: (1) surgical data-mfp diff; (2) _bgRefreshChat for async cost/file-change prefetch — anchor-relative scrollTop restore (NOT raw pixel, correct when above-fold bubbles grow) + compare-before-swap (unchanged nodes keep DOM/expand state)
enabled: true
tags: [javascript, frontend, scroll, dom, performance, bug-fix, renderChat, content-visibility, surgical-update, activeTaskId]
created: 2026-03-25T09:52:29Z
updated: 2026-07-06T12:07:28Z
---

# renderChat Scroll Flicker / Jump-to-Bottom Fixes

Distinct bugs, all in `static/js/ui/chat_render.js`. Root principle: **a
background re-render must never move the reader — even when content ABOVE the
viewport changes height.**

## Bug A — background sync innerHTML wipe → flicker to TOP
`renderChat(conv, false)` used `inner.innerHTML = html`, destroying all nodes
(scrollTop→0) and losing `content-visibility:auto` caches. Fix = **surgical
per-message diff**: `_msgFingerprint(msg)` → `data-mfp` on each `<div id=msg-N>`;
same=skip, different=`outerHTML` replace, missing=append; remove stale nodes.
Full-innerHTML fallback only when `forceScroll !== false`, different conv, no
msg DOM, or empty conv.

## Bug B (2026-07-06) — async prefetch callbacks force-scrolled to BOTTOM
Symptom: open conv, scroll UP to read, ~1s later flicker then JUMP to bottom.
NOT translation (scroll-safe via `translation.js::_renderMsgInPlace`). Cost +
file-change data is DELIBERATELY excluded from `_msgFingerprint` (async-derived),
so the diff can't repaint it. THREE callbacks worked around that with the
force-scroll full-render path:
- `_prefetchConvCosts(...).then()` → `renderChat(conv,true)` (chat_render.js)
- `_prefetchConvFileChanges(...).then()` → `renderChat(conv,true)`
- `renderFileChangesBar` async fallback → bare `renderChat(conv)` (finish_info.js)
`renderChat(conv,true)` (a) resets lazy window to last `_INITIAL_RENDER`(20)
msgs, (b) `cv-off` height recompute = flicker, (c) `_forceScrollToBottom()` =
jump. Re-entering renderChat (translation done → `renderChat(conv,false)`) re-runs
those callbacks → retriggers.

### Fix: `_bgRefreshChat(conv)` — anchor-relative, compare-before-swap repaint
Repaints ONLY `role==='assistant'` bubbles (they alone carry cost/file-change/
finish bars); user bubbles untouched. All 3 callbacks route through it
(finish_info keeps a defensive `renderChat(conv,false)` fallback). TWO invariants:

1. **Compare-before-swap.** Stamp `node.__bgHtml = freshHtml`; `outerHTML`-replace
   ONLY when the new render differs. Unchanged bubbles keep their exact DOM node
   → manually-expanded tool-round `<details>` state survives. If nothing changed,
   return before touching DOM/scroll.
2. **Anchor-relative scroll restore (NOT raw scrollTop).** A raw `scrollTop = sv`
   restore is WRONG here: on first open the bars aren't fetched, so above-fold
   assistant bubbles render SHORT and GROW when the batch lands → raw restore
   drifts the reader DOWN by the added above-fold height. Instead, under the
   `cv-off` guard: find topmost `[id^=msg-]` still intersecting the viewport
   (`rect.bottom > containerTop+1`), record `rect.top - containerTop` BEFORE
   swaps; after swaps re-measure and `scrollTop += (newOffset - anchorOffset)`
   to re-pin it. Viewport preserved even when above-fold heights change.

No-op during streaming (`#streaming-msg`) or no msg DOM (welcome/skeleton).

## Testing (`tests/test_frontend_bg_refresh_scroll.py`, 3/3)
jsdom has NO layout engine, so DON'T assert raw scrollTop (correct anchor code
moves it on purpose). Install a DETERMINISTIC model: override
`Element.prototype.getBoundingClientRect` + back `scrollTop`/`scrollHeight` with a
vertical-stack height fn where assistant bubbles grow short(100)→tall(130) once
they carry `data-repainted="1"` (the stub `renderMessage` marker). Park the
reader so an above-fold bubble grows, then assert the ANCHOR element's viewport
offset is preserved (±1px) and that a 2nd identical refresh REUSES the same node
object (identity + a `__keepMarker`). HARNESS GOTCHA: `chat_render.js` defines a
hoisted `renderMessage` that shadows a pre-eval stub (pulls in
`renderFileChangesBar`) → REASSIGN `renderMessage` to the marker version AFTER
`eval(src)`. DOUBLE-NEUTER (one per invariant): NC-anchor (`+= (newOffset-
anchorOffset)` → `+= 0`) flips only the anchor checks; NC-compare (`if(__bgHtml
=== fresh) return` → `if(false)`) flips only node-identity. Both files already in
`_BUNDLE_FILES`.

## Related: `_activeTaskClearedAt` race
After `finishStream` sets `conv.activeTaskId=null`, async
`syncConversationToServer` may lag; `loadConversationsFromServer` →
`_applySettingsToConv` restores the stale id. Fix: set
`conv._activeTaskClearedAt=Date.now()` on clear, skip server restore if cleared
within 60s.
