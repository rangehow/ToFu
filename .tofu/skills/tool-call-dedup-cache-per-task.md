---
name: tool-call-dedup-cache-per-task
description: Per-task dedup cache for idempotent tool calls (fetch_url, web_search, read_files, etc.) in tool_dispatch.py — prevents redundant network requests when LLM repeats the same tool call within a single task; invalidated on write ops
enabled: true
tags: [python, tool-dispatch, dedup, cache, performance, fetch_url, web_search]
created: 2026-03-30T10:35:21Z
updated: 2026-03-30T10:35:21Z
---

# Tool Call Dedup Cache

## Problem
LLMs frequently repeat identical tool calls (especially `fetch_url` and `web_search`) within a single task, even when the results are already in the conversation context. This wastes time on redundant network requests and bloats context.

## Solution
Per-task dedup cache in `lib/tasks_pkg/tool_dispatch.py`:

1. **Cache scope**: `task['_tool_result_cache']` — auto-created, lives for one task only
2. **Idempotent tools**: `_IDEMPOTENT_TOOLS` frozenset (fetch_url, web_search, read_files, list_dir, grep_search, find_files, browser_*, check_error_logs, etc.)
3. **Cache key**: `fn_name::json.dumps(fn_args, sort_keys=True)` — deterministic regardless of arg ordering
4. **Cache hit**: Returns cached result with `[Note: cached result...]` prefix so the model knows
5. **Cache invalidation**: `_invalidate_project_cache()` removes project-tool cache entries after write_file/apply_diff/code_exec/bash_exec
6. **UI feedback**: Cache hits show "♻️ cached" badge in tool result display

## Key files
- `lib/tasks_pkg/tool_dispatch.py` — all dedup logic lives here

