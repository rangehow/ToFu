---
name: web-search-persist-preview-and-toolcontent-after-budget
description: Fix: web_search persist preview shows all results (structured), toolContent/Preview reflects post-budget model content
enabled: true
tags: [compaction, web_search, preview, budget, tool_dispatch]
created: 2026-04-08T04:22:49Z
updated: 2026-04-08T04:22:49Z
---

# Web Search Persist Preview & toolContent After Budget

## Problem 1: Web search persist preview too small
When web_search results exceed the 30K budget and get persisted to disk, the default
2000-char preview only showed the first result. With 6 results, the model lost all
info about results 2-6.

**Fix**: `_generate_web_search_preview()` in `compaction.py` parses the structured
web_search output format (`[N] Title / URL / ──── Full Page Content ────`) and
generates a preview with title+URL+500-char content snippet for ALL results.
Falls back to default truncation if content doesn't match web_search format.

## Problem 2: Preview showed wrong content
`toolContent` on `round_entry` and the `tool_complete` SSE event were set BEFORE
`budget_tool_result` ran, so frontend Preview showed raw unbudgeted content.

**Fix**: Moved `toolContent` assignment and `tool_complete` event emission to AFTER
budget_tool_result runs, so Preview always shows exactly what the model receives.
Also updated aggregate budget check to sync `round_entry['toolContent']` when it
modifies message content.

## Files Changed
- `lib/tasks_pkg/compaction.py`: `_persist_to_disk()`, `_generate_web_search_preview()`
- `lib/tasks_pkg/tool_dispatch.py`: Post-phase reorder in `execute_tool_pipeline()`
- `tests/test_compaction_improvements.py`: 3 new tests for structured preview

