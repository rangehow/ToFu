---
name: frontend-backend-migration-deprecated-cleanup
description: Frontend→backend migration: buildApiMessages removed, convRef resolution moved to backend, debug-messages endpoint added
enabled: true
tags: [frontend, backend, migration, branch, message-builder, architecture]
created: 2026-04-14T05:16:43Z
updated: 2026-04-14T16:47:25Z
---

# Frontend→Backend Migration: Completed Cleanups

## buildApiMessages (REMOVED from frontend — 2026-04-14)
- `buildApiMessages()`, `_collapseHistoricalEndpointSessions()`, `_formatConvRefText()` removed from `static/js/main.js` (~250 lines)
- Backend equivalent: `lib/tasks_pkg/conv_message_builder.py` → `build_api_messages_from_db()`
- Debug panel fallback in `core.js` now calls `GET /api/conversations/<id>/debug-messages`
- Endpoint added in `routes/conversations.py` → `debug_messages()`

## Conversation Reference Resolution (moved to backend — 2026-04-14)
- Frontend no longer fetches referenced conversations and formats them client-side
- `sendMessage()` now sends only `convRefs: [{id, title}]` — no `convRefTexts`
- Backend `_resolve_conv_refs()` in `routes/chat.py` resolves refs using `lib/conv_ref.get_conversation()`
- `lib/message_queue.py` also resolves refs server-side when dispatching queued messages
- Backward compatible: if `convRefTexts` is still provided (old clients), it's used as-is

## Key Files
- `routes/chat.py` — `_resolve_conv_refs()`, `_build_user_msg_from_payload()` updated
- `routes/conversations.py` — new `debug_messages()` endpoint
- `lib/message_queue.py` — convRef resolution in `dispatch_next_queued()`
- `static/js/main.js` — removed ~250 lines of deprecated code
- `static/js/core.js` — debug panel uses backend API instead of `buildApiMessages()`

