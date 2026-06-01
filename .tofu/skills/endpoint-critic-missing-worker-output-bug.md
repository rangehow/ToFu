---
name: endpoint-critic-missing-worker-output-bug
description: Bug fix: run_task's while-loop only appends assistant reply to messages when tool_calls present — non-tool-call completions leave assistant reply missing from messages, breaking endpoint critic
enabled: true
tags: [python, bug-fix, endpoint, critic, messages, stale-state, orchestrator]
created: 2026-03-29T04:00:40Z
updated: 2026-04-03T10:39:14Z
---

# Endpoint Critic Missing Worker Output Bug

## Bug (Two-part)

### Part 1 (fixed earlier): No write-back
`run_task()` creates a LOCAL copy of messages (`messages = list(task['messages'])`) but never wrote it back. Fixed by adding `task['messages'] = messages` after the loop.

### Part 2 (the real culprit): Assistant reply never appended for non-tool completions
Even after Part 1, the assistant's text reply is **only** appended to `messages` inside the tool_calls branch (line ~698: `messages.append(clean_msg)` — guarded by `assistant_msg['tool_calls']`). When the LLM returns text content **without** tool_calls (normal completion, `finish_reason="end_turn"`), `analyse_stream_result` returns `action='break'` and the loop exits WITHOUT ever appending the assistant reply.

So `task['messages']` (written back at line 820) is missing the assistant's reply.

## Impact
- `_run_single_turn()` returns `result['messages']` missing the worker's assistant reply
- In `endpoint.py`, `messages = list(turn_messages)` gets stale messages
- `_run_critic_turn` builds `critic_messages` from `worker_messages`, iterating non-system messages — assistant reply is absent
- Critic sees `['system', 'user', 'user']` instead of `['system', 'user', 'assistant', 'user']`
- Critic can't review what it can't see → bad verdicts

## Fix
After the while-loop exits, BEFORE `task['messages'] = messages`, append the assistant reply if:
1. `assistant_msg` exists
2. `assistant_msg` has NO `tool_calls` (tool_calls path already appended it)
3. There's actual content or reasoning_content

```python
if assistant_msg and not assistant_msg.get('tool_calls'):
    _final_content = assistant_msg.get('content') or ''
    _final_reasoning = assistant_msg.get('reasoning_content') or ''
    if _final_content or _final_reasoning:
        _final_assistant = {'role': 'assistant', 'content': _final_content}
        if _final_reasoning:
            _final_assistant['reasoning_content'] = _final_reasoning
        messages.append(_final_assistant)
```

## Key Files
- `lib/tasks_pkg/orchestrator.py` — `run_task()` post-loop section (line ~815)
- `lib/tasks_pkg/endpoint.py` — `run_endpoint_task()` uses `turn_messages` from `_run_single_turn`
- `lib/tasks_pkg/endpoint_review.py` — `_run_critic_turn()` builds messages from `worker_messages`
- `tests/test_endpoint_messages.py` — 23 tests validating message shapes

