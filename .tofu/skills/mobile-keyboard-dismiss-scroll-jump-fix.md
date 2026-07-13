---
name: mobile-keyboard-dismiss-scroll-jump-fix
description: Bug fix: mobile keyboard close after sendMessage causes chat to jump to middle — viewport grows but scrollTop stays same
enabled: true
tags: [mobile, scroll, keyboard, visualViewport, bug-fix]
created: 2026-04-10T00:06:48Z
updated: 2026-07-10T00:00:00Z
---

# Mobile Keyboard Dismiss Scroll Jump Fix

## Problem
On mobile, after sending a message in a multi-round conversation, the chat "jumps to the middle" instead of staying at the bottom.

## Root Cause
1. User types message with keyboard open → viewport is ~50% of screen height
2. `sendMessage()` appends user msg, calls `scrollToBottom(true)` — scrolls to bottom of keyboard-open viewport
3. `startAssistantResponse()` appends streaming-msg, calls `scrollToBottom()`
4. Keyboard dismisses (input cleared, focus lost) → `visualViewport` resize fires → `body.style.height` grows back to full screen
5. `scrollTop` stays at the same pixel value, but `clientHeight` has doubled → content appears in the middle

## Fix (in `initMobileKeyboardHandler`, main.js)
The `onViewportResize` handler now distinguishes keyboard opening (viewport shrinking) vs closing (viewport growing):

- **Keyboard opening**: Records `_wasNearBottom = isNearBottom(200)` + scrolls textarea into view (existing behavior)
- **Keyboard closing**: If `_wasNearBottom` was true OR there's an active stream, force-scrolls to bottom:
  1. Sync reflow (`void cc.scrollHeight`) after body height change
  2. Immediate `cc.scrollTop = cc.scrollHeight`
  3. Safety rAF `scrollToBottom(true)` for keyboard animation that fires multiple resize events

## Key Detail
The `growing` flag (`newH > lastHeight`) determines direction. This is reliable because `visualViewport.resize` fires incrementally as the keyboard animates.

## Related: same symptom, DIFFERENT mechanism on desktop
"Sending at the bottom lands mid-history" also happens on DESKTOP, but via a
distinct cause — do NOT conflate them. Desktop = the single-rAF `scrollToBottom`
clamps against the 120px `content-visibility:auto` estimate of the freshly-
appended user + streaming bubbles BEFORE their real height paints, so
`scrollHeight` grows below the fixed `scrollTop`. Fixed (2026-07-10, see JOURNAL)
by routing the three send-path scrolls through the real-height
`_forceScrollToBottom(null,true)` (cv-off + forced reflow). This mobile fix
(`visualViewport` resize grows `clientHeight` while `scrollTop` stays fixed) and
the desktop cv:auto fix are separate — the mobile keyboard site
`main_folders_mobile.js:750 scrollToBottom(true)` is deliberately left on THIS
path, not the cv-off path.

