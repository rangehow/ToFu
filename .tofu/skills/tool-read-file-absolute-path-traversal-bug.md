---
name: tool-read-file-absolute-path-traversal-bug
description: Bug fix + refactor: removed redundant tool_read_file, unified into tool_read_files with absolute path support and per-spec _base for multi-root
enabled: true
tags: [bug-fix, refactor, path-traversal, tool-results, read_files, multi-root]
created: 2026-04-13T02:58:22Z
updated: 2026-04-13T03:04:40Z
---

# Bug: tool_read_file blocks absolute paths as "Path traversal"

## Root Cause
`execute_tool()` for `read_files` reimplemented the read loop (calling `tool_read_file`
per spec for multi-root support), bypassing `tool_read_files` which had absolute path
routing, range merging, image handling, and WHOLE_FILE_THRESHOLD auto-expansion.

When a persisted tool-result absolute path was read from a conversation with a different
project base, `tool_read_file` → `_safe_path` blocked it as path traversal.

## Fix (refactor)
1. **Deleted `tool_read_file`** — renamed to `_read_project_file` (private helper)
2. **`tool_read_files`** is now the single entry point:
   - Supports per-spec `_base` override for multi-root workspaces
   - Routes absolute paths to `_read_absolute_file`
   - `_merge_same_file_ranges` preserves `_base` through merging
3. **`execute_tool`** just resolves `_resolve_base` per spec then delegates to `tool_read_files`
4. All imports/exports updated (`__init__.py`, `tools.py`, `healthcheck.py`)

## Files Changed
- `lib/project_mod/read_tools.py` — `tool_read_file` → `_read_project_file`, `_merge_same_file_ranges` preserves `_base`
- `lib/project_mod/tools.py` — `execute_tool` simplified, removed `tool_read_file` import
- `lib/project_mod/__init__.py` — export `tool_read_files` instead of `tool_read_file`
- `healthcheck.py` — updated reference

