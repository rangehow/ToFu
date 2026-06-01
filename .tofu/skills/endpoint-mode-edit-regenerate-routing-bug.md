---
name: endpoint-mode-edit-regenerate-routing-bug
description: Bug fix: edit/regenerate in endpoint mode ran as normal task, not planner→worker→critic loop; also _epIteration not filtered in conv_message_builder
enabled: true
tags: [bug-fix, endpoint, autonomous-mode, routing, edit, regenerate, planner, worker, critic]
created: 2026-04-13T04:17:36Z
updated: 2026-04-13T04:17:36Z
---

# Endpoint Mode: Edit/Regenerate Routing Bug Fix (2026-04-13)

## Bug 1 (Critical): `_start_task_for_conv` ignored `endpointMode`
**Symptom**: When user edits/regenerates a message in endpoint (autonomous) mode, the new task ran as normal chat (single-turn agent), NOT the planner→worker→critic loop.

**Root cause**: `saveEditAndResend()` and `regenerateFromUser()` POST to `/api/chat/regenerate` → `_start_task_for_conv()` → `run_task()`. The `config['endpointMode']` was in the config but never checked. Only `/api/endpoint/start` route called `run_endpoint_task()`.

**Fix**: `_start_task_for_conv()` now checks `config.get('endpointMode')` and routes to `run_endpoint_task()` when truthy, setting `task['endpoint_mode']`, `task['_endpoint_phase']`, `task['_endpoint_iteration']` properly.

## Bug 2: Frontend didn't mark `_isEndpointPlanner` on assistant msg in edit/regen flows
**Symptom**: SSE reconnection couldn't identify the planner message correctly after edit/regenerate.

**Fix**: Added `if (_regenConfig.endpointMode) assistantMsg._isEndpointPlanner = true;` in `saveEditAndResend()`, `regenerateFromUser()`, and `sendMessage()`.

## Bug 3: `conv_message_builder._transform_messages()` didn't skip `_epIteration` worker messages
**Symptom**: Worker iteration messages (role=assistant, `_epIteration=N`) passed through to the API message list, unlike planner and critic messages which were filtered.

**Fix**: Added `if msg.get('_epIteration'): continue` to the filter loop, alongside the existing `_isEndpointPlanner` and `_isEndpointReview` skips.

## Key Files
- `routes/chat.py` — `_start_task_for_conv()` now auto-routes endpoint mode
- `static/js/ui.js` — `saveEditAndResend()` marks `_isEndpointPlanner`
- `static/js/main.js` — `regenerateFromUser()` and `sendMessage()` mark `_isEndpointPlanner`
- `lib/tasks_pkg/conv_message_builder.py` — `_transform_messages()` filters `_epIteration`

## Two API Paths for Endpoint Mode
1. `startAssistantResponse()` → `/api/endpoint/start` → `run_endpoint_task()` (original path)
2. `sendMessage/edit/regen` → `/api/chat/send` or `/api/chat/regenerate` → `_start_task_for_conv()` → `run_endpoint_task()` (fixed path)

