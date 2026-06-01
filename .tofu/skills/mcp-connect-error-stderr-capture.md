---
name: mcp-connect-error-stderr-capture
description: MCP connect failures: capture child stderr via tempfile errlog + unwrap nested ExceptionGroups into MCPConnectError; surface to UI with stderr tail
enabled: true
tags: [mcp, diagnostics, error-handling, robustness]
created: 2026-05-10T06:22:41Z
updated: 2026-05-10T06:22:41Z
---

# MCP connect failures: useful diagnostics

## Problem
A failed MCP `connect_server` (`lib/mcp/client.py`) propagated a giant
nested traceback ending in `ExceptionGroup: unhandled errors in a TaskGroup`
with the real cause buried 3 levels deep (typically `McpError: Connection closed`).
That string was returned verbatim to the UI as
`Config saved but connection failed: unhandled errors in a TaskGroup (1 sub-exception)`,
which is useless. The actual reason — a Python traceback or `ModuleNotFoundError`
from the launcher — was discarded because the child's stderr was sent to the
parent's stderr by default and never captured.

## Fix (lib/mcp/client.py)

1. **`_unwrap_exception_group(exc)`** — walks `BaseExceptionGroup.exceptions`
   recursively, returns the deepest non-group leaf. Keeps the actual
   `McpError`, `FileNotFoundError`, `TimeoutError`, etc.

2. **Per-server stderr capture** — in `_server_owner`, the stdio path now
   creates a `tempfile.TemporaryFile(mode='w+b')` and passes it as
   `errlog=` to `mcp.client.stdio.stdio_client(params, errlog=...)`.
   Must be a real fd-backed file (mcp passes it straight to
   `anyio.open_process(stderr=...)`); a `SpooledTemporaryFile`-only or
   in-memory wrapper does NOT work — needs `fileno()`.

3. **`MCPConnectError(server, cause, stderr_tail)`** — single exception
   class. `str(e)` formats `MCP server 'X': <cause>\n\nServer stderr (tail):\n<tail>`.
   "Connection closed" is rephrased to make the failure mode explicit
   ("launcher started but exited before completing the MCP handshake").

4. The owner's `except BaseException` unwraps + reads the stderr tail
   and stores `MCPConnectError` on `_ready_future` (so the caller sees
   the rich error). Stderr file is closed in `finally`.

5. `connect_all`'s except clause distinguishes `MCPConnectError` (log
   single line at error, full chain at debug) from other exceptions.

## Routes (routes/mcp.py)

`POST /api/mcp/catalog/install` and `POST /api/mcp/connect` both catch
`MCPConnectError` separately and put the formatted message + raw
`stderr_tail` in the JSON response so the UI can show real diagnostics.

## Sanity test
A `/tmp/dying.sh` that prints a fake Python traceback to stderr and
exits 1 produces, end-to-end:

```
MCP server 'die': Connection closed by server during initialize ...
Server stderr (tail):
ModuleNotFoundError: No module named 'nonexistent_module'
```

## Invariants
- `errlog` must be an fd-backed file (has `fileno()`) — `TemporaryFile`
  works; `SpooledTemporaryFile` may not always work; `BytesIO` won't.
- Always close + None-out `handle._stderr_file` in the owner's `finally`
  so we don't leak file descriptors per reconnect.
- Don't change `MCP_CONNECT_TIMEOUT` / `MCP_CALL_TIMEOUT` to "fix"
  diagnostics — those are guarded hyperparameters (per
  `mandatory-approval-before-disruptive-actions`).

