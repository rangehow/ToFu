---
name: overleaf-mcp-download-pdf-404
description: overleaf-mcp download_pdf 404 — must use per-build URL from compile
enabled: true
tags: [overleaf, mcp, bug]
created: 2026-04-18T07:48:33Z
updated: 2026-04-18T07:48:33Z
---

# overleaf-mcp download_pdf 404 bug

## Symptom
`mcp__overleaf__download_pdf` fails with `Client error '404 Not Found' for url 'https://www.overleaf.com/project/<id>/output/output.pdf'`.

## Root cause
Overleaf's legacy shortcut `/project/<id>/output/output.pdf` is no longer served. The compile API now returns only per-build URLs:
`/project/<id>/user/<uid>/build/<buildId>/output/output.pdf`

## Fix (in `overleaf-mcp/src/overleaf_mcp/compile.py`)
`download_pdf` should call `compile_project()` first, then iterate `output_files` to find the `path == "output.pdf"` entry and use its `url` (prefix with `OVERLEAF_BASE_URL` if relative). MCP server must be restarted to pick up the change.

## Workaround without restart
Use the build URL from a successful compile response directly with curl + `Cookie: overleaf_session2=<session>`.

## Also: stale MCP config (missing env)
If an overleaf MCP server was registered before `env_specs` existed in `lib/mcp/registry.py`, `data/config/mcp_servers.json` has no `env` block and `OVERLEAF_SESSION` isn't passed to the subprocess. Fix: uninstall + reinstall from the catalog UI so it prompts for the session cookie (and optional `OVERLEAF_GIT_TOKEN`).

