---
name: continue-lossless-per-provider-matrix
description: Claude thinking-signature loss: REAL path is OpenAI-compat reasoning_details (not Anthropic protocol); capture+rebuild in _sse_core/body.py
enabled: true
tags: [continue, resume, claude, gemini, thinking, thought-signature, capability-probe]
created: 2026-04-21T04:12:29Z
updated: 2026-06-05T08:48:33Z
---

# Continue Lossless — Per-Provider Capability Matrix

## CRITICAL: aws.claude-opus-4.x uses the OpenAI-compat path, NOT api_protocol='anthropic'

The sankuai/Meituan provider (`base_url: https://aigc.sankuai.com/v1/openai/native`) has **no `protocol` field** → `slot.protocol=''` → `api_protocol='openai'`. So Claude models there go through `/chat/completions`, NOT the Anthropic Messages API. The `AnthropicSSETranslator` path only fires when a provider explicitly sets `protocol: anthropic`.

Over this OpenAI-compat wire the gateway uses **OpenRouter-style `reasoning_details`**:
- thinking text deltas: `delta.reasoning_content` AND/OR `delta.reasoning_details:[{"type":"thinking","thinking":"…"}]`
- opaque signature (once per turn): `delta.reasoning_details:[{"type":"thinking","signature":"Et…"}]`

This was the cause of the recurring `message_builder.py:259` warning "thinking but NO signature": text was captured but the signature in `reasoning_details` was dropped (`_handle_delta` only mined `d.get('text')`, never `thinking` or `signature`). Diagnose via `logs/raw_sse_anomaly.log` (grep `reasoning_details`).

## The full fix (2026-06-05)

CAPTURE (`lib/llm/_sse_core.py::SSEAccumulator._handle_delta`): from `reasoning_details` list, harvest text via `d.get('thinking') or d.get('text')` AND accumulate `self.thinking_signature += d['signature']`. `finalize()` attaches `msg['thinking_signature']`. (Also kept the Anthropic-protocol `signature_delta` capture + flat `delta.thinking_signature`.)

REPLAY (`lib/llm/body.py::_inject_claude_reasoning_details`, called in `build_body`): for Claude, when an assistant msg has BOTH `reasoning_content`+`thinking_signature` but no `reasoning_details`, rebuild `reasoning_details:[{type:thinking,thinking,signature}]`. The gateway requires the signed block back in THIS shape; the flat `thinking_signature` field alone is ignored. Idempotent; Claude-only.

IN-LOOP (`lib/tasks_pkg/orchestrator.py:~1598`): the live `clean_msg` appended each tool round now copies `thinking_signature` too (was only copying `reasoning_content`) — else every in-loop turn after the first was lossy.

WHITELIST (`lib.llm_sanitize._API_MESSAGE_FIELDS`): added `reasoning_details` alongside `reasoning_content` + `thinking_signature` so the rebuilt block survives `_strip_non_api_fields`.

## Persistence chain (unchanged, already worked once msg carries the field)

- `tool_dispatch.py::parse_tool_calls` reads `assistant_msg.get('thinking_signature')` → `round_entry['thinkingSignature']`.
- `static/js/main/main_toolbar_ui.js::_buildToolHistoryRound` → `toolHistory[i].thinkingSignature`.
- `message_builder.py::inject_tool_history` (Continue) and `conv_message_builder.py::_reconstruct_tool_call_messages` (DB history) re-emit flat `reasoning_content`+`thinking_signature`; `build_body` then rebuilds `reasoning_details`.

## Capability Probes (lib/model_info.py)

- `model_requires_thinking_signature_replay(m)` → True for Claude.
- `model_requires_thought_signature_on_tool_calls(m)` → True for Gemini (`extra_content.google.thought_signature`).
- `model_supports_assistant_prefill(m)` → False only for Claude.

## Hard Limits

- Anthropic rejects a trailing assistant prefill — never emulate. `contentPrefix` is task bookkeeping only.
- Free-form text between tool batches against Claude can't be made lossless; tool + signed-thinking continuity now IS lossless on BOTH paths.
- Plain OpenAI/o1/o3 strip reasoning_content server-side — don't send.

## Tests

`tests/test_continue_lossless.py`: `TestOpenAICompatReasoningDetails` (PRODUCTION path — capture from reasoning_details, build_body rebuild/skip), `TestAnthropicSignatureCapture` + `TestAnthropicOutboundReplay` (protocol=anthropic path), plus original injection cases. Env pytest is BROKEN (rogue editable `pytest-5.2.4.dev` on sys.path → `TypeError: required field "lineno" missing`); run by importing the module and calling test methods directly.

