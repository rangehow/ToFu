---
name: antml-thinking-spurious-tool-call-bug
description: Bug fix: Anthropic proxy leaks 'antml:thinking' as spurious tool_call delta in OpenAI-compatible SSE stream, rendering as fake search round in UI — filter by prefix in llm_client + colon guard in tool_dispatch
enabled: true
tags: [python, anthropic, streaming, tool-call, bug-fix, proxy, antml, llm-client]
created: 2026-03-24T09:50:14Z
updated: 2026-03-24T09:50:14Z
---

# antml:thinking Spurious Tool Call Bug

## Symptom
A search-round block appears in the UI showing:
```
⚡ Antml:thinking: 🔧 antml:thinking
```
with a pulsing animation, as if the model is executing a tool called "antml:thinking".

## Root Cause
When using Claude/Anthropic models via an OpenAI-compatible proxy, the proxy sometimes leaks
internal Anthropic streaming tokens (`antml:thinking`, `antml:invocation`) as `tool_call` deltas
in the SSE stream. The `antml:` prefix is Anthropic's internal XML namespace for extended thinking blocks.

These fake tool calls flow through:
1. `llm_client.py` → accumulated in `tool_calls_acc` without validation
2. `tool_dispatch.py` → `parse_tool_calls` processes them as real tools
3. `tool_display.py` → `_tool_display_generic` catch-all creates a round entry with `🔧 antml:thinking`
4. Frontend `_getToolDisplay` → generic fallback renders `⚡` icon with gray color

## Fix (Defense in Depth)

### Layer 1: `lib/llm_client.py` (after stream loop, before building assistant message)
Filter out tool calls whose function names start with known internal prefixes:
```python
_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')
if tool_calls_acc:
    _filtered = {}
    for idx, tc_entry in tool_calls_acc.items():
        fn_name = tc_entry['function']['name']
        if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
            logger.debug('Filtering spurious internal tool call: %s', fn_name)
            continue
        _filtered[idx] = tc_entry
    tool_calls_acc = _filtered
```

### Layer 2: `lib/tasks_pkg/tool_dispatch.py` (`parse_tool_calls`)
Skip tool calls with colons or `__` prefix (no legitimate tool name uses these):
```python
if ':' in fn_name or fn_name.startswith('__'):
    logger.warning('[Task %s] Skipping spurious/internal tool call name: %s', tid, fn_name)
    continue
```

## Key Insight
No real tool name in the system contains `:` — all tool names use `snake_case` (e.g. `web_search`, `fetch_url`, `bash_exec`, `browser_click`). The colon is a reliable signal for filtering internal artifacts.

