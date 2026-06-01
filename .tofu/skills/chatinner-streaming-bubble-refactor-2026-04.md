---
name: chatinner-streaming-bubble-refactor-2026-04
description: Refactored chatInner streaming bubble HTML into shared _streamingBubbleHTML() and _surgicalTruncateDOM() helpers, fixed 2 bugs
enabled: true
tags: [javascript, frontend, refactor, streaming, chatInner, bug-fix]
created: 2026-04-17T04:41:37Z
updated: 2026-04-17T04:41:37Z
---

# chatInner Streaming Bubble Refactor (2026-04-17)

## Shared helpers added to ui.js (near line 522):

1. **`_streamingBubbleHTML(role, status, timeStr)`** — Generates the HTML for `#streaming-msg` bubble.
   - `role`: `'worker'` | `'planner'` | `'critic'`
   - Handles avatar SVG, CSS classes, role label, status text
   - Used by 11 call sites across main.js and ui.js

2. **`_streamingBubbleRole(conv, cfg)`** — Determines role from config/conv state.

3. **`_surgicalTruncateDOM(conv, cutoffIdx)`** — Removes DOM elements for messages with index > cutoffIdx.
   - Used by `regenerateFromUser` and `saveEditAndResend`

## Bugs fixed:

1. **`regenerateFromUser` abort-during-translation** was missing:
   - `syncConversationToServer(conv, { allowTruncate: true })` — messages already truncated
   - `fetch(abort-conv/convId)` — server may have started a task
   (Compare: `saveEditAndResend` and `sendMessage` both do these correctly)

2. **`image-gen.js` scroll no-op**: `chatDiv.scrollTop = chatDiv.scrollHeight` where chatDiv is `chatInner` (not the scrollable container `chatContainer`). Fixed to use `scrollToBottom()`.

