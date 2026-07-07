---
name: create-project-tool-new-repo-scaffolding
description: Absolute-path writes auto-register nearest existing dir as a root (no mandatory create_project). EXCEPT temp-dir scratch (untracked); NEW roots emit workspace_root_added event.
enabled: true
tags: [python, project-tools, create_project, multi-root, write_file, absolute-path, feature]
created: 2026-04-19T08:46:37Z
updated: 2026-06-26T05:59:40Z
---

# create_project tool + auto-registering absolute-path writes

## Problem (original)
`write_file` / `apply_diff` / `insert_content` historically rejected any absolute path outside the primary root, so "generate a new repo at /some/path" needed an explicit `create_project` first. The model rarely thought to call it. Asymmetric with `read_files`, which reads ANY absolute path freely.

## Fix (2026-06 — friction removal)
`_resolve_write_path(base, rel_path)` in `lib/project_mod/write_tools.py` AUTO-REGISTERS instead of rejecting:
1. If the abs path already resolves under a registered root → use directly.
2. Else, if NOT a forbidden system path → find deepest existing ancestor (`_nearest_existing_dir`) and `add_project_root(anchor)`, then allow the write.
3. Forbidden system paths still rejected (`_is_forbidden_create_path` on path AND dirname).

So **`create_project` is OPTIONAL** — only for (a) pre-creating an empty dir, or (b) an explicit `name:` prefix.

NOTE: the `name:` colon-prefix path in `_resolve_base` is STILL strict — unknown root name raises `UnknownWorkspaceRootError`. Only bare-absolute-path auto-registers.

## 2026-06-26 — two refinements to the auto-register branch
**(A) Temp-dir scratch writes are UNTRACKED.** Step **1.5** in `_resolve_write_path` (after registered-root check, before system-path refusal): if `_is_temp_path(abs_path)` → return abspath WITHOUT `add_project_root`. `_temp_roots()` (cached on `_temp_roots._cache`) = `tempfile.gettempdir()` (honours $TMPDIR) + `/tmp` + `/var/tmp`, realpath-normalized — NO hardcoded single path. The 3 write fns gate `_record_modification` behind `_should_record_modification(target)` which skips ONLY temp paths OUTSIDE all registered roots (a project legitimately opened under /tmp/proj IS still tracked). Net: `write_file('/tmp/x.py')` now matches `run_command` to /tmp — completes, no file-changes-bar entry, no undo record.

**(B) Silent auto-registration is now OBSERVABLE.** New event `EventType.WORKSPACE_ROOT_ADDED` (`workspace_root_added`) in `lib/agent_core/events.py`. Write layer (has only conv_id/task_id, not `task`) queues a per-thread signal via `_signal_root_added`/`drain_root_added_signals` (`threading.local` — race-free: handler→execute_tool→_resolve_write_path is one synchronous thread, and execute_tool stringifies the write result so the dict can't carry the signal). Only signals a GENUINELY-new root (not a re-resolved existing one). `lib/tasks_pkg/handlers/project.py` drains right after the write tool (next to artifact-promotion post-hook) and `emit(task, WORKSPACE_ROOT_ADDED, roots=[{rootName,path}])`. Frontend `_handleWorkspaceRootAdded` (sse_handlers_misc.js) + dispatch (sse_pipeline.js) → toast; i18n `workspaceRoot.added`.

Tests: `tests/test_temp_write_and_root_signal.py` (9). Regression: test_readonly_roots, test_event_registry, test_event_emit. The `llm_platform/llm-mcp` confusion that prompted this = `llm-mcp` is a SUBDIR of the registered `llm_platform` root → no extra root ever created, `_mod_attribution` (deepest registered root) honestly prefixes `llm_platform:`.

## create_project(path, name?, overwrite?) — still available
`lib/project_mod/write_tools.py::tool_create_project`. Expands `~`, abspath, validates via `_is_forbidden_create_path`. Registers via `add_project_root` (extra root, never replaces primary). Non-empty existing dir needs `overwrite=true`. `audit_log('project_create', ...)`.

## Integration points
Schema `PROJECT_TOOL_CREATE_PROJECT` in `lib/tools/project.py`; dispatch in `lib/project_mod/tools.py::execute_tool`; display `tool_display.py`; meta `_build_create_project` in `lib/tools/meta.py`; approval gate in `lib/tasks_pkg/tool_dispatch.py`; system prompt in `lib/project_mod/indexer.py`; frontend `static/js/ui.js`.

