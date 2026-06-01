---
name: input-toolbar-chat-decoupled-layout
description: Layout fix: chat-inner uses fixed 820px max-width, decoupled from toolbar width; --chat-w removed; --toolbar-w still measured via JS
enabled: true
tags: [css, layout, performance, toolbar]
created: 2026-04-06T14:26:44Z
updated: 2026-04-06T14:51:47Z
---

# Decoupled Input Toolbar / Chat Layout

## Problem
The input box width (`--toolbar-w`) and chat message area width (`--chat-w`) were synced — 
`_reflowToolbar()` set both. This caused `.chat-inner` to shrink to toolbar width (~650px) 
instead of a comfortable reading width.

## Solution (2026-04-06)
- **Decoupled**: `.chat-inner` uses a fixed `max-width: 820px` — no CSS variable.
- **`--chat-w` removed**: No JS sets it. The CSS `var(--chat-w, 820px)` was replaced with plain `820px`.
- **`--toolbar-w` kept**: `_reflowToolbar()` still measures toolbar width via the 9999px expansion trick 
  and sets `--toolbar-w` on `.input-inner`. This ensures the textarea + toolbar are a cohesive unit.
- The toolbar and chat area are now **independent widths**.

## Key files
- `static/styles.css` line ~69: `.chat-inner{max-width:820px;...}`
- `static/js/main.js` `_reflowToolbar()`: only sets `--toolbar-w`, not `--chat-w`

## Anti-pattern
Never tie `.chat-inner` width to toolbar measurement — they serve different purposes 
(readability vs button layout).

