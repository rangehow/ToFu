---
name: gemini-thought-signature-openai-compat
description: Gemini 3.x thought_signature handling in OpenAI-compatible format — capture extra_content from tool_call deltas to avoid HTTP 400
enabled: true
tags: [gemini, thought-signature, tool-calls, openai-compat, bug-fix]
created: 2026-04-01T11:47:22Z
updated: 2026-04-01T11:47:22Z
---

# Gemini 3.x Thought Signature in OpenAI-Compatible Format

## Problem
Gemini 3.x models (gemini-3-pro, gemini-3-flash, gemini-3.1-flash-lite, etc.) require
`thought_signature` to be sent back on function call parts in multi-step tool-use
conversations. Missing signatures cause **HTTP 400**:
```
Unable to submit request because function call `X` in the N. content block is missing a `thought_signature`
```

## How It Works in OpenAI-Compatible Format
In the Gemini OpenAI-compatible API (`/chat/completions`), the thought_signature appears as
`extra_content.google.thought_signature` **inside each tool_call object** in the streaming delta:

```json
{
  "choices": [{
    "delta": {
      "tool_calls": [{
        "extra_content": {
          "google": {
            "thought_signature": "CvcQAdHN2O...kYA=="
          }
        },
        "function": { "name": "run_command", "arguments": "{...}" },
        "id": "function-call-123",
        "type": "function"
      }]
    }
  }]
}
```

## Fix
1. **Capture** `extra_content` from each tool_call delta during SSE streaming
2. **Store** it in `tool_calls_acc[idx]['extra_content']` alongside `id`, `function`, `type`
3. The `extra_content` flows naturally through:
   - `stream_chat()` → assistant_msg → `msg['tool_calls']`
   - orchestrator copies `assistant_msg['tool_calls']` → `clean_msg['tool_calls']`
   - `build_body()` → `_strip_non_api_fields()` only strips top-level msg keys, not tool_call internals
4. On next round, `extra_content` is present in the request body → Gemini validates ✅

## Key Rules
- **Only the current turn's signatures are validated** (from the most recent non-functionResponse user message forward)
- For **parallel function calls**: only the first functionCall part has the signature
- For **sequential** (multi-step): each step's first functionCall has a signature
- **cross-conversation** (Continue flow): new task = new turn = fresh signatures → no issue
- **Escape hatch**: set `thought_signature` to `"skip_thought_signature_validator"` (native API only, degrades performance)

## References
- https://ai.google.dev/gemini-api/docs/thought-signatures
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures

