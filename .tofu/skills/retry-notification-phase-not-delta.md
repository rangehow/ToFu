---
name: retry-notification-phase-not-delta
description: System retry/fallback/compact notifications must use phase events (transient UI), never delta events (which permanently pollute assistantMsg.content)
enabled: true
tags: [python, sse, convention, retry, ui]
created: 2026-04-04T05:41:49Z
updated: 2026-04-04T05:41:49Z
---

# Retry/Fallback Notification Convention: Phase Events, Not Deltas

## Rule
All system-level notifications (retries, fallbacks, compaction, auto-learned limits)
MUST use `phase` events, NEVER `delta` events.

### Why
- `delta` events with `content` are permanently appended to `assistantMsg.content`
  on the frontend, polluting the actual assistant reply.
- `phase` events are transient UI indicators (spinning icon, status text) that
  disappear when the next phase begins or when streaming completes.

### Correct Pattern
```python
# ✅ CORRECT — transient UI notification
append_event(task, {
    'type': 'phase',
    'phase': 'retrying',
    'detail': '⚠️ 网络中断，正在自动重试 (1/2)…',
})

# ✅ CORRECT — for compaction
append_event(task, {
    'type': 'phase',
    'phase': 'compacting',
    'detail': 'Compressing context…',
})
```

### Forbidden Pattern
```python
# ❌ FORBIDDEN — pollutes assistantMsg.content permanently
append_event(task, {
    'type': 'delta',
    'content': '⚠️ Some system notification…',
})

# ❌ FORBIDDEN — double pollution (both task dict and SSE)
with task['content_lock']:
    task['content'] += notice
append_event(task, {'type': 'delta', 'content': notice})
```

### When to use `delta`
Only for actual LLM-generated content:
- `_on_content(cd)` callback in `stream_llm_response`
- `_on_thinking(td)` callback in `stream_llm_response`
- `emit_to_user` comment (intentional final content from a tool)

### Frontend Phase Handlers
The frontend already handles these phase types:
- `retrying` — shows ⟳ icon + detail text + dots animation
- `compacting` — shows detail text + dots animation
- `llm_thinking` — shows spinner + round info
- `tool_exec` — shows tool execution detail
- `thinking_active` — shows brain emoji during thinking token generation

### Persistent Info in Done Event
If information needs to survive (e.g., which fallback model was used),
put it in the `done` event fields instead:
- `fallbackModel` / `fallbackFrom` — shown as badge in finish tags
- `apiRounds` — shown in cost breakdown
- `error` — shown as error banner

## Files Modified (2026-04-04)
- `lib/tasks_pkg/stream_handler.py` — premature close retry: delta → phase
- `lib/tasks_pkg/llm_fallback.py` — reactive compact + model fallback: delta → phase
- `lib/tasks_pkg/manager.py` — model limit auto-learned: delta → phase

