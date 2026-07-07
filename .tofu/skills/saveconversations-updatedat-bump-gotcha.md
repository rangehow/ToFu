---
name: saveConversations-updatedAt-bump-gotcha
description: Bug: saveConversations(convId) bumps updatedAt=now; startup recovery (main_init_tasks.js Case B/D/F) wrongly used conv.id, stamping old convs with load-time in sidebar — pass null
enabled: true
tags: [javascript, frontend, bug-pattern, sidebar]
created: 2026-04-08T03:36:48Z
updated: 2026-06-18T06:27:37Z
---

## Pattern
`saveConversations(changedConvId)` in `core/conversations.js:10` bumps `conv.updatedAt = Date.now()` when `changedConvId` is non-null and the conv is not actively streaming. The sidebar both sorts by AND displays `conv.updatedAt` (`_convSorter` in `core/folders.js:92`), so a wrong bump makes an old conv jump to the top showing the current time.

## When to use null
Pass `null` as `changedConvId` for **metadata-only / housekeeping** changes that are NOT new conversation activity:
- Tool state saves (`_saveConvToolState`)
- Deferred save on conversation switch (`loadConversation`)
- Pin/unpin, folder moves (`core/folders.js`)
- **Startup recovery cleanups in `main/main_init_tasks.js`** (fixed 2026-06): Case B (clearing a finished `activeTaskId`, ~line 404), Case D (ghost empty assistant pop, ~line 152), Case F (clearing stale `server_offline` error, ~line 473). These previously used `saveConversations(conv.id)`, which stamped yesterday's conversations with the page-load time on every refresh. `syncConversationToServer` sends `conv.updatedAt || Date.now()`, so once conv.updatedAt isn't mutated the preserved value is shipped — no server re-stamp.

## When to pass conv.id
Only for genuine new activity: user sends a message, stream finishes (`finishStream` — runs after `activeStreams.delete` so the guard passes), content edit/regenerate.

## Symptom to recognize
A conversation completed days ago shows today's clock time in the sidebar and floats to the top after a page refresh, with no actual new messages.

