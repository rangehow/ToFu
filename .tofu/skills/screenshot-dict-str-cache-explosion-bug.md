---
name: screenshot-dict-str-cache-explosion-bug
description: Bug fix: __screenshot__ dicts str()-ified in dedup/prefetch cache → 843K base64 dumped as plain text in context, blowing 1M token limit
enabled: true
tags: [bug-fix, screenshot, cache, base64, token-explosion, context-window, streaming-executor, dedup]
created: 2026-04-13T05:06:32Z
updated: 2026-04-13T05:06:32Z
---

# Screenshot Dict Cache Explosion Bug (2026-04-13)

## Root Cause
When `read_files` reads a PNG/image via absolute path, it returns a `__screenshot__` protocol dict containing base64-encoded image data. This dict flows through two cache paths:

### Path 1: Streaming Tool Executor (`streaming_tool_executor.py`)
- `_execute_one()` calls `execute_tool('read_files', ...)` which returns a `{'__batch_images__': {0: {__screenshot__: ...}}, '_text_content': '...'}` dict
- `inject_into_cache()` did `cache[cache_key] = (str(content), ...)` — converting the dict to a massive Python repr string
- `len(content)` on a dict with 2 keys logged "2 chars" (misleading — it's the key count, not content size!)

### Path 2: Dedup Cache Hit (`tool_dispatch.py`)
- `dedup_content = cached_content if isinstance(cached_content, str) else str(cached_content)` — also stringified the dict
- Post-phase check `isinstance(tool_content, dict) and tool_content.get('__screenshot__')` FAILED because it's now a string
- The 843K string (632KB PNG → base64 → str()) went through as plain text tool result
- Injected into conversation messages, consuming ~1M tokens → API 400 prompt too long

## Fix
1. **streaming_tool_executor.py**: Added `_normalize_image_result()` to convert `__batch_images__` to `__screenshot__` dict, and `_prepare_cache_value()` to preserve image dicts as-is in cache
2. **tool_dispatch.py**: Added `__screenshot__` check before `str()` conversion in dedup cache hit path
3. Fixed log message to not materialize massive `str()` just for logging

## Key Pattern
Any code path that does `str(content)` on tool results MUST check for `__screenshot__` dicts first — they contain massive base64 data that should never be stringified.

