---
name: grep-search-find-files-batch-mode
description: Batch mode for grep_search and find_files tools — `searches` array parameter like read_files/apply_diff
enabled: true
tags: [python, tools, batch-mode, grep_search, find_files, project-tools]
created: 2026-04-14T02:16:42Z
updated: 2026-04-14T02:16:42Z
---

# grep_search & find_files Batch Mode

Both `grep_search` and `find_files` now support a `searches` array parameter for batch processing, similar to `read_files` (batch reads) and `apply_diff` (batch edits).

## Schema

```python
# grep_search batch
{"searches": [
    {"pattern": "foo", "path": "lib/", "include": "*.py", "max_results": 10},
    {"pattern": "bar", "include": "*.js"},
]}

# find_files batch
{"searches": [
    {"pattern": "*.py", "path": "lib/", "max_results": 50},
    {"pattern": "Dockerfile*"},
]}
```

## Files Changed

1. **`lib/tools/project.py`** — Tool definitions: added `searches` array param to both tools
2. **`lib/project_mod/read_tools.py`** — `tool_grep_batch()` and `tool_find_files_batch()` functions
3. **`lib/project_mod/tools.py`** — `execute_tool` dispatch handles batch; `project_tool_display` shows batch labels
4. **`lib/project_mod/__init__.py`** — Re-exports for batch functions
5. **`lib/tools/meta.py`** — `_build_grep_search` and `_build_find_files` handle batch in metadata

## Design Decisions

- Batch budget: 100K chars (vs 200K for read_files) since search results are denser
- Max batch size: 20 (same as read_files)
- Multi-root aware: each search spec can target a different base path
- Dedup cache: works automatically via `_make_cache_key` JSON serialization
- Frontend: no JS changes needed — generic badge/display path handles batch metadata
- Single-mode backward compatible: if no `searches` array, old behavior unchanged

