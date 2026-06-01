---
name: finishstream-scroll-jump-upward-fix
description: Bug fix: finishStream content jumps upward because streaming-msg→final message outerHTML swap collapses expanded thinking block + _forceScrollToBottom forces to smaller scrollHeight
enabled: true
tags: [javascript, frontend, scroll, bug-fix, finishStream, content-visibility, thinking-block]
created: 2026-04-09T13:00:23Z
updated: 2026-04-09T13:00:23Z
---

# finishStream Scroll-Jump-Upward Fix

## Problem
After generation completes, the content visually "jumps upward" — the user's reading position is lost.

## Root Cause (two-part)
1. **Height collapse**: During streaming, `.thinking-block.expanded` has `max-height:none` showing full thinking text (potentially thousands of px). `finishStream` does `sm.outerHTML = renderMessage(msg, idx)` which renders the thinking block **collapsed** (no `.expanded` class → `max-height:0`). This dramatically shrinks the message height.

2. **Forced scroll**: `_forceScrollToBottom()` (without `forceActualHeights`) was called unconditionally after the replacement. It scrolls to the new (much smaller) `scrollHeight`, which maps to a visually different position.

## Fix (in `finishStream`, ui.js)
1. **Save/restore scrollTop** around the `outerHTML` replacement — exactly like `_startAutoTranslateForMsg` already does for translation re-renders.
2. **Replace `_forceScrollToBottom()` with `if (isNearBottom(80)) scrollToBottom()`** — only auto-scroll if user was already near bottom, don't hijack their position.

## Also applies to
- The translation poll loop in `_startAutoTranslateForMsg` already had the save/restore pattern (was correct).
- The `_resumePendingTranslations` uses `renderChat(conv, false)` with surgical update (was correct).

