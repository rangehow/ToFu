---
name: overleaf-mcp-clsiserverid-404-bug
description: Overleaf output URLs now REQUIRE ?clsiserverid=... query — overleaf-mcp download_log/pdf 404
enabled: true
tags: [overleaf, mcp, bug, clsi, 404]
created: 2026-05-05T07:02:44Z
updated: 2026-05-05T11:39:00Z
---

# overleaf-mcp download_log/download_pdf return 404 (clsiserverid missing)

## Symptom (2026-05)
`mcp__overleaf__download_log` / `download_pdf` both fail with `404 Not Found`
on the per-build URL even though `compile_project` reports a valid build:
`/project/<pid>/user/<uid>/build/<bid>/output/output.log` → 404.

## Root cause
Overleaf's CLSI output CDN now **requires** a `?clsiserverid=<clsi-...>` query
parameter on every per-build output URL. The compile response includes it as a
top-level `clsiServerId` field (also `outputUrlPrefix`, `pdfDownloadDomain`).

Verified:
```
<base>/project/<pid>/user/<uid>/build/<bid>/output/output.log                                 → 404 (0 bytes)
<base>/project/<pid>/user/<uid>/build/<bid>/output/output.log?clsiserverid=clsi-pre-emp-...   → 200 (full log)
```

## FIX APPLIED (2026-05-05) — overleaf-mcp-plus 0.1.4
Repo: `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/overleaf-mcp`

`src/overleaf_mcp/compile.py`:
- `compile_project()` now returns `clsi_server_id`, `output_url_prefix`,
  `pdf_download_domain` alongside `status` / `output_files`.
- New helper `_build_output_url(url, clsi_server_id)` appends
  `?clsiserverid=<id>` to any per-build URL (handles both absolute and
  site-relative URLs, and the `?`/`&` separator correctly).
- `download_pdf()` calls `_build_output_url(...)` before GETting.

`src/overleaf_mcp/server.py`:
- `download_log` handler imports and uses `_build_output_url(log_path,
  compile_result.get("clsi_server_id"))`.

`pyproject.toml`: version bumped `0.1.3` → `0.1.4`.

The currently-running MCP server is still 0.1.3 (uv cache) — restart the
chatui MCP client after publishing 0.1.4 to PyPI, OR point the mcp_servers
config to the local editable install.

## Related / still true
- Legacy shortcut `/project/<id>/output/output.pdf` (no `/user/<uid>/build/<bid>`)
  also returns 404 — always use per-build URLs from `outputFiles[*].url`.
- `download_source_zip` / `download_source` use `/project/<id>/download/zip`
  and do NOT need the clsi query param.
- Compile mode (xelatex etc.) switched via `POST /project/<id>/settings`
  body `{"compiler":"xelatex"}` → HTTP 204.

