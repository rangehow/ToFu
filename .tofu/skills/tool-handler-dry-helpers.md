---
name: tool-handler-dry-helpers
description: DRY helpers for tool handlers: _finalize_tool_round() replaces 3-line round_entry/status/append_event boilerplate, _build_simple_meta() builds standard meta dicts, _APP_ROOT avoids os.path.dirname×3 duplication — all in lib/tasks_pkg/executor.py; also fixed _save_image_to_disk writing to lib/uploads/ instead of project-root uploads/
enabled: true
tags: [python, refactoring, dry, tool-handler, executor, architecture]
created: 2026-03-30T17:30:24Z
updated: 2026-03-30T17:36:42Z
---

# Tool Handler DRY Helpers

## Location
`lib/tasks_pkg/executor.py` — exported and re-imported by `lib/tasks_pkg/tool_dispatch.py`

## Helpers

### `_finalize_tool_round(task, rn, round_entry, results, *, query_override='')`
Replaces the 3-line boilerplate that every tool handler repeated:
```python
round_entry['results'] = results
round_entry['status'] = 'done'
append_event(task, {'type': 'tool_result', 'roundNum': rn, 'query': ..., 'results': ...})
```

### `_build_simple_meta(fn_name, tool_content, *, source, icon='', badge='', title='', snippet='', extra=None)`
Builds the standard tool result meta dict with `toolName`, `title`, `snippet`, `source`, `fetched`, `fetchedChars`, `badge`. Extra keys merged via `extra` dict.

### `_APP_ROOT`
Module-level constant = `os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`.
Replaces duplicated 3-level dirname computation. **Critical**: `executor.py` is at depth 3 (`lib/tasks_pkg/executor.py`), so 2-level dirname only reaches `lib/`, not the project root. This caused a real bug in `_save_image_to_disk` (wrote to `lib/uploads/images/` instead of `uploads/images/`).

## When NOT to use `_finalize_tool_round`
- **web_search handler**: uses `display_results` (a processed list) + `searchDiag` conditional fields
- **Image gen progress emission**: sets `status='running'` (not `'done'`)
- **Handlers with complex meta dicts**: e.g. image_gen's `imagePrompt/imageUrl/imageSavedUrl` fields — use `_finalize_tool_round` for the event emission but build meta manually

## Bug Pattern: dirname depth mismatch
When computing paths relative to `__file__` in nested packages, always verify the dirname depth:
- `lib/foo.py` → 2 levels to project root
- `lib/tasks_pkg/foo.py` → 3 levels to project root
- `routes/foo.py` → 2 levels to project root

Using `_APP_ROOT` constant (computed once) eliminates this class of bugs entirely.

