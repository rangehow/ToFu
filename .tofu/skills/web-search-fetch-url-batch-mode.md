---
name: web-search-fetch-url-batch-mode
description: Batch mode for web_search (queries array) and fetch_url (urls array) tools
enabled: true
tags: [python, tools, batch-mode, web_search, fetch_url, concurrent, performance]
created: 2026-04-14T02:30:04Z
updated: 2026-04-14T02:30:04Z
---

# web_search and fetch_url Batch Mode

## Schema
- **web_search**: `queries` array of `{query}` objects (max 5), all run concurrently
- **fetch_url**: `urls` array of `{url}` objects (max 10), all run concurrently
- Both tools still support single-mode (`query`/`url` params) for backward compat

## Files Changed
- `lib/tools/search.py` — Added `queries`/`urls` params to SEARCH_TOOL_MULTI and FETCH_URL_TOOL
- `lib/tasks_pkg/handlers/search.py` — `_handle_web_search_batch()` and `_handle_fetch_url_batch()` functions
  - web_search batch uses ThreadPoolExecutor(max_workers=5) for concurrent searches
  - fetch_url batch uses ThreadPoolExecutor(max_workers=8) for concurrent fetches, with 300K char budget
  - Both preserve ordered results and build combined display_results for frontend
- `lib/tasks_pkg/tool_display.py` — Updated `_tool_display_web_search` and `_tool_display_fetch_url` for batch display strings
- `lib/tasks_pkg/streaming_tool_executor.py` — Lightweight concurrent prefetch for batch mode (no SSE side effects)
- `lib/tasks_pkg/tool_dispatch.py` — Updated `_build_cache_hit_meta` for batch web_search

## Frontend
- No frontend changes needed — the existing `_renderUnifiedToolLine` handles `web_search`/`fetch_url` with results arrays generically
- Batch results are merged into one `results` array, rendered in the collapsible panel
- Display query shows "3 searches: ..." or "📄 3 URLs: ..." for batch mode

## Dedup Cache
- Works automatically — `_make_cache_key` serializes full `fn_args` (including `queries`/`urls` array) as JSON

