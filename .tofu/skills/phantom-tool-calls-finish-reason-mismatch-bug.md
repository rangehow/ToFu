---
name: phantom-tool-calls-finish-reason-mismatch-bug
description: Bug fix: finish_reason=tool_calls with 0 actual tool_calls causes spurious "loop ended unexpectedly" error
enabled: true
tags: [python, bug-fix, tool-calls, phantom, finish-reason, stream-handler, orchestrator]
created: 2026-04-16T04:52:09Z
updated: 2026-04-16T04:52:09Z
---

# Phantom Tool Calls Finish Reason Mismatch

## Bug
API returns `finish_reason=tool_calls` but the phantom/spurious filter in `llm_client.py` removes ALL
tool calls (e.g. all were `antml:*` prefix artifacts). The `analyse_stream_result` in `stream_handler.py`
enters the "Normal exit — no tool calls" branch and breaks the loop, but does NOT update `last_finish_reason`.

After the loop, `_finalize_and_emit_done` in `orchestrator.py` (line ~215) checks:
```python
elif last_finish_reason in ('tool_use', 'tool_calls') and not task.get('error'):
    last_finish_reason = 'error'
    task['error'] = 'Model requested tool calls but the loop ended unexpectedly.'
```
This triggers because `last_finish_reason` is still `'tool_calls'` from the API.

## Fix
In `stream_handler.py`, the "Normal exit" branch now normalizes `finish_reason` to `'stop'`
when it's `tool_calls` or `tool_use` but `assistant_msg` has no actual tool_calls:

```python
if last_finish_reason in ('tool_calls', 'tool_use'):
    logger.warning(...)
    result['last_finish_reason'] = 'stop'
```

## Log Signature
```
stream_llm_response complete: finish_reason=tool_calls ... tool_calls=0
...
Persisting result: ... finishReason=error ... error=Model requested tool calls but the loop ended unexpectedly.
```

## Root Cause
The phantom/spurious tool call filter in `llm_client.py` (around line 2035) can remove ALL accumulated
tool calls if they all match `_INTERNAL_TOOL_PREFIXES` (e.g. `antml:`, `anthropic.`, `__`). The
`finish_reason` from the API is set before filtering, creating the mismatch.

