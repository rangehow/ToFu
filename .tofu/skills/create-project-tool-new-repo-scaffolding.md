---
name: create-project-tool-new-repo-scaffolding
description: New create_project tool + absolute-path writes under registered roots — unblocks "generate new repo at /path" scenarios
enabled: true
tags: [python, project-tools, create_project, multi-root, write_file, absolute-path, feature]
created: 2026-04-19T08:46:37Z
updated: 2026-04-19T08:46:37Z
---

# create_project tool — scaffolding new repositories outside the current project

## Problem
`write_file` / `apply_diff` / `insert_content` historically used `_safe_path(base, rel)` which rejected any path outside the primary project root. So when the user asked the model to "generate a whole new repo at /some/path referencing this repo", the model could read the current repo but had no way to create the target — it got "Path traversal blocked" errors.

## Solution — three pieces

### 1. `create_project(path, name?, overwrite?)` tool
- Defined in `lib/project_mod/write_tools.py` (`tool_create_project`).
- Expands `~`, abspath, validates via `_is_forbidden_create_path` (blocks `/`, `/etc`, `/usr`, `/bin`, `/sbin`, `/boot`, `/sys`, `/proc`, `/dev`, `/var`, `/lib*`, `/root`, Windows `C:\` system paths, and `$HOME` itself — descendants of `/home`, `/opt`, `/tmp`, `/workspace` are fine).
- Creates directory (`makedirs exist_ok=True`).
- Registers as **extra root** via `add_project_root` (never replaces primary — the "reference" project stays available for reading).
- Returns `{ok, path, rootName, created, overwrite, message}`.
- If the target already exists and is non-empty, fails unless `overwrite=true`. With `overwrite=true`, existing files are NOT deleted — only the guard is bypassed so the dir can be registered.
- Writes `audit_log('project_create', ...)`.

### 2. Write tools accept absolute paths under registered roots
- `_resolve_write_path(base, rel_path)` in `write_tools.py`: if rel_path starts with `/` or `~`, resolves to abspath and validates it lies under any entry of `_roots` (the registered workspace roots dict). Otherwise falls back to the existing `_safe_path`.
- Symmetric with `read_files` which already accepted absolute paths via `_read_absolute_file`.
- Any absolute path NOT under a registered root is rejected with: *"Absolute path X is outside all registered workspace roots. Call create_project(path=...) first, or use a 'rootname:relative' prefix."*
- Applied in `tool_write_file`, `_apply_one_diff`, `_insert_one`.

### 3. Integration points touched
- **Schema**: `PROJECT_TOOL_CREATE_PROJECT` in `lib/tools/project.py`, added to `PROJECT_TOOLS`, `PROJECT_TOOL_NAMES`, and `__all__`.
- **Dispatch**: new `elif fn_name == 'create_project'` branch in `execute_tool()` in `lib/project_mod/tools.py`.
- **Display**: `project_tool_display` branch for `create_project`.
- **Meta builder**: `_build_create_project` in `lib/tools/meta.py` → `_META_BUILDERS`.
- **Approval gate**: added to `is_write_op` set, `_WRITE_TOOLS`, and `_APPROVAL_META_ENRICHERS` (`_approval_meta_create_project`) in `lib/tasks_pkg/tool_dispatch.py`. Creates go through the same approve/reject UI flow as `write_file`.
- **Deferral**: added to `CORE_TOOL_NAMES` and `_NEVER_DEFER` in `lib/tools/deferral.py` so it's always loaded.
- **Cache invalidation**: added to the `elif fut_fn_name in (...)` check that triggers `_invalidate_project_cache`.
- **System prompt**: added to the tools list in `lib/project_mod/indexer.py` with an explicit use-case note.
- **Frontend**: added to `_isRoundProject()` and icon map in `static/js/ui.js`.
- **Healthcheck**: added `tool_create_project` to the `lib.project_mod` export list.

## Usage pattern for the LLM
```
1. User: "Generate a new FastAPI repo at ~/projects/myapi, referencing patterns from the current project."
2. Model: create_project(path='~/projects/myapi') → {rootName: 'myapi', ...}
3. Model reads current project normally (list_dir, grep_search, read_files).
4. Model writes to new project via either:
     write_file(path='myapi:src/main.py', content=...)     # name: prefix (preferred)
     write_file(path='/home/u/projects/myapi/src/main.py', content=...)  # absolute
5. Both primary and new roots available for reads. User can still Stop/Reject via the approval UI.
```

## Safety layers
1. `_is_forbidden_create_path` blocks system paths at creation time.
2. Non-empty existing directory requires `overwrite=true`.
3. Absolute-path writes must be under some registered root (transitive safety — attackers can't just write `/etc/passwd` by guessing an absolute path).
4. Approval flow gates `create_project` just like `write_file`.
5. No undo recorded for the directory creation itself (MVP limitation — manual cleanup needed if abandoned). File writes inside the new project ARE tracked normally by `_record_modification`.

## Testing
Full smoke test:
```python
from lib.project_mod import set_project, tool_create_project, execute_tool
set_project(os.getcwd())
r = tool_create_project('/tmp/foo', conv_id='c', task_id='t')  # ok
execute_tool('write_file', {'path': 'foo:hello.py', 'content': '...'}, os.getcwd(), conv_id='c', task_id='t')  # ok
execute_tool('write_file', {'path': '/tmp/foo/bar.py', 'content': '...'}, os.getcwd(), conv_id='c', task_id='t')  # ok (abs under registered root)
execute_tool('write_file', {'path': '/etc/x', 'content': '...'}, os.getcwd(), ...)  # rejected
tool_create_project('/etc/evil')  # rejected (system path)
```
All tests/test_project_tools.py, test_streaming_and_prefetch.py, test_compaction_improvements.py (227 tests) still pass.

