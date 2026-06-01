---
name: saveConversations-updatedAt-bump-gotcha
description: Bug pattern: saveConversations(convId) bumps updatedAt to Date.now() — pass null for metadata-only changes to avoid sidebar reordering
enabled: true
tags: [javascript, frontend, bug-pattern, sidebar]
created: 2026-04-08T03:36:48Z
updated: 2026-04-08T03:36:48Z
---

# saveConversations(changedConvId) — updatedAt Bump Gotcha

## Pattern
`saveConversations(changedConvId)` in `core.js` bumps `conv.updatedAt = Date.now()` when `changedConvId` is non-null and the conv is not actively streaming. This causes the conversation to sort to the top of the sidebar.

## When to use null
Pass `null` as `changedConvId` for **metadata-only** changes that are NOT new conversation activity:
- Tool state saves (`_saveConvToolState`) — toggling search/fetch/code etc.
- Deferred save on conversation switch (`loadConversation`)
- Clearing stale project paths (`_restoreConvProject` failure)
- Pin/unpin (already correct — see `togglePinConversation`)

## When to pass conv.id
Pass the actual conv ID only for genuine new activity:
- User sends a message
- Stream finishes
- Content is modified (edit, regenerate)

## Reference
The `togglePinConversation` function already has a comment documenting this:
```javascript
/* Pass null instead of id — pin/unpin is a metadata-only change,
 * NOT new conversation activity.  Passing changedConvId would bump
 * updatedAt = Date.now(), which makes the unpinned conversation
 * jump to the top of the non-pinned section. */
saveConversations(null);
```

