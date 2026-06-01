---
name: continue-lossless-per-provider-matrix
description: Per-provider capability matrix for Continue resume: Claude thinking block + sig, Gemini thought_signature, Anthropic no prefill
enabled: true
tags: [continue, resume, claude, gemini, thinking, thought-signature, capability-probe]
created: 2026-04-21T04:12:29Z
updated: 2026-04-21T04:12:29Z
---

# Continue Lossless — Per-Provider Capability Matrix

For a more comprehensive writeup see `.chatui/skills/continue-checkpoint-based-resumption.md`.

## Capability Probes (lib/model_info.py)

- `model_requires_thinking_signature_replay(m)` → True for Claude. Must re-inject `reasoning_content` + `thinking_signature` on replayed assistant turn that made tool calls, else Anthropic API returns HTTP 400.
- `model_requires_thought_signature_on_tool_calls(m)` → True for Gemini. Must attach `extra_content.google.thought_signature` on each replayed tool_call, else 400.
- `model_supports_assistant_prefill(m)` → False only for Claude. Do NOT emit a trailing `role='assistant'` prefill message against Claude.

## What Flows Where

- `lib/tasks_pkg/tool_dispatch.py::parse_tool_calls` stores per-round: `thinking`, `thinkingSignature`, `extraContent`.
- `static/js/main.js::_buildToolHistoryRound` propagates them to `cfgPayload.toolHistory[i]`.
- `lib/tasks_pkg/message_builder.py::inject_tool_history` gates injection by the two probes above; unsupported providers get the plain shape only.
- `lib/tasks_pkg/conv_message_builder.py::_reconstruct_tool_call_messages` mirrors the same fields for historical DB turns.
- `lib.llm_client._API_MESSAGE_FIELDS` whitelists `thinking_signature` so `_strip_non_api_fields` preserves it.

## Hard Limits

- Anthropic Messages API rejects a trailing `assistant` prefill — we do NOT emulate. `contentPrefix` is only used as `task['content']` bookkeeping, never as a message.
- Free-form text between/after tool batches against Claude cannot be made lossless. Tool + thinking-block continuity is lossless.
- OpenAI-compat APIs silently strip `reasoning_content` server-side (o1/o3 especially); don't bother sending it.

## Tests

`tests/test_continue_lossless.py` — 21 cases covering OpenAI plain path, Claude thinking-block round-trip, Gemini thought_signature round-trip, edge cases (missing signature, legacy rows, empty history).

