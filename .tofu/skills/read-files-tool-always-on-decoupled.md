---
name: read-files-tool-always-on-decoupled
description: read_files decoupled from project mode: registered unconditionally as READ_FILES_TOOL so absolute local paths (PDFs/images/Office) work without project
enabled: true
tags: [python, tools, read_files, architecture, project-mode]
created: 2026-04-20T10:13:00Z
updated: 2026-04-20T10:26:31Z
---

# read_files tool fully decoupled from project mode (2026-04-20)

## Why
`read_files` was gated by `project_enabled` (i.e. only exposed when a
project path was attached). Since the 2026-04-08 merge of `read_local_file`
into `read_files`, the tool handles absolute paths (PDFs, images, Office
docs, text files) via `lib.file_reader.read_local_file` — so there's no
architectural reason to keep it project-scoped. Users asking "read this
PDF at /tmp/foo.pdf" in a bare conversation had no tool to do so.

## Final clean design
`read_files` is a **standalone global tool** — not in `PROJECT_TOOLS`,
not in `PROJECT_TOOL_NAMES`. Three independent registrations:

1. **Tool schema** — `READ_FILES_TOOL` constant in `lib/tools/project.py`
   (lives in that file for proximity to related tools; exported via
   `__all__`).
2. **Dispatch** — `@tool_registry.tool('read_files', category='files')`
   stacked on top of the existing `@tool_registry.tool_set(PROJECT_TOOL_NAMES)`
   decorator on `_handle_project_tool` in `lib/tasks_pkg/handlers/project.py`.
   Both decorators route to the same handler (which already has a
   `fn_name=='read_files' and not project_path` branch).
3. **Display** — explicit `table.setdefault('read_files', _tool_display_project)`
   in `lib/tasks_pkg/tool_display.py` after the PROJECT_TOOL_NAMES loop.

## Orchestrator wiring
- `lib/tasks_pkg/model_config.py`: `_assemble_tool_list` unconditionally
  `tool_list.append(READ_FILES_TOOL)` before the project-tools block.
- `lib/scheduler/timer.py`: `_build_poll_tools` same unconditional add.

## Why read_files stays OUT of PROJECT_TOOL_NAMES
That set is used by:
- `tool_dispatch.py:602` — `needs_approval` check for write ops (N/A,
  read_files isn't a write op).
- Display table loop — now handled by explicit `setdefault`.
- Handler registration — now handled by dedicated `@tool_registry.tool`.

Leaving `'read_files'` in PROJECT_TOOL_NAMES would make the name set
semantically wrong (claims read_files is project-scoped when it's not)
and is redundant with the new direct registration.

## Invariants preserved
- `_PROJECT_CACHEABLE_TOOLS` in `tool_dispatch.py` still lists
  `'read_files'` as a string literal — keeps per-task dedup cache
  invalidation on write ops.
- `_IDEMPOTENT_TOOLS` in `tool_dispatch.py` still includes `'read_files'`.
- ~~`CORE_TOOL_NAMES` in `deferral.py` still lists `'read_files'`~~ —
  **STALE (2026-05-17):** the whole tool-deferral subsystem (`lib/tools/deferral.py`,
  `CORE_TOOL_NAMES`, `_NEVER_DEFER`, `tool_search`) was REMOVED (see
  `tool-deferral-subsystem-removed-2026-05` memory). Nothing is deferred any more;
  read_files is simply always in the list. Tool assembly also moved from
  `model_config._assemble_tool_list` to the declarative `ToolSpec` registry
  `lib/tools/registry.py::assemble_tool_list` (`_build_read_files` — "read_files is ALWAYS on").

## Side effect (intentional)
Because `_assemble_tool_list` uses `has_real_tools = len(tool_list) > 0`
as the gate for auto-adding memory tools and `emit_to_user`, those are
now also always present (even in bare conversations). This is fine —
they're cheap and always useful.

## Files touched
- `lib/tools/project.py` — rename → `READ_FILES_TOOL`, drop from
  PROJECT_TOOLS and PROJECT_TOOL_NAMES
- `lib/tasks_pkg/model_config.py` — unconditional append
- `lib/scheduler/timer.py` — unconditional append
- `lib/tasks_pkg/handlers/project.py` — stacked `@tool_registry.tool('read_files')`
- `lib/tasks_pkg/tool_display.py` — explicit `setdefault('read_files', ...)`
- `tests/test_cc_alignment.py` — tool_list no longer None in bare mode
- `benchmarks/tool_mode_ab_test.py` — prepend READ_FILES_TOOL to mode-A tools

