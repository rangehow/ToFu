---
name: gemini-thought-signature-openai-compat
description: Gemini 3.x thought_signature: capture extra_content from streaming, inject skip_thought_signature_validator on cross-model fallback tool_calls in build_body/_inject_gemini_thought_signatures
enabled: true
tags: [gemini, thought-signature, tool-calls, openai-compat, bug-fix]
created: 2026-04-01T11:47:22Z
updated: 2026-06-03T07:01:35Z
---

# Gemini 3.x Thought Signature in OpenAI-Compatible Format

## Problem
Gemini 3.x models require `thought_signature` on function call parts in multi-step tool-use
conversations. Missing signatures cause **HTTP 400**:
```
Function call is missing a thought_signature in functionCall parts
```

### Two separate scenarios:
1. **Same-model multi-round**: Gemini produces tool_calls with `extra_content.google.thought_signature` during streaming. If we don't capture and echo it back, next request 400s.
2. **Cross-model fallback**: Conversation built with non-Gemini model (e.g. Claude) has tool_calls without `thought_signature`. When dispatch falls back to Gemini, ALL historical tool_calls are validated → 400.

## Fix — Scenario 1 (capture during streaming)
In `lib/llm/_sse_core.py`, `SSEAccumulator` captures `extra_content` from each tool_call delta:
```python
if tc.get('extra_content'):
    self.tool_calls_acc[idx]['extra_content'] = tc['extra_content']
```
This flows through `stream_chat()` → assistant_msg → `msg['tool_calls']` → `build_body()` → next request.

## Fix — Scenario 2 (cross-model fallback)
`lib/llm/body.py::_inject_gemini_thought_signatures()` — called in `build_body()` after message cleanup. For each assistant message with `tool_calls` that lack `thought_signature`, injects `'skip_thought_signature_validator'` on the first tool_call.

Per Google's official FAQ: set `thought_signature` to `"skip_thought_signature_validator"` or `"context_engineering_is_the_way_to_go"` to skip validation for cross-model tool_calls.

Also wired into `dispatch_stream()` pre-built body path (alongside the Claude prefill guard).

## Key Rules
- **Only the first functionCall** per step needs the signature (parallel calls: only first)
- **Sequential** (multi-step): each step's first functionCall has a signature
- Gemini validates from the most recent non-functionResponse user message forward
- `skip_thought_signature_validator` works in both native and OpenAI-compat format

## Files
- `lib/llm/body.py` — `_inject_gemini_thought_signatures()`, `_GEMINI_SKIP_SIGNATURE`
- `lib/llm/_sse_core.py` — `extra_content` capture in SSEAccumulator
- `lib/llm_dispatch/api.py` — pre-built body path injection
- `lib/tasks_pkg/tool_dispatch.py` — preserves `extra_content` in tool round events
- `lib/tasks_pkg/message_builder.py` — re-injects `extra_content` on Continue/replay

