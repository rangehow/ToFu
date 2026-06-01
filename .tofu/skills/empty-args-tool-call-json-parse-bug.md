---
name: empty-args-tool-call-phantom-and-parse-bug
description: Bug fix: LLM phantom tool calls with empty arguments — filtered only when SAME-NAME call with real args exists (prevents dropping legitimate no-arg tools like check_error_logs)
enabled: true
tags: [python, debugging, json, tool-call, llm-client, streaming, phantom]
created: 2026-03-18T15:45:11Z
updated: 2026-04-03T00:25:27Z
---

# Empty Arguments Tool Call — Phantom & Parse Bug

## Symptom: Phantom Tool Call
Model emits TWO tool_calls where the first has a valid name (e.g. `apply_diff`) but completely empty `arguments: ""`, and the second has the real arguments. The phantom call wastes a round.

Example:
```json
{"tool_calls": [
  {"id": "toolu_01...", "function": {"name": "apply_diff", "arguments": ""}},
  {"id": "toolu_02...", "function": {"name": "apply_diff", "arguments": "{...real...}"}}
]}
```

## Fix — Same-Name Duplicate Detection (3 layers):

### Key Insight
The filter must NOT drop legitimate no-arg tools (e.g. `check_error_logs`, `list_conversations`) when they appear alongside other tool calls. The fix: only filter empty-arg calls when **another call with the SAME function name** has real arguments — confirming the empty one is a phantom duplicate.

- `""` (empty string) = phantom (no argument deltas ever arrived)
- `"{}"` (empty JSON object) = legitimate no-arg call (model explicitly sent `{}`)

### Layer 1: `lib/llm_client.py` (post-stream filter)
Build `_names_with_args` set of function names that have non-empty arguments. Filter tool calls where `not fn_args_str.strip() and fn_name in _names_with_args`.

### Layer 2: `lib/tasks_pkg/streaming_tool_executor.py` (on_tool_call_ready)
Do NOT filter here — during streaming we can't compare against other calls that haven't arrived yet. A stray `tool_start` event for a phantom is harmless.

### Layer 3: `lib/tasks_pkg/tool_dispatch.py` (parse_tool_calls)
Build `_names_with_real_args` set before the loop. Skip tool calls where `not _raw_check and fn_name in _names_with_real_args`.

## Original Bug: Empty Arguments JSON Parse
When model sends `arguments: ""` (empty string), `json.loads("")` raises `JSONDecodeError`. Fix: `fn_args = json.loads(raw_args) if raw_args.strip() else {}` in tool_dispatch.py.
