---
name: read-files-read-local-file-merge
description: Merge: read_local_file removed — read_files now handles both project-relative and absolute paths (images, PDFs, Office docs)
enabled: true
tags: [tools, refactoring, architecture]
created: 2026-04-08T09:42:12Z
updated: 2026-04-08T09:42:12Z
---

# read_files ← read_local_file merge (2026-04-08)

## What changed
- `read_local_file` tool was **removed entirely** from the tool surface
- `read_files` now accepts **both relative and absolute paths**
- Absolute paths (starting with `/` or `~`) route to `_read_absolute_file()` in `read_tools.py`, which delegates to `lib.file_reader.read_local_file()` internally
- Image results (dicts with `__screenshot__`) are supported in batch via `__batch_images__` wrapper
- Line ranges now work on absolute text files too

## Key architecture
- `lib/file_reader.py` → core function `read_local_file()` remains as **internal implementation**
- `lib/project_mod/read_tools.py` → `_is_absolute_path()`, `_read_absolute_file()`, and enhanced `tool_read_files()`
- `lib/tools/project.py` → `PROJECT_TOOL_READ_LOCAL_FILE` removed; `PROJECT_TOOL_READ_FILES` description updated
- `lib/tasks_pkg/handlers/project.py` → handles `__batch_images__` from mixed batch reads

## Files modified (17 total)
`lib/project_mod/read_tools.py`, `lib/tools/project.py`, `lib/tasks_pkg/model_config.py`, `lib/scheduler/timer.py`, `lib/tasks_pkg/handlers/project.py`, `lib/project_mod/tools.py`, `lib/tools/meta.py`, `lib/tools/deferral.py`, `lib/tasks_pkg/streaming_tool_executor.py`, `lib/tasks_pkg/compaction.py`, `lib/tools/search.py`, `lib/tasks_pkg/handlers/search.py`, `lib/file_reader.py`, `lib/tasks_pkg/tool_dispatch.py`, `tests/test_compaction_improvements.py`, `tests/test_cc_alignment.py`, `tests/test_cache_breakpoints.py`

