---
name: mcp-uninstall-bridge-disconnect-method-name
description: routes/mcp.py must call bridge._disconnect_one (sync), not non-existent _async_disconnect_one
enabled: true
tags: [mcp, bug, routing, debugging]
created: 2026-04-24T06:14:08Z
updated: 2026-04-24T06:14:08Z
---

# MCP Uninstall "does nothing" — silent AttributeError in route handlers

## Symptom
Clicking "卸载" (uninstall) on an MCP catalog card (e.g. Hope) in Settings does
nothing visible. No error dialog. The card still shows connected.

## Root cause
`routes/mcp.py` called `bridge._run_async(bridge._async_disconnect_one(target))`
in three places (delete_server, disconnect_servers, uninstall_from_catalog).
`_async_disconnect_one` does NOT exist on `MCPBridge` — only the synchronous
`_disconnect_one(name)` does (which internally runs `_async_signal_shutdown`
through `_run_async_with_timeout`).

The `AttributeError` was caught by the broad `except Exception` block and
logged as a WARNING ("Error disconnecting hope: 'MCPBridge' object has no
attribute '_async_disconnect_one'"). The route then proceeded to set
`enabled=False` in config and returned `ok: true`, so the frontend happily
refreshed the catalog — but because the bridge was never actually
disconnected, `connected=true` persisted in the `/api/mcp/catalog` response
and the card re-rendered identically. To the user, the button looked dead.

## Fix
Replace all three `bridge._run_async(bridge._async_disconnect_one(X))` calls
with `bridge._disconnect_one(X)`. The sync wrapper already schedules the
async shutdown signal on the MCP event loop with a bounded timeout.

## Takeaway
- Broad `except Exception` + generic warning masked a trivial typo bug for
  weeks. The `logs/error.log` had the exact AttributeError but nobody looked
  (no user-visible error).
- For button-does-nothing bugs in this project, always grep
  `logs/error.log` / `logs/app.log` for the feature name first — the
  logging discipline means the traceback is almost always captured.

