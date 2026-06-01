---
name: split-persist-search-results
description: Oversized web_search/grep_search results are split into per-result/per-file separate disk files for selective reading
enabled: true
tags: [compaction, persistence, search, web_search, grep_search, split-persist]
created: 2026-04-12T09:47:20Z
updated: 2026-04-12T09:47:20Z
---

# Split-Persist for Search Tool Results

When `budget_tool_result()` triggers disk persistence for oversized results, multi-item tools now split each item into a **separate file** instead of one monolithic file.

## web_search
- Each search result `[N] Title` (separated by `════`) → saved as `search_{id}_{N}_{safe_title}.txt`
- Returns an index with title, URL, fetched status, file path, and content preview per result
- Falls through to single-file if only 1 result or can't parse format

## grep_search  
- Results grouped by source file path → each group saved as `grep_{id}_{safe_filepath}.txt`
- Returns an index with file path, match count, preview lines, and file path per group
- Falls through to single-file if only 1 source file

## Key functions in `lib/tasks_pkg/compaction.py`:
- `_persist_web_search_split()` — split web_search by result
- `_persist_grep_search_split()` — split grep_search by source file
- `_sanitize_filename()` — helper for safe filenames from titles/paths
- `_persist_to_disk()` — tries split-persist first, falls back to single-file

## Benefit
Model can selectively `read_files` individual results instead of reading everything.

