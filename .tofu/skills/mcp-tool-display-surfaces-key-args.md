---
name: mcp-tool-display-surfaces-key-args
description: MCP tool display: surface resource + human-readable name + CLICKABLE link. overleaf-mcp emits canonical project_url (OVERLEAF_BASE_URL) in EVERY write/read tag; chatui harvests URL+name→_mcpLinks{label→href}, frontend _linkifyMcpLabels wraps the label in <a>
enabled: true
tags: [mcp, ui, tool-display, ux, overleaf]
created: 2026-04-29T14:34:41Z
updated: 2026-06-25T16:54:06Z
---

# MCP tool display — surface resource + HUMAN-READABLE container + CLICKABLE link

## Problem
The UI showed every overleaf MCP call as `🔌 overleaf/edit_file — acl.tex @ 6a1e7…a668`.
Users can't read or navigate the 24-hex id; they need the human name AND a hyperlink.

## Fix — layers

### 1. `lib/tasks_pkg/tool_display.py` — title-line suffix
`_tool_display_mcp` → `_mcp_arg_suffix`: resource (file_path/path/name/title/
issue_number/query/doc) `@` container (project_id → cached name else `short…id`).

### 2. `lib/mcp/project_names.py` — name + URL cache
Process dicts behind one lock, filled by `ingest_tool_result()`:
- `_cache` project_id→name, `_doc_cache` contentId→title
- `_proj_url_cache`/`_doc_url_cache` id→full URL (harvested via `_OVERLEAF_URL_RE`
  `(https?://[^\s/]+)/project/([0-9a-f]{24})` / `_KM_FULL_URL_RE` collabpage)
- `_overleaf_base` learned from first harvested overleaf URL (self-hosted!); default
  `https://www.overleaf.com`
- `get_project_url` ALWAYS synthesizes for valid 24-hex id; `get_doc_url` only when harvested.

### 3. Linkify (frontend `static/js/ui/tool_rounds.js`)
Backend `_tool_display_mcp` attaches `extra['_mcpLinks']={label→href}` keyed by the EXACT
rendered label. `_linkifyMcpLabels(text, round)` runs on already-escaped `q`, indexOf the
escaped label, wraps in `<a class="ptool-mcp-link" target=_blank rel=noopener>`.
**XSS guard: only `^https?://` hrefs.** CSS `.ptool-mcp-link` in styles.css.

### 4. `lib/tasks_pkg/handlers/mcp.py` `_post_build`
Calls `ingest_tool_result()` THEN rebuilds the suffix so the learning call shows it.

## ⭐ overleaf-mcp = authoritative URL source (2026-06-25 part 2)
**The client must NOT guess the host.** overleaf-mcp owns `OVERLEAF_BASE_URL`
(env, default www.overleaf.com — same constant compile.py/metadata.py use). So the
SERVER emits the canonical URL in EVERY project-scoped result; chatui just harvests it.
- `config.py::project_url(pid)` — `{BASE.rstrip('/')}/project/{pid}`, '' for invalid id.
- `git_client.py::_project_tag` now returns `[Name] (short…id) <url>` → ALL write tools
  (create/edit/rewrite/delete/upload) carry the link. **This was the latent bug:** before,
  ONLY create_project embedded a URL, so on self-hosted deploys every edit linked to the
  WRONG host (www.overleaf.com) until a create happened to run.
- server.py: `list_projects` (per-line `[id] <url>`), `status_summary` (`URL:` line),
  `create_project` (uses project_url instead of hardcoded www.overleaf.com).

## Tests
- chatui `tests/test_mcp_tool_links.py` (9): incl. write-tool-result-carries-harvestable-url.
- overleaf-mcp `tests/test_project_url.py` (8): default/self-hosted base, invalid→'',
  _project_tag embeds url. (reload config module per-test to pick up monkeypatched env.)

## Takeaway
For opaque-id MCP tools: the SERVER (which knows its base URL) emits the full URL in
its text → client harvests url→id + name→id into a process cache → display keys
`_mcpLinks` by the exact rendered label → frontend linkifies that substring (http(s) only).
Don't synthesize URLs client-side from a guessed host.
