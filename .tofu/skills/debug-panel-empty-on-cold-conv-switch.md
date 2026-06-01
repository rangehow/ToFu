---
name: debug-panel-empty-on-cold-conv-switch
description: restoreDebugForConv must fetch /debug-messages even when local conv.messages is empty (shell convs)
enabled: true
tags: [debug-panel, needs-load, shell-conv]
created: 2026-05-13T05:53:04Z
updated: 2026-05-13T05:53:04Z
---

# Debug panel empty after server restart / cold conv switch

## Problem
Switching to an old conversation right after server restart showed an
empty debug panel. Content only appeared after a new generation
completed (a `messages_snapshot` SSE).

## Root cause
`restoreDebugForConv` in `static/js/core.js` gated the server fetch on
`conv.messages.length > 0`. But sidebar entries are **shell convs** —
metadata only, with `_needsLoad=true` and `messages=[]` until the user
clicks. The fetch never fired, the `else` branch ran `clearDebug()`.

## Fix (2026-05-13)
- `restoreDebugForConv` now fetches when ANY of:
  `conv.messages.length > 0` OR `(_serverMsgCount||0) > 0` OR `_needsLoad`.
- Shows a "Loading messages from server…" placeholder while in flight.
- Drops the cb if the user switches conv mid-fetch (compares `activeConvId`
  on resolve so we don't paint into the wrong panel).
- `routes/conversations.py:debug_messages` now runs the result through
  `_strip_base64_for_snapshot` before returning — same treatment as the
  live SSE path; prevents huge base64 images from bloating the response
  when an old conv has many image attachments.

## Files
- `static/js/core.js` — `restoreDebugForConv` (~line 761)
- `routes/conversations.py` — `debug_messages` endpoint
- `static/styles.css` — `.debug-loading`

