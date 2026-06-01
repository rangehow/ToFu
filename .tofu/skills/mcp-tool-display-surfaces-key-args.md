---
name: mcp-tool-display-surfaces-key-args
description: MCP tool display lines in chatui should surface the most informative arg (file_path, project_id short, name) so users can tell which resource a generic tool like create_file touched
enabled: true
tags: [mcp, ui, tool-display, ux, overleaf]
created: 2026-04-29T14:34:41Z
updated: 2026-04-29T14:45:41Z
---

# MCP tool display — surface resource + HUMAN-READABLE container

## Problem
Without surfacing args, the UI shows every overleaf MCP call as
`🔌 overleaf/create_file` with no clue *which* file, *which* project.
Even after adding the short project_id (`69f21…cca7`), users still can't
read project IDs — they need the **human-readable name**.

## Fix — three layers

### 1. `lib/tasks_pkg/tool_display.py` — title-line suffix
`_tool_display_mcp` splits args into:
- **resource** (what is being operated on): file_path / path / name / title /
  issue_number / pull_number / query / url / branch
- **container** (what scope it lives in): project_id / repo — rendered via
  the human-readable project name when available, else `short…id` form

Format: `<tool> — <resource> @ <container>`. Section titles compose with
file path as `main.tex › Introduction`.

Examples:
- `🔌 overleaf/create_file — acl.sty @ [EMNLP Demo] Tofu`
- `🔌 overleaf/update_section — main.tex › Introduction @ [EMNLP Demo] Tofu`
- `🔌 github/create_issue — title @ torvalds/linux`
- `🔌 github/get_issue — torvalds/linux#42`

### 2. `lib/mcp/project_names.py` — name cache (NEW)
Process-level `dict[project_id, name]`, populated opportunistically by
`ingest_tool_result()` from:
- `list_projects` JSON array with id/name fields
- `status_summary` plain-text header  `📄 Project: <title>  [<24-hex-id>]`
  (regex anchors on the 24-hex ID to tolerate bracketed titles like
  `[EMNLP Demo] Tofu`)
- `create_project` friendly text response
- `_ingest_obj` recursively walks any JSON for `{id/project_id, name/project_name}` pairs

`get_project_name(pid)` returns the cached name or `''`. Thread-safe via
lock. No network calls on the chatui side.

### 3. `lib/tasks_pkg/handlers/mcp.py` — hook into post-execution
`_post_build` calls `ingest_tool_result()` with the completed tool's result
THEN rebuilds the arg suffix (so the very call that learned the name shows
it). Also applies the same suffix to `meta.title`.

## overleaf-mcp side — `git_client.py`
Write ops return strings with project name + short id:
`✅ Created 'acl.sty' in project [EMNLP Demo] Tofu (69f21…cca7)`

`_resolve_project_name(pid)` calls `compile.list_projects_web()` once and
caches per-process. Falls back to `(short_id)` only if session cookie is
missing.

## Takeaway
For MCP tools with opaque 24-hex IDs, do BOTH:
1. Have the server return a human-readable identity in its response
2. Have the chatui client harvest name→id mappings from tool results into
   a process cache, so later calls (within the same session) can show
   the name even when they only pass the ID.

