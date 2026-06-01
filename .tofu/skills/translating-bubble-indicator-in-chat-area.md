---
name: translating-bubble-indicator-in-chat-area
description: Fix: show "Translating…" bubble in chat area during server-side translation in atomic send/regenerate flows
enabled: true
tags: [javascript, frontend, translation, ui, bug-fix, streaming]
created: 2026-04-13T12:43:33Z
updated: 2026-04-13T12:43:33Z
---

# Translating Bubble Indicator in Chat Area

## Problem
When auto-translate is on and user sends Chinese text, the atomic `/api/chat/send` or `/api/chat/regenerate` endpoint blocks while translating server-side. During this time:
- Stop button was visible ✓
- Sidebar showed "翻译中" ✓  
- **Chat area showed nothing** — user message appeared but no indication of activity below it

## Fix
Added `_renderTranslatingBubble()` and `_removeTranslatingBubble()` in `main.js`:
- Shows an agent bubble with pulsing "Translating…" text (reuses `stream-status` + `pulse` CSS)
- Rendered when `_willTranslate` is detected, before the blocking `fetch`
- Removed in: success path (before streaming bubble), abort path, and `finally` block (safety)

## Affected Flows (all 3 patched)
1. `sendMessage()` in `main.js` — new message send
2. `saveEditAndResend()` in `ui.js` — edit + resend
3. `regenerateFromUser()` in `main.js` — regenerate

## DOM Element
- `id="translating-msg"` — distinct from `id="streaming-msg"` to avoid conflicts
- Uses same `stream-status` + `pulse` CSS classes for consistent look

