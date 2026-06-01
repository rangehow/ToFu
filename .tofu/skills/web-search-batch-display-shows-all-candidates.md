---
name: web-search-batch-display-shows-all-candidates
description: Tool-call display for batch web_search/fetch_url shows ALL candidate terms, not just first 3 truncated
enabled: true
tags: [ui, tool-display, web_search, fetch_url, batch-mode]
created: 2026-04-20T09:25:58Z
updated: 2026-04-20T09:25:58Z
---

# Batch web_search / fetch_url display: show all candidates

**File**: `lib/tasks_pkg/tool_display.py`

## Issue
Original `_tool_display_web_search` / `_tool_display_fetch_url` in batch mode
(queries/urls array) truncated aggressively:
- First 3 entries only, with "+N more" suffix
- Each query cut to 30 chars (URLs 40 chars)

Users couldn't see what candidate search terms the model was trying.

## Fix
- Render ALL entries, not just first 3. Use ` | ` separator for queries,
  `, ` for URLs.
- Per-query truncation raised to 80 chars (URLs use `_short_url(max_len=60)`).
- Also emit full untruncated list via `_batchQueries` / `_batchUrls` fields
  on both the round_entry and the `tool_start` SSE event payload (fields
  don't start with `_display_` so they pass the filter in
  `_build_tool_round_entry`).

## Frontend
No frontend change needed — `.ptool-text` uses `word-break: break-word`
so long display strings wrap. `_batchQueries` / `_batchUrls` are available
on the message for future structured rendering (chip lists, etc.).

