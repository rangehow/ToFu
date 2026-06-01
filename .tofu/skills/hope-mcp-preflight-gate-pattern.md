---
name: hope-mcp-preflight-gate-pattern
description: MCP tool preflight: cached login gate prevents non-auth tools from hanging on 30s timeouts
enabled: true
tags: [hope-mcp, mcp, pattern, ux, tool-design]
created: 2026-04-23T04:16:15Z
updated: 2026-04-23T04:16:15Z
---

# Pattern: preflight login gate for MCP tool servers

## Problem
Users get an infinite-retry loop if they call a stateful MCP tool (e.g.
`hope_list_jobs`) without logging in first. The CLI blocks on its RPC
handshake, times out at the tool level, and surfaces as a generic
timeout — the LLM has no way to know the real cause is "no session",
so it retries and gives up.

## Fix
Every non-login tool awaits an `ensure_logged_in()` gate at the top of
its body. The gate:

1. Checks a module-level monotonic-time cache (`MAX_AGE_SEC = 60`).
2. If cache is fresh → return None (tool proceeds).
3. If cache is stale → run a cheap `check_login` probe ONCE.
4. If probe says logged in → prime cache, return None.
5. Otherwise → return a short-circuit dict that exactly matches the
   established `login_required: true` + `error_hint` envelope, plus
   `deferred_tool: "<tool_name>"` so the agent log shows which call
   triggered the login.

## Cache invalidation
Three sources invalidate or prime the cache:
- `login()` — primes on success, clears on denied/timeout
- `check_login()` — primes on positive probe (handles out-of-band login)
- Any tool that gets `login_required: true` back from `run_hope` —
  clears, so the NEXT tool re-probes (catches mid-session expiry when
  the cache was still warm)

## Reference implementation
`hope-mcp/src/hope_mcp/tools/preflight.py` — 167 LOC
`tools/jobs.py` — every tool now has a 3-line preflight stanza AFTER
its docstring and BEFORE its validation:
```python
async def list_jobs(...) -> dict[str, Any]:
    """Docstring."""
    _gate = await ensure_logged_in(tool_name="hope_list_jobs")
    if _gate is not None:
        return _gate
    # ... normal body ...
```
Plus each `result = await run_hope(...)` is followed by:
```python
if result.login_required:
    mark_logged_out()
```

## Import order matters
In `tools/__init__.py`, import `preflight` THEN `login` THEN `jobs` —
preflight's `check_login` import at call time is fine, but
`jobs.py`'s import-time `from .preflight import ...` must see a
fully-loaded preflight module. Test: `tests/test_preflight_gate.py`
(6 tests).

## Tools exempt from the gate
- `hope_login` — the user's only recovery path
- `hope_check_login` — used BY the gate
- `hope_prepare_login` (if present) — pure, no subprocess

## Test strategy
1. shim that always says "Please login first" → list_jobs returns
   `login_required=True` WITHOUT spawning the real command
2. successful login primes cache → next tool call spawns exactly 1
   subprocess, not 2 (no redundant probe)
3. stale-positive cache + login_required from real call → cache
   invalidated, next call re-probes
4. denied login → cache cleared
5. check_login positive → cache primed (out-of-band login works)
6. login tool itself bypasses the gate

