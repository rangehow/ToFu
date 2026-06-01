---
name: conv-message-builder-structured-tool-history
description: conv_message_builder now expands stored toolRounds into proper assistant(tool_calls)+tool messages — unifies debug panel and live request
enabled: true
tags: [conv_message_builder, debug-messages, toolRounds, tool_calls, continue, architecture]
created: 2026-04-20T11:01:41Z
updated: 2026-04-20T11:01:41Z
---

# `conv_message_builder` — structured tool history expansion

## What changed (2026-04-20)

`lib/tasks_pkg/conv_message_builder.py::_build_assistant_message` was
replaced with `_build_assistant_messages` (plural) that can return a
LIST of messages instead of a single one.

When a stored assistant message has `toolRounds` with complete info
(`toolCallId` + `toolName` + `status == 'done'` + `toolContent` non-None),
it's expanded into proper OpenAI-style structured messages:

```
assistant(content=assistantContent, tool_calls=[...])  # one per batch
tool(tool_call_id=..., content=toolContent)
tool(tool_call_id=..., content=toolContent)
...
assistant(content=final_content)   # final answer text appended last
```

Batch = contiguous rounds with same `llmRound` (preferred), or
`roundNum` gap ≤ 1 for legacy data without `llmRound`.

Fallback path (legacy / incomplete rounds) still emits the old
summary-JSON placeholder in `content`.

## Why

The debug-messages endpoint (`/api/conversations/<id>/debug-messages`)
and the actual `/api/chat/start` request both go through
`build_api_messages_from_db` → `_transform_messages`. Previously the
builder collapsed all tool info to a single lossy JSON
`[{name, args}, ...]` string. So:

- Debug panel showed `{"name": "...", "query": "..."}` — no result content.
- After a Continue click (which rewrites the DB message with
  `content=preservedContent` often empty), the debug panel showed
  nothing useful, *even though* the toolRounds still had full content.
- The live LLM request got proper tool history only via
  `inject_tool_history()` in `lib/tasks_pkg/message_builder.py` —
  from the separate `toolHistory` config payload — so debug and real
  request diverged.

Now both paths agree: the debug preview matches what the LLM actually sees.

## Related code paths

- `routes/conversations.py::debug_messages` — unchanged, benefits automatically
- `routes/chat.py::chat_start` — unchanged
- `routes/chat.py::branch start` — `build_branch_api_messages` unchanged
- `routes/endpoint.py::endpoint_start` — unchanged
- Continue flow (`main.js::continueAssistant`) still sends
  `excludeLast:true` + `toolHistory` config; the excluded assistant
  isn't emitted structured, and `inject_tool_history` handles the
  checkpoint — no double-injection.
- `_merge_consecutive_same_role` was hardened to NEVER merge across
  `tool_calls` / `tool_call_id` messages.
- `cache_tracking.sort_tool_results` already handles `role=='tool'`
  messages so it picks up the new structured format automatically.

## Verification

See inline test in commit: T1 plain / T2 two-batch expansion /
T3 missing toolContent fallback / T4 legacy no toolCallId fallback.

