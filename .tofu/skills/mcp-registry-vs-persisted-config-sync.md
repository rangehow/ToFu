---
name: mcp-registry-vs-persisted-config-sync
description: MCP config bug pattern: registry fix doesn't propagate to already-installed entries in mcp_servers.json
enabled: true
tags: [mcp, debugging, config]
created: 2026-04-20T10:59:57Z
updated: 2026-04-20T10:59:57Z
---

# MCP registry ≠ persisted config

`lib/mcp/registry.py` `CATALOG` is ONLY consulted during catalog install.
Once installed, a server's command/args are written to
`data/config/mcp_servers.json` and every subsequent connect reads from
that file — not the registry.

**Symptom:** after fixing a catalog entry (e.g. switching `overleaf-mcp`
bare-command to `uvx --from …`), users who had already installed the card
on an older version keep hitting `FileNotFoundError: [Errno 2] No such
file or directory: 'overleaf-mcp'`. Their `mcp_servers.json` is pinned to
the stale command.

**Fix pattern:** add a `_STALE_COMMAND_MIGRATIONS` table + `_migrate_stale_entries`
helper called inside `load_mcp_config()` in `lib/mcp/config.py`. On load,
rewrite matching entries in-place (env/credentials preserved) and save
once. This self-heals users' configs on the next server start.

Implemented in chatui commit around v0.9.2. Entry for `overleaf`:
- match `command == 'overleaf-mcp'`
- rewrite to `uvx --from overleaf-mcp-plus[compile] overleaf-mcp`

When adding future migrations, append to `_STALE_COMMAND_MIGRATIONS` —
don't ship ad-hoc upgrade scripts.

