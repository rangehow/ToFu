---
name: overleaf-mcp-output-url-quirk
description: Overleaf /project/{id}/output/* shortcut returns 404; must use per-build URL
enabled: true
tags: [overleaf, mcp, bug]
created: 2026-04-19T09:13:07Z
updated: 2026-05-05T07:02:54Z
---

# Overleaf compile output URL quirks

## (A) Legacy shortcut → 404 (old quirk)
The convenience URL `/project/{project_id}/output/output.pdf`
(and `output.log`, etc.) returns **404**. Must use the per-build URL
returned inside `compile_project()`'s `output_files`:
`/project/{project_id}/user/{user_id}/build/{build_id}/output/output.pdf`

`download_pdf` already builds this; `download_log` was fixed 2026-04 to
prefer `file.get("url")` from `output_files` like `download_pdf`.

## (B) clsiserverid query param NOW REQUIRED (2026-05)
The per-build URL **also** requires `?clsiserverid=<clsi-...>` — omitting
it gives the same 404. The id is in the compile response as a top-level
`clsiServerId` field, which `compile_project()` in overleaf-mcp-plus
0.1.3 **drops**. Result: current `download_log`/`download_pdf` are
completely broken.

See separate memory: `overleaf-mcp-clsiserverid-404-bug` for fix.

## (C) create_project via /project/new/blank is flaky
On current Overleaf `POST /project/new/blank` may return 404. pyoverleaf
doesn't implement it either. Expect `create_project` to sometimes fail.
(It worked for this session 2026-05.)

