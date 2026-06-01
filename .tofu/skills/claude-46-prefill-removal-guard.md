---
name: claude-46-prefill-removal-guard
description: Claude 4.6 breaking change: assistant message prefill removed — messages must NOT end with role=assistant or API returns HTTP 400; defence-in-depth guard in build_body() and dispatch_stream()
enabled: true
tags: [python, anthropic, claude, breaking-change, prefill, http-400, build-body, dispatch]
created: 2026-03-30T09:30:33Z
updated: 2026-03-30T09:30:33Z
---

# Claude 4.6 Assistant Message Prefill Removal

## Breaking Change
Claude 4.6 (Opus and Sonnet, released ~Jan 2026) removed support for "assistant message prefill".
This means API requests **must NOT end with a `role: "assistant"` message** — the conversation
must end with `user` or `tool`.

This affects ALL Claude 4.6 deployments: AWS Bedrock, Vertex AI, and the direct API.

**Error:** HTTP 400:
```json
{"type":"error","error":{"type":"invalid_request_error",
  "message":"This model does not support assistant message prefill. The conversation must end with a user message."}}
```

## Root Causes (why messages end with assistant)
- Frontend `buildApiMessages()` slices off the last message (empty assistant placeholder), but if there are two consecutive assistant messages, the second-to-last is still assistant
- Orphan task recovery adds an assistant placeholder that persists
- Premature stream close leaves partial assistant content in history
- Compaction edge cases after message mutation

## Fix: Defence-in-Depth Guard
Added `_strip_trailing_assistant_for_claude()` in two places:

1. **`lib/llm_client.py` → `build_body()`** — called for ALL code paths (orchestrator, swarm, fallback, compaction, etc.)
2. **`lib/llm_dispatch/api.py` → `dispatch_stream()`** — for pre-built bodies where model is swapped from non-Claude to Claude by the dispatcher

The guard:
- Strips empty trailing assistant messages (most common case)
- Strips orphaned assistant messages with tool_calls but no tool results
- Converts non-empty trailing assistant content to a user context message (last resort)

## References
- https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-6#prefill-removal
- Multiple open-source projects hit this same issue (opencode, strands-agents, deepagents, etc.)

