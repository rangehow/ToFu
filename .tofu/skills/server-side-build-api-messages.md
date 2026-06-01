---
name: server-side-build-api-messages
description: Architecture change: buildApiMessages moved from frontend to backend — frontend sends {convId, config} only, backend loads from DB
enabled: true
tags: [architecture, frontend, backend, messages, refactoring]
created: 2026-04-11T15:39:36Z
updated: 2026-04-13T04:33:27Z
---

# Server-Side buildApiMessages Migration

## Architecture Change (2026-04-11)
Frontend no longer sends processed messages to `/api/chat/start`. Instead:

1. Frontend sends `{convId, config}` — no `messages` array
2. Backend loads raw messages from PostgreSQL via `build_api_messages_from_db()`
3. Backend transforms them (same logic as old JS `buildApiMessages()`)

## Key Files
- `lib/tasks_pkg/conv_message_builder.py` — Server-side message builder (NEW)
  - `build_api_messages_from_db(conv_id, config, exclude_last=False)` — Main entry point
  - `_transform_messages(raw_messages, config)` — Pure transformation logic
  - `_build_user_message(msg)` — Handles quotes, refs, PDFs, images, notranslate
  - `_build_assistant_message(msg)` — Handles toolSummary fallback
  - `_merge_consecutive_same_role(messages)` — In-place merge
- `routes/chat.py` — `chat_start()` now calls builder when no messages in POST
  - `chat_send()` — Atomic send: create user msg + translate + persist + start task
  - `chat_regenerate()` — Atomic: truncate + edit + translate + start task
  - `_start_task_for_conv()` — Shared helper: build API messages + start task (handles endpoint mode + external backends)
  - `_build_user_msg_from_payload()` — Build user message dict from frontend payload
  - `_persist_conv_messages()` — Full UPSERT with search_text, settings, msg_count
  - `chat_tool_state()` — Lightweight PATCH for tool settings only
- `routes/endpoint.py` — `endpoint_start()` same pattern
- `static/js/main.js` — Frontend refactored:
  - `sendMessage()` → calls `/api/chat/send` (atomic)
  - `regenerateFromUser()` → calls `/api/chat/regenerate` (atomic)
  - `startAssistantResponse()` → calls `/api/chat/start` (no messages, backend loads from DB)
  - `continueAssistant()` → calls `/api/chat/start` with `excludeLast: true` + toolHistory
  - `_buildConvConfig(conv)` — Shared config builder (per-conv state, avoids globals cross-talk)
  - `_buildConvSettings(conv)` — Settings for server persistence

## Critical: Frontend buildApiMessages Still Used By
- `static/js/branch.js` — Branch mode sends messages directly in POST body (legacy path)
- `static/js/core.js` — Debug panel fallback for displaying messages

## Critical: Sync Before POST
Frontend MUST `await syncConversationToServer(conv)` BEFORE the POST to `/api/chat/start`,
because the backend reads from DB. Without this, the DB has stale messages.

## Server-Side Message Queue
- `lib/message_queue.py` — Server-side queue for conversations with active tasks
  - `dispatch_next_queued()` — Auto-dispatches after task completes
  - Called from `persist_task_result()` → `_dispatch_queued_message()`
  - Queue routes: POST/GET/DELETE `/api/chat/queue[/<conv_id>]`

## Bug Fixes Applied (2026-04-13)
1. Dead code after `chat_tool_state()` referencing undefined `task` variable — removed
2. `continueAssistant()` config used globals instead of per-conv state — replaced with `_buildConvConfig(conv)`
3. Queue-on-stream config duplicated inline — replaced with `_buildConvConfig(conv)`
4. `_load_messages_from_db()` missing `user_id=1` filter — added
5. `dispatch_next_queued()` missing `_translateDone = True` flag — added

## Tests
- `tests/test_conv_message_builder.py` — 25 tests covering all transformations

