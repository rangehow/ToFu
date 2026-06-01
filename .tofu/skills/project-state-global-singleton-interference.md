---
name: project-state-global-singleton-interference
description: Bug pattern: server-side global project _state causes context loss when conversations switch projects — fixed with _tree_cache + ensure_project_state
enabled: true
tags: [javascript, frontend, bug-fix, crosstalk, global-state, per-conversation, config-dispatch, background-stream]
created: 2026-03-24T04:29:47Z
updated: 2026-04-05T04:49:23Z
---

# Project State Global Singleton Interference

## Problem
The server has a single global `_state` dict in `lib/project_mod/config.py` that holds the current project path, file tree, etc. When conversation A sets the project to `/foo` and conversation B switches to `/bar`, the global state changes to `/bar`. Conversation A's task then calls `get_context_for_prompt("/foo")` but finds `_state['path'] != "/foo"` → falls back to no file tree → LLM lacks project context → appears "backend cannot use tools."

## Root Cause
- `_state['path']` is set by `/api/project/set` (called by frontend's `_restoreConvProject`)
- Tasks receive `project_path` from config but rely on `_state` for the file tree
- `get_context_for_prompt(base_path)` checks `state_matches = (path == _state['path'])` — if false, returns minimal context

## Fix (3 parts)

### 1. Tree Cache (`config.py`)
```python
_tree_cache = {}  # abs_path → { 'tree': str, 'fileCount': int, 'scannedAt': int }
```
Survives project switches. Populated by `_scan_worker` on completion.

### 2. Tree Cache Fallback (`indexer.py`)
In `get_context_for_prompt`, when `state_matches` is False and path not in `_roots`, check `_tree_cache[path]` before giving up.

### 3. Auto-reconcile (`scanner.py` + `orchestrator.py`)
`ensure_project_state(path)` — called at task start:
- If `_state['path']` already matches: no-op
- If `_tree_cache` has the path: fast restore from cache (no re-scan)
- Otherwise: calls `set_project()` (triggers background scan)

### 4. Scan worker caches even on abort
When `_scan_worker` completes but `_state['path']` has changed, it still writes the tree to `_tree_cache` so future tasks can use it.

## Key files
- `lib/project_mod/config.py` — `_tree_cache` dict
- `lib/project_mod/scanner.py` — `ensure_project_state()`, `_scan_worker` cache writes
- `lib/project_mod/indexer.py` — `get_context_for_prompt` cache fallback
- `lib/tasks_pkg/orchestrator.py` — `ensure_project_state()` call before prefetch

