---
name: tool-call-bar-multi-root-prefix
description: Tool-call lines now show rootname: pill in multi-root workspaces via _toolRoot on round_entry/event
enabled: true
tags: [frontend, multi-root, tool-display, ui]
created: 2026-05-10T17:25:09Z
updated: 2026-05-10T17:25:09Z
---

# Tool-Call Bar — Multi-Root Workspace Prefix

Companion to `file-changes-bar-multi-root-prefix`. While the file-changes
bar already showed `rootname:` after a round finished, the live "Working…"
tool-call panel did not — users saw `Read static/posters/` or `$ ls foo`
with no indication which workspace root was targeted.

## Fix (3 layers)

### 1. Backend — attach `_toolRoot` per tool round
`lib/tasks_pkg/tool_display.py`:
- New `_FS_TOOLS_FOR_ROOT_PILL` frozenset (read_files, list_dir,
  grep_search, find_files, write_file, apply_diff, insert_content,
  create_project, run_command).
- `_split_rootname_prefix(path)` parses `rootname:rel/path` (skips
  abs paths, Windows drive letters).
- `_extract_first_path_arg(fn_name, fn_args)` reads the first path
  from flat or batch shapes (reads/edits/searches), uses
  `working_dir` for `run_command`.
- `_resolve_tool_root_name(fn_name, fn_args, conv_id)` consults
  `_conv_roots[conv_id]` (or the global `_roots` fallback). Returns:
  - `''` for non-FS tools or single-root workspaces (≤1 root)
  - the prefix's rootname when present (case-insensitive match
    against registry; unknown prefixes returned as-is so typos
    surface in UI)
  - the primary root's name when no prefix
- `_build_tool_round_entry(..., conv_id=None)` calls the resolver and
  attaches `_toolRoot` to both `round_entry` and the `tool_start`
  SSE event.

### 2. Callers pass conv_id
- `lib/tasks_pkg/tool_dispatch.py::parse_tool_calls` → uses
  `task.get('convId') or task.get('id')`.
- `lib/tasks_pkg/streaming_tool_executor.py::StreamingToolAccumulator._emit_tool_start`
  → same.

### 3. Frontend — render `.ptool-root` pill
`static/js/ui.js`:
- New `_renderToolRootPill(round)` — returns a `<span class="ptool-root">name:</span>`
  span when `round._toolRoot` is set AND `projectState.extraRoots.length > 0`.
- Pill injected after the `<span class="ptool-icon">…</span>` in
  these branches of `_renderUnifiedToolLine`:
  - run_command/code_exec running header
  - run_command/code_exec done header
  - awaiting_stdin header
  - generic searching state (`ptool-line ptool-active`)
  - default done line
- Single-root sessions get no pill (frontend guard).

`static/styles.css` — added `.ptool-root` matching the look of
`.fc-root` (blue pill) plus a small `.ptool-cmd-header .ptool-root`
margin tweak.

## Why _toolRoot only for FS tools
Per user request: web_search, fetch_url, browser_*, mcp__*, memory,
schedule, etc. don't operate on workspace paths, so a rootname pill
would be noise. The frozenset gates which tools get labeled.

## run_command without working_dir
Resolves to the primary root's name by design — without it, users
can't tell whether `ls foo/` ran in chatui vs hope-mcp.

