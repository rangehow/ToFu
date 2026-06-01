---
name: sse-delta-tool-calls-null-guard
description: Streaming SSE delta parsers must guard tool_calls with `or []` — providers can send {"tool_calls": null}
enabled: true
tags: [llm_client, streaming, bug-pattern, glm]
created: 2026-05-13T05:17:11Z
updated: 2026-05-13T05:17:11Z
---

# SSE delta parsing: `{"tool_calls": null}` crashes `if 'tool_calls' in delta`

## The trap
Some OpenAI-compat gateways (observed on GLM5.1 in 2026-05-13 task
d551dd42, conv mp2qryrrnmbhtn) emit a final streaming chunk shaped like:

```json
{"choices":[{"delta":{"tool_calls":null,"content":""},"finish_reason":"stop"}]}
```

The key `tool_calls` IS present in the delta dict but the value is
`None`. The classic guard

```python
if 'tool_calls' in delta:
    for tc in delta['tool_calls']:   # TypeError: NoneType not iterable
```

passes the membership check then crashes on iteration. dispatch_stream
treats this as a per-slot error, excludes the (key, model) pair, retries
on the other GLM slots — which all emit the same shape — and eventually
re-raises. The visible symptom is "Reactive compact retry also failed:
'NoneType' object is not iterable" obscuring the original error.

## The fix
Use `delta.get('tool_calls') or []` everywhere SSE deltas are parsed.
Two call sites in `lib/llm_client.py`:
- `_stream_chat_once` main path (~line 1843) — `_tc_list = delta.get('tool_calls') or []; if _tc_list: for tc in _tc_list:`
- `_stream_chat_once` t_chunk-translated path (~line 1644) — `for tc in (delta.get('tool_calls') or []):`

Same pattern likely applies to `'reasoning_content' in delta` and
similar guards if a provider ever sends those as null.

## How to spot this in logs
- error.log shows "TypeError: 'NoneType' object is not iterable" with
  traceback into `for tc in delta['tool_calls']` AT `lib/llm_client.py`.
- dispatch_stream then logs "NO SLOT available on attempt 3/3" with
  `exclude_pairs={(key, model), (key2, model), (key3, model)}` — all
  three GLM slots excluded for the same reason.
- The original FATAL may be a DIFFERENT error (e.g. PromptTooLongError)
  because it's the original-call exception that re-propagates after the
  reactive-compact retry blew up on the null tool_calls.

