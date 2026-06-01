---
name: endpoint-mode-multi-session-history-bug
description: Fix: endpoint mode follow-up messages in same conversation — collapse historical sessions, use reverse find for planner
enabled: true
tags: [endpoint, bug-fix, multi-session, planner, critic, message-builder, history]
created: 2026-04-14T04:36:41Z
updated: 2026-04-14T04:36:41Z
---

# Endpoint Mode Multi-Session History Bug

## Symptoms
1. After an endpoint session ends, sending another message with endpoint mode ON causes the planner to "reuse" the previous plan
2. The critic from the first session may not display properly
3. The new planner's streaming content goes to the old planner object

## Root Causes

### Backend: `conv_message_builder.py` strips ALL endpoint history
`_transform_messages()` unconditionally skipped every message with `_isEndpointPlanner`, `_isEndpointReview`, or `_epIteration`. When a follow-up message was sent after a completed endpoint session:
- All endpoint messages stripped → no assistant response from session 1
- user(Q1) and user(Q2) merged into one → planner saw old question

### Frontend: `find()` returns FIRST planner, not current session's
`conv.messages.find(m => m._isEndpointPlanner)` found session 1's old planner. Streaming content for session 2 went to wrong object.

## Fix

### Backend: `_collapse_historical_endpoint_sessions()` 
New pre-processing step in `_transform_messages()`:
- Scans for contiguous endpoint blocks
- **Historical** blocks (followed by non-endpoint messages): collapsed to just the last worker output (stripped of `_epIteration` marker)
- **Trailing** block (current/in-progress): left as-is for the skip filter
- Role check: `m.get('role') == 'assistant'` to distinguish workers from critics (both have `_epIteration`)

### Frontend: Reverse find for planners
All `conv.messages.find(m => m._isEndpointPlanner)` changed to `[...conv.messages].reverse().find(m => m._isEndpointPlanner)` to get the current session's planner, not session 1's.

### Frontend: `buildApiMessages` now also collapses
Added `_collapseHistoricalEndpointSessions()` JS helper + `_epIteration` skip in frontend buildApiMessages.

### Files Changed
- `lib/tasks_pkg/conv_message_builder.py` — `_collapse_historical_endpoint_sessions()`, skip `_epIteration`
- `static/js/main.js` — `_collapseHistoricalEndpointSessions()`, skip `_epIteration`
- `static/js/ui.js` — 4× reverse find for `_isEndpointPlanner`
- `tests/test_conv_message_builder.py` — 5 regression tests

