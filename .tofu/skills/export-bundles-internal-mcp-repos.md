---
name: export.py bundles sibling internal MCP repos under vendor/
description: export.py bundles sibling hope-mcp/xuecheng-mcp into dest/vendor/ for personal+internal; install.sh pip-installs them; _portablize_bundled_mcp_config rewrites copied mcp_servers.json uvx --from /abs to bare launchers; personal tar fast-path calls both explicitly (NOT via _create_skeleton)
enabled: true
tags: [export, mcp, hope-mcp, xuecheng-mcp, vendor, install.sh]
created: 2026-05-07T12:13:59Z
updated: 2026-06-03T06:03:24Z
---

# export.py bundles sibling internal MCP repos under vendor/

## Why

`hope-mcp` and `xuecheng-mcp` are private Meituan sibling repos at
`<ROOT>/../hope-mcp` and `<ROOT>/../xuecheng-mcp`. Not on PyPI, so a
fresh personal/internal install otherwise has no launcher on PATH and
the MCP Install button fails with `launcher '...-mcp' is not on PATH`.

## How

`export.py::_bundle_internal_mcp_repos(dest, mode)` (~L1485):
- personal + internal only (never opensource)
- copies siblings → `<dest>/vendor/<name>/`, skips .git/build/cache

`export.py::_portablize_bundled_mcp_config(dest, mode)` (~L1548):
- personal + internal only
- rewrites the COPIED `data/config/mcp_servers.json` hope/xuecheng
  entries from `uvx --from /mnt/abs/...-mcp` → bare `{command:'<n>-mcp',
  args:[]}`, preserving `env`. Without this, personal export pins the
  source machine's absolute path and FAILS on another machine.
- idempotent; opensource no-op.

`install.sh` (~L994): scans `vendor/hope-mcp`/`vendor/xuecheng-mcp` for
pyproject.toml, `pip install --upgrade` them → bare launchers on PATH.

## CRITICAL wiring gotcha (fixed)

`_create_skeleton` calls both helpers, BUT personal mode does NOT call
`_create_skeleton` — it uses the tar fast-path `_export_personal_via_tar`
and `export_project` guards the skeleton with `if mode != 'personal'`
(~L1991). The sibling repos live OUTSIDE the source tree (ROOT.parent),
so the personal tar copy never picks them up. Therefore the personal
branch in `export_project` (~L1977) now calls
`_bundle_internal_mcp_repos(dest,'personal')` +
`_portablize_bundled_mcp_config(dest,'personal')` EXPLICITLY. Internal/
opensource still go through `_create_skeleton`. If you add a 3rd helper
that must run for personal export, wire it in BOTH places.

## Modes

| Mode | Bundled+portablized? |
|---|---|
| personal | yes (explicit, in export_project) |
| internal | yes (via _create_skeleton) |
| opensource | NO (internal repos; registry entries stripped) |

## Don't regress

- Siblings must stay at `<ROOT>/../hope-mcp` + `<ROOT>/../xuecheng-mcp`.
- Registry (lib/mcp/registry.py) already defaults both to bare command.
- Persisted `mcp_servers.json` overrides registry on load — that's why
  portablize must rewrite the copied file, not rely on the registry.
- `_STALE_COMMAND_MIGRATIONS` in lib/mcp/config.py self-heals only
  `overleaf`, not hope/xuecheng (absolute paths are per-user, can't match).

