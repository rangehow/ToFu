---
name: endpoint-ghost-assistant-non-empty-content-race
description: Endpoint mode planner/worker rendering bugs: stale planner streaming-msg eaten by worker, duplicate worker on SSE reconnection, and prior fixes (user-replacement, planner-to-agent flash, force-refresh duplicate)
enabled: true
tags: [python, javascript, endpoint, ghost-message, race-condition, bug-fix, sync]
created: 2026-04-02T08:16:30Z
updated: 2026-04-03T01:42:45Z
---

# Endpoint Planner — Architecture & Bug Fixes

## Problem v6: Planner eaten by worker + Duplicate worker on reconnection

### Symptom 1: Planner not visible ("user-agent" only)
During the initial SSE stream, the planner bubble shows "Planner" while streaming, but after
`endpoint_planner_done` fires, if `activeConvId !== convId` at that moment (user switched convs),
the DOM finalization is skipped — `streaming-msg` stays. Then `endpoint_iteration(working)` checks
`document.getElementById("streaming-msg")` — finds it exists → skips creating the worker message
AND buffer reset. Worker deltas stream into the planner's old buffer → planner bubble shows worker
content, planner is never visible as a separate message.

### Root Cause 1
`endpoint_iteration(working)` handler used `if (!existingSm)` to decide whether to create a new
worker message/buffer. If `streaming-msg` existed (stale planner), it skipped everything — no new
assistant msg, no buffer reset, no DOM element.

### Fix 1
Before the `if (!existingSm)` check, detect if the existing `streaming-msg` has `ep-planner-msg`
class. If so, finalize the planner element (render it as a static message) and set `existingSm = null`
to force creation of the worker message/buffer/DOM.

### Symptom 2: Duplicate worker on SSE reconnection
After page refresh, the SSE `state` handler's full reconnection path does `renderChat(conv)` (renders
ALL messages including the in-progress worker as a static element), then creates a NEW `streaming-msg`
for the same worker → duplicate "dead agent" + "live agent".

### Root Cause 2
`renderChat(conv)` renders all `conv.messages` including the in-progress worker/critic as a static
`msg-N` element. Then the streaming-msg is created for the same message → two copies in DOM.

### Fix 2
After `renderChat(conv)`, remove the last rendered element (`msg-${conv.messages.length - 1}`) before
creating the streaming-msg. This ensures only the streaming version is shown.

## Previous Fixes (still relevant)

### v5: Force-refresh "Agent" instead of "Planner" + Duplicate Planner Bubbles
- connectToTask detects planner phase for reconnection avatar/role
- State reconnection always uses fast path during planning (skip renderChat)
- Consistent _isEndpointPlanner filter in base-message filter

### v3: User Message Replacement
Planner output REPLACES the original user message in working messages.

### v4: Planner-to-Agent Flash Race Condition
Set `task['_endpoint_phase'] = 'planning'` and `task['_endpoint_iteration'] = 0` BEFORE starting thread.

### Ghost assistant placeholder stripping
`_sync_endpoint_turns_to_conversation` strips trailing unmarked assistants.

## Architecture

### LLM Working Messages:
```
system → user(planner_content)  [first worker turn]
system → user(planner_content) → assistant(worker) → user(critic_feedback) → ...
```

### Frontend Display:
```
user(original) → planner(assistant) → agent → critic → agent → critic → ...
```

### DB/Persistence:
```
user(original) → assistant(_isEndpointPlanner) → assistant(_epIteration=1)
→ user(_isEndpointReview) → assistant(_epIteration=2) → ...
```

