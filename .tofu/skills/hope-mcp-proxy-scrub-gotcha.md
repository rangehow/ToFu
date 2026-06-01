---
name: hope-mcp-proxy-scrub-gotcha
description: Hope cluster is intranet-only; strip HTTP(S)_PROXY from every hope subprocess env
enabled: true
tags: [hope-mcp, proxy, gotcha, subprocess]
created: 2026-04-23T03:35:47Z
updated: 2026-04-23T03:35:47Z
---

# Hope's gateway is intranet-only — strip proxy env at exec time

## Symptom
Every `hope` call hangs for exactly `HOPE_MCP_TIMEOUT` seconds and surfaces as
`"stderr": "timeout after 30s", "returncode": -1, "timed_out": true`. The LLM
retries once and gives up. Conversation `moax47qbreufy4` was a clean
repro.

## Root cause
Meituan's cluster gateway is only reachable on the corporate intranet.
Any `HTTP_PROXY` / `HTTPS_PROXY` in scope (from the user's shell, or from
ChatUI's own env for LLM API calls) gets inherited by the MCP server,
which inherits it to the `hope` child. hope's Thrift/gRPC call then
silently routes through the proxy, which cannot reach the intranet
gateway, and hangs until the connect timeout.

Setting `NO_PROXY` is **not** enough because some HTTP stacks only
honour specific host-match formats.

## Fix (in `hope-mcp`)
In `src/hope_mcp/cli.py::run_hope`, build a scrubbed env explicitly and
pass `env=` to `asyncio.create_subprocess_exec`:

```python
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
    "NO_PROXY", "no_proxy",   # strip NO_PROXY too — want default direct
)

def _build_hope_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_VARS}
```

Every `run_hope` caller (jobs, login, status, …) now spawns hope with a
proxy-free environment. Confirmed by `tests/test_proxy_scrub.py`.

## Related: make timeouts actionable
A timeout is NOT proof of expired auth, so we don't flag
`login_required=true`. Instead we add a distinct `timeout_hint` field
that mentions BOTH possible causes:

> "hope timed out after Ns. Causes: (1) not logged in — call
> hope_check_login/hope_login; (2) a proxy is interfering — unset
> HTTP_PROXY/HTTPS_PROXY. Retrying WILL NOT help."

The LLM now escapes the retry loop on the first timeout.

