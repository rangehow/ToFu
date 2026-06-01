---
name: empty-user-content-claude-http-400
description: Bug fix: empty user/tool message content triggers Anthropic HTTP 400; guard added in lib/llm_sanitize.py + build_body()
enabled: true
tags: [claude, anthropic, bugfix, sanitize]
created: 2026-05-22T01:46:00Z
updated: 2026-05-22T01:46:00Z
---

# Empty user/tool content → Claude HTTP 400

## Bug Pattern
Claude/Bedrock rejects requests where any `role: "user"` (or `role: "tool"`) message has empty content:

```
HTTP 400 messages.N: user messages must have non-empty content
```

Once such a message lands in `server_message_store`, every subsequent turn fails at round 1
on the same conversation — the user sees a permanently-wedged conversation.

## Sources of empty content
- non-vision-model image stripping in `lib/llm/body.py::build_body` collapsing `content` to `''`
  when the only blocks were `image_url`s
- failed upload / transcription saving a blank user turn
- compaction producing an empty placeholder
- `tool` messages whose result was an empty string

## Fix (sibling to the orphan-tool-call / trailing-assistant guards)
`_fix_empty_user_messages(messages)` in `lib/llm_sanitize.py`:
- scans `role in (user, tool)` messages
- treats `None`, `''`/whitespace string, `[]`, and "all-empty text blocks" as empty
- replaces empty content with a placeholder string `"[empty message]"` / `"[empty tool result]"`
  (does NOT delete — deleting would break tool_call_id adjacency)
- mutates in-place, returns the list for chaining

Wired into `lib/llm/body.py::build_body` AFTER the non-vision image-stripping step
(otherwise we'd miss user messages whose images were just stripped to `''`).
Re-exported from `lib/llm/__init__.py`.

## Why a placeholder, not deletion
Anthropic also requires tool_use → tool_result adjacency. If we deleted an empty
`role: tool` message we'd orphan the assistant's preceding `tool_calls`, triggering
a different HTTP 400. A non-empty placeholder satisfies both invariants.

