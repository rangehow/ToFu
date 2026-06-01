---
name: overleaf-mcp-project-id-rule
description: Overleaf MCP tools require 24-hex project_id, not local paths
enabled: true
tags: [overleaf, mcp, convention]
created: 2026-04-18T14:46:10Z
updated: 2026-04-18T14:46:10Z
---

# Overleaf MCP — `project_id` is always a 24-hex string

## The rule
Every `mcp__overleaf__*` tool (except `list_projects`, `create_project`) takes
`project_id` — this is the **remote Overleaf project ID**, a 24-character
lowercase hex string (MongoDB ObjectID format, e.g. `692a83fb82feceb233c4b0e7`).

**NEVER pass these as `project_id`:**
- `"."` or any local filesystem path
- A folder name like `"overleaf-project"` or `"latex/"`
- The project's human-readable title or name

## How to get the right ID
1. `mcp__overleaf__list_projects()` — returns all projects with their IDs in
   the format `• <Name> [24-hex-id]`.
2. Or copy from the Overleaf project URL: `overleaf.com/project/<24-hex-id>`.

## Local vs remote
These tools always operate on the **remote Overleaf repo** (via
`git.overleaf.com`). They clone/pull into a server-side cache
(`./overleaf_cache/<project_id>`).

If the user has already downloaded the project locally with
`download_source` or `download_source_zip`, use the normal local tools
(`read_files`, `grep_search`, `apply_diff`) on that local path instead of
calling `get_sections` / `read_file` / etc. with a path as `project_id`.

## Server-side enforcement (2026-04-18)
`overleaf-mcp/src/overleaf_mcp/config.py::get_project()` now validates
`project_id` against `^[0-9a-f]{24}$` and returns a descriptive error
pointing at this rule. The JSON schema `_PROJECT_ID_PROP` in
`server.py` also has `"pattern": "^[0-9a-f]{24}$"` so strict MCP clients
reject bad IDs client-side.

## Debugging symptom
If you see an error like `Error: /path/to/chatui/overleaf_cache` (short, ~95 chars),
the call was made with a non-hex `project_id` and the pre-fix server tried
to clone into a cache path that didn't exist. Re-issue with `list_projects`
output.

