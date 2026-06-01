---
name: mcp-soft-uninstall-semantics
description: MCP catalog uninstall is soft by default (disable, keep env); purge:true for hard delete (now under /api/v1/mcp)
enabled: true
tags: [mcp, routes, settings.js, ux]
created: 2026-04-18T14:07:35Z
updated: 2026-05-29T03:17:03Z
---

# MCP catalog uninstall — soft vs purge

As of 2026-04-18 (route moved to `/api/v1/mcp/catalog/uninstall` on
2026-05-29), uninstall behaves in two modes:

- **Default (soft)** — disconnects the server and sets `enabled: false` in
  `data/config/mcp_servers.json`, but **keeps the `env` block** so the user
  can re-enable via "连接" (reconnect) without re-entering credentials.
- **`{"purge": true}`** — removes the config row entirely, forgetting all
  credentials. Used for the "清除凭据" button on IDLE (installed but
  disconnected) cards.

Related behaviors:
- `POST /api/v1/mcp/catalog/install` merges user-supplied env with any
  existing stored env so a previously-soft-uninstalled entry can be
  "reinstalled" with partial (or no) input. It also forces `enabled:
  true` on the saved cfg.
- `POST /api/v1/mcp/connect` for a specific server also flips `enabled`
  back to `true` on success — so reconnecting from the IDLE state
  "undoes" the soft uninstall.

Frontend (`static/js/settings/mcp.js`):
- Connected card: `_mcpUninstall` (soft, confirm explains cred is kept).
- IDLE card: "连接" (`_mcpReconnect`) + "清除凭据" (`_mcpPurge`, posts `purge:true`).

Audit trail: `audit_log('mcp_uninstall', server=<id>, mode='soft'|'purge')`.

