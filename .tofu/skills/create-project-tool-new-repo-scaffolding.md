---
name: create-project-tool-new-repo-scaffolding
description: Absolute-path writes auto-register nearest existing dir as a root (no mandatory create_project); create_project now optional
enabled: true
tags: [python, project-tools, create_project, multi-root, write_file, absolute-path, feature]
created: 2026-04-19T08:46:37Z
updated: 2026-06-05T08:17:29Z
---

# create_project tool + auto-registering absolute-path writes

## Problem (original)
`write_file` / `apply_diff` / `insert_content` historically rejected any absolute path outside the primary root, so "generate a new repo at /some/path" needed an explicit `create_project` first. The model rarely thought to call it, hitting *"Absolute path X is outside all registered workspace roots. Call create_project(path=...) first…"*. This was asymmetric with `read_files`, which reads ANY absolute path freely.

## Fix (2026-06 — friction removal)
`_resolve_write_path(base, rel_path)` in `lib/project_mod/write_tools.py` now AUTO-REGISTERS instead of rejecting:
1. If the abs path already resolves under a registered root → use directly.
2. Else, if NOT a forbidden system path → find the deepest existing ancestor dir (`_nearest_existing_dir`) and `add_project_root(anchor)`, then allow the write. Writes "just work" like reads.
3. Forbidden system paths (`_is_forbidden_create_path` on the path AND its dirname: `/etc`, `/usr`, `$HOME` itself, …) are still rejected with *"Refusing to write to system path …"*.

So **`create_project` is now OPTIONAL** — only needed to (a) pre-create an empty dir, or (b) assign an explicit `name:` prefix. Tool descriptions in `lib/tools/project.py` were updated: `write_file` documents the auto-register behavior; `create_project` says "you usually do NOT need this".

NOTE: the `name:` (colon-prefix) path in `_resolve_base` (tools.py) is STILL strict — an unknown root name raises `UnknownWorkspaceRootError` (no silent fallback to primary → avoids clobber). Only the bare-absolute-path case auto-registers.

## create_project(path, name?, overwrite?) — still available
- `lib/project_mod/write_tools.py::tool_create_project`. Expands `~`, abspath, validates via `_is_forbidden_create_path`. Creates dir, registers via `add_project_root` (extra root, never replaces primary). Returns `{ok, path, rootName, created, overwrite, message}`. Non-empty existing dir needs `overwrite=true` (files NOT deleted). Writes `audit_log('project_create', ...)`.

## Integration points (unchanged from original wiring)
- Schema `PROJECT_TOOL_CREATE_PROJECT` in `lib/tools/project.py`; dispatch branch in `lib/project_mod/tools.py::execute_tool`; display in `tool_display.py`; meta `_build_create_project` in `lib/tools/meta.py`; approval gate in `lib/tasks_pkg/tool_dispatch.py` (`is_write_op`, `_WRITE_TOOLS`, `_APPROVAL_META_ENRICHERS`); deferral `CORE_TOOL_NAMES`/`_NEVER_DEFER`; cache-invalidation list; system prompt in `lib/project_mod/indexer.py`; frontend `static/js/ui.js`.

## Safety layers
1. `_is_forbidden_create_path` blocks system paths at both create AND auto-register time.
2. Auto-register only touches the nearest EXISTING ancestor (doesn't create system dirs).
3. Approval flow still gates write_file/create_project.

## Testing
End-to-end through `execute_tool('write_file', {'path': '/tmp/x/proxy/src/core/client.py', ...}, cwd)` succeeds with NO prior create_project; nearest existing ancestor `/tmp/x` gets auto-registered; `/etc/...` still blocked. (pytest suites can't run in this env due to a polluted sibling-workspace `pytest11` entrypoint causing `TypeError: required field "lineno" missing from alias` — unrelated to this change.)

