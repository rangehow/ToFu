---
name: mcp-tool-display-resource-keys
description: tool_display._MCP_RESOURCE_KEYS / _MCP_CONTAINER_KEYS — extending for new MCP servers (xuecheng/hope precedent)
enabled: true
tags: [mcp, tool-display, convention]
created: 2026-05-08T11:34:33Z
updated: 2026-05-08T11:34:33Z
---

# MCP Tool Display: Resource & Container Keys

## What
`lib/tasks_pkg/tool_display.py` renders `🔌 server/tool — RESOURCE @ CONTAINER`
in the chat tool panel. The suffix builder is generic — it scans `fn_args`
for keys in two ordered lists:

- `_MCP_RESOURCE_KEYS` — the inner "what" (file_path, doc, run_id, keyword, …)
- `_MCP_CONTAINER_KEYS` — the outer "where" (project_id, repo, cluster, namespace)

First match wins. If a new MCP server's args don't appear here, the panel
shows only the bare `🔌 server/tool` (this is what hope/xuecheng hit before).

## Adding a new MCP server
1. Identify the 1-2 args most users want to see in the title.
2. Append to `_MCP_RESOURCE_KEYS` (resources) or `_MCP_CONTAINER_KEYS`
   (containers) — order matters; put the most specific key first.
3. If the value needs shortening (long IDs, URLs), add a `_short_*`
   helper and dispatch in `_render_mcp_arg(key, val)`.
4. For 2-arg containers (like github's `owner` + `repo`), add an explicit
   `if 'k1' in fn_args and 'k2' in fn_args:` branch in `_mcp_arg_suffix()`.

## Existing special-cases
- `owner` + `repo` → `owner/repo` (github)
- `cluster` + `namespace` → `cluster/namespace` (hope log endpoints)
- `doc` (url-or-id) → numeric id via `_short_doc_id()` (xuecheng)
- `project_id` (24-hex) → `prefix…suffix` or cached project name (overleaf)
- `job_id` / `app_id` / `appid` (psx-prefixed) → `_short_job_id()` (hope)
- `job_ids` (comma list) → first 2 + `+N more`
- `issue_number` / `pull_number` → `#N` and combined with owner/repo as
  `owner/repo#N` (no `@`)

## Frontend
`static/js/ui.js` `tool_start` handler uses `ev.query` verbatim — all
formatting happens server-side.

## Coverage check
After adding a server, smoke-test with a python one-liner:
```python
from lib.tasks_pkg.tool_display import _tool_display_mcp
print(_tool_display_mcp('mcp__server__tool', {...args...}, 't', '{}')[0])
```

