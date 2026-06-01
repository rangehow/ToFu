---
name: malformed-tool-name-xml-guard
description: Bug fix: MiniMax emits corrupted tool names with XML artifacts — added isalnum guard in tool_dispatch
enabled: true
tags: [python, bug-fix, tool-dispatch, minimax, xml-injection, guard]
created: 2026-04-13T12:41:34Z
updated: 2026-04-13T12:41:34Z
---

# Malformed Tool Name Guard (XML/HTML Artifacts)

## Bug
MiniMax-M2.7 sometimes emits corrupted tool names containing XML/HTML artifacts from hallucinated raw XML content, e.g.:
- `list_dir">.</parameter>\n</invoke>\n<invoke name="grep_search`

These pass through existing guards (`:` check for Anthropic artifacts, `__` prefix check for internal names) and cause:
1. `[Orchestrator] Unregistered tool` warnings in tool_display.py
2. `[Executor] Unknown tool requested` warnings in executor.py
3. Wasted round-trip sending error back to LLM

## Fix
Added a guard in `lib/tasks_pkg/tool_dispatch.py` after the colon/underscore checks:
```python
if not fn_name.replace('_', '').replace('-', '').isalnum():
    logger.warning('[Task %s] Skipping malformed tool name (non-alphanumeric): %.80s', tid, fn_name)
    continue
```

Valid tool names (including MCP `mcp__github__search_code`) only contain alphanumeric, underscore, and hyphen characters, so this catches any names with `<`, `>`, `"`, `(`, `)`, newlines, etc.

