---
name: tofu-endpoint-mode-consecutive-roles
description: Fix: endpoint mode consecutive assistant messages (planner+worker) — 3-layer fix: skip _isEndpointPlanner in buildApiMessages, merge consecutive same-role in build_body, post-process merge in JS
enabled: true
tags: [endpoint, autonomous-mode, bug-pattern, message-roles]
created: 2026-04-03T10:22:54Z
updated: 2026-04-03T10:43:07Z
---

# Endpoint Mode: Consecutive Duplicate Roles — FIXED

## Problem
In endpoint/autonomous mode (自主模式), the DB conversation has:
```
user(original) → assistant(planner, _isEndpointPlanner) → assistant(worker, _epIteration=1)
→ user(critic, _isEndpointReview) → assistant(worker, _epIteration=2) → ...
```

When a user sends a follow-up message, `buildApiMessages` was producing consecutive
assistant messages (planner + worker) which violates LLM API role alternation
requirements (especially Claude).

Multi-iteration endpoints had an additional issue: after filtering both planner
and critic messages, consecutive worker assistant messages remained.

## Fix (3 layers, all implemented 2026-04-03)

### Layer 1: Frontend `buildApiMessages()` in `main.js` + `bundle.js`
- Skip `_isEndpointPlanner` messages (display-only, content already replaced user in working messages)
- Skip `_isEndpointReview` messages (already existed)
- Post-processing merge loop: scan backwards, merge consecutive user/assistant by concatenating content with `\n\n` separator. Handles multimodal (array) content.

### Layer 2: Backend `_merge_consecutive_same_role()` in `lib/llm_client.py`
- Defence-in-depth function called from `build_body()` after `_strip_non_api_fields()`
- Merges consecutive user/assistant messages (never system/tool)
- Handles multimodal content (list blocks)
- Logs merge count for diagnostics

### Layer 3: Tests in `tests/test_endpoint_messages.py`
- `_build_api_messages_python` helper updated to skip `_isEndpointPlanner` and merge consecutive same-role
- All tests updated to check for NO consecutive assistants (not just users)
- `test_planner_filtered` (renamed from `test_planner_preserved`) verifies planner is excluded
- 23/23 tests pass

## Key Files
- `static/js/main.js` — `buildApiMessages()` (skip + merge)
- `static/js/bundle-e88e13b4.js` — same
- `lib/llm_client.py` — `_merge_consecutive_same_role()`, called from `build_body()`
- `tests/test_endpoint_messages.py` — `_build_api_messages_python()`, all test classes
- `lib/tasks_pkg/endpoint.py` — unchanged (creates the display structure)

## Why Planner Is Display-Only
The planner's output REPLACES the original user message in the LLM working messages
(the `messages` list passed to worker/critic). So the planner content is already
seen by the worker as the user request. Including it again in follow-up turns:
1. Duplicates context (wastes tokens)
2. Creates consecutive assistant messages (API violation)
The planner message in DB/frontend is purely for display/history purposes.

