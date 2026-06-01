---
name: overleaf-mcp-plus-pypi-publish
description: How to build + publish overleaf-mcp-plus to PyPI (token in ~/.pypirc; uv publish needs UV_PUBLISH_TOKEN env)
enabled: true
tags: [overleaf-mcp, pypi, publish, uv, release]
created: 2026-04-22T11:35:56Z
updated: 2026-05-05T12:08:51Z
---

# overleaf-mcp-plus — PyPI release workflow

Project lives at `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/overleaf-mcp`.
Package name on PyPI: **`overleaf-mcp-plus`** (the plain `overleaf-mcp` name was taken).
CLI entry point is still `overleaf-mcp`.

## Token storage (on this MLP machine)

**IMPORTANT**: `$HOME` is on ephemeral container storage. Persist the PyPI
token on the FUSE `/mnt/dolphinfs` path so it survives container restarts:

Path: `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/.secrets/pypirc`
(chmod 600). Format matches `~/.pypirc`:

```ini
[pypi]
  username = __token__
  password = pypi-AgEI...          # the PyPI API token
```

Do NOT drop this into the `overleaf-mcp/` or `chatui/` repos — they're
publishable. `.secrets/` is a dedicated sibling directory.

## Release steps

1. Bump `version = "x.y.z"` in `pyproject.toml`.
2. Build:
   ```bash
   cd overleaf-mcp
   rm -rf dist && uv build
   # → dist/overleaf_mcp_plus-x.y.z{-py3-none-any.whl,.tar.gz}
   ```
3. Publish — **`uv publish` does NOT read `~/.pypirc`**, so feed the token via env:
   ```bash
   UV_PUBLISH_TOKEN=$(grep -E '^\s*password\s*=' \
     /mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/.secrets/pypirc \
     | head -1 | sed 's/.*=\s*//') \
     uv publish dist/overleaf_mcp_plus-x.y.z*
   ```
   Alternative: `uv publish --token pypi-... dist/...`
4. Verify (no proxy needed to `pypi.org`):
   ```bash
   curl -s https://pypi.org/pypi/overleaf-mcp-plus/json | python -c \
     "import sys,json;d=json.load(sys.stdin);print(d['info']['version'])"
   ```
   or open https://pypi.org/project/overleaf-mcp-plus/

## Gotchas

- `uv publish` ignores `~/.pypirc` (that's a twine convention). Use
  `UV_PUBLISH_TOKEN` env var or `--token` flag.
- `twine` is not installed in the `sglang` conda env by default; `uv publish`
  is fine, no need to `pip install twine`.
- PyPI refuses re-upload of the same version — bump patch before `uv publish`
  if you made any content change.
- The `[compile]` optional-dependency previously pulled playwright (~100 MB)
  but 0.1.3 trimmed it to just `beautifulsoup4`.
- The uv *cache* at `~/.cache/uv/archive-v0/<hash>/overleaf_mcp/` holds the
  last-used version used by `uvx`. Bumping PyPI alone won't clear that cache —
  `uvx` will pick up the new version on next cold run (or bump the `>=` pin).

## Release history
- 0.1.1, 0.1.2 (Apr 20) — initial releases.
- 0.1.3 (Apr 22) — trimmed `[compile]` deps to just bs4.
- 0.1.4 (May 5 2026) — fix `download_log` / `download_pdf` 404 caused by
  missing `?clsiserverid=<id>` query param on per-build output URLs.
  See memory `overleaf-mcp-clsiserverid-404-bug`.
- 0.1.5 (May 5 2026) — README-only: fix pip install instructions
  (`pip install overleaf-mcp-plus`, not `overleaf-mcp`).

## ChatUI MCP config

`data/config/mcp_servers.json` may pin `overleaf-mcp-plus[compile]>=X.Y.Z`.
If you release a new version, either:
- leave the pin as `>=` (users auto-upgrade next `uvx` run), or
- bump the pin if a new feature is required.

Symptom when pin > PyPI latest: `uvx` fails with "No solution found when
resolving tool dependencies" → MCP stdio closes → `McpError: Connection
closed` in lib.mcp.client logs.

