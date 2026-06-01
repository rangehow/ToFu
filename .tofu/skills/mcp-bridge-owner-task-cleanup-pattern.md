---
name: mcp-bridge-owner-task-cleanup-pattern
description: MCP bridge: long-lived owner task pattern avoids AsyncExitStack cross-task aclose() hangs
enabled: true
tags: [mcp, asyncio, anyio, pattern, cancel-scope, bugfix]
created: 2026-04-23T04:49:03Z
updated: 2026-04-23T04:49:03Z
---

# MCP Bridge: Owner-Task Pattern for AsyncExitStack Lifecycle

## Bug (2026-04-23)
`POST /api/mcp/catalog/install` on an already-connected server took ~133s.
Catalog GETs piled up behind `connect_server`'s `self._lock`, so the whole
Settings UI froze for >60s at a time.

## Root cause
`lib/mcp/client.py` used one coroutine (`_async_connect`) to open the
`AsyncExitStack` holding stdio_client + ClientSession, and a *different*
coroutine (`_async_disconnect_one`) to close it. anyio's cancel-scope
(used by the mcp sdk's stdio transport) forbids cross-task teardown —
`aclose()` silently blocks until the outer `_run_async` timeout
(`MCP_CALL_TIMEOUT + 10 = 130s`) fires. The empty log
`[MCP] Error disconnecting old hope:` is `str(futures.TimeoutError())`.

## Fix — long-lived owner task per server
Each server now has a dedicated coroutine `_server_owner(handle)` that:
1. `async with AsyncExitStack() as stack:` → opens transport + session.
2. `session.initialize()` + `session.list_tools()`.
3. Signals readiness via `handle._ready_future.set_result(tools)`.
4. `await handle._shutdown_event.wait()` — blocks until shutdown requested.
5. `AsyncExitStack.__aexit__` fires **in the same task** — no mismatch.
6. `finally: handle._closed_future.set_result(None)` — unblocks callers.

`connect_server` awaits `_ready_future` (with a shielded timeout).
`_disconnect_one` sets `_shutdown_event` and awaits `_closed_future`
with a tight 5s cap; on timeout it force-cancels the owner task.

## Invariants the code must preserve
- **Never** call `handle._ctx_stack.aclose()` directly (there is no such
  attr anymore — the owner owns the stack).
- Never hold `self._lock` across async calls to `_run_async` on the event
  loop thread — that blocks catalog GETs during reconnect.
- Pop from `self._servers` and clean `self._tool_index` BEFORE awaiting
  shutdown so callers see the server as gone immediately.
- Use `_run_async_with_timeout(coro, timeout=5.0)` for disconnect paths;
  the default `MCP_CALL_TIMEOUT + 10` budget (130s) is only for tool calls.

## Regression test
`debug/test_mcp_reconnect_fast.py` — connects then reconnects the
filesystem MCP server; asserts total reconnect time < 30s (was 130s).

## Related
- `search_memories('mcp')` for bridge architecture + config sync notes.
- The 429 / slow catalog pattern in error.log always coincides with a
  reconnect — if you see them together, this fix should have covered it.

