---
name: orphaned-tool-use-abort-http-400
description: Bug fix: aborted task saves orphaned tool_use without tool_result → Claude HTTP 400 on next turn
enabled: true
tags: [python, bug-fix, tool-calls, abort, http-400, claude, anthropic, server-message-store, orchestrator]
created: 2026-04-13T05:23:15Z
updated: 2026-04-13T06:23:08Z
---

# Orphaned tool_use blocks causing HTTP 400 on Claude

## Bug Pattern
When a task is aborted mid-tool-call (user clicks Stop after LLM returns tool_calls but before tools execute), the orchestrator:
1. Appends `{role: assistant, tool_calls: [...]}` to `messages` (line ~879 in orchestrator.py)
2. Checks `task['aborted']` and breaks the loop
3. Saves messages to `server_message_store` via `_save_messages_to_store()`
4. The stored messages now end with `tool_use` blocks but NO matching `tool_result`

On the next turn, `rebuild_messages_with_history()` replays these broken messages, and Claude API rejects with:
```
HTTP 400: "messages.52: `tool_use` ids were found without tool_result blocks immediately after"
```

## Four-Layer Fix (layers 1-3 applied 2026-04-13, layer 4 added 2026-04-13)

### Layer 1: orchestrator.py — abort handler
When aborting before tool execution, pop the trailing assistant message with `tool_calls` from `messages`. If it had content alongside, re-add just the content.

### Layer 2: server_message_store.py — save_messages & rebuild
- In `save_messages()`: strip trailing orphaned tool_calls before storing
- In `rebuild_messages_with_history()`: strip trailing orphaned tool_calls before adding new user message

### Layer 3: llm_client.py — build_body (defence-in-depth)
`_fix_orphaned_tool_calls()` scans ALL messages for tool_call IDs without matching tool_result IDs and strips them. This catches cases from frontend-constructed messages too.

### Layer 4: Anthropic adjacency validation (2026-04-13)
`_fix_tool_call_adjacency()` — validates that tool_result messages are **immediately** after their corresponding assistant tool_calls message. Anthropic requires adjacency, not just matching IDs. If results are found elsewhere in the conversation, they're reordered; if genuinely missing, tool_calls are stripped.

Also added: removal of orphaned tool_results (role=tool messages without any matching tool_call in the conversation).

## Key insight
- The tool_call_id matching is critical: tool_calls have `.id`, tool messages have `.tool_call_id`. Both must match for the API to accept the sequence.
- Anthropic requires tool_result to be **immediately** after the tool_use message — not just "somewhere" in the conversation.
- Even when abort handler strips the last tool_calls, mid-conversation orphans can exist from complex abort timing (e.g., multi-round execution where abort arrives between tool execution and the next LLM call).

