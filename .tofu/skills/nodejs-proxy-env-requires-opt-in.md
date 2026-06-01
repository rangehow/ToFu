---
name: nodejs-proxy-env-requires-opt-in
description: Node.js MCP proxy: NODE_OPTIONS=--use-env-proxy + pin HTTP(S)_PROXY in mcp_servers.json env to avoid parent-env drift; fixes both instant-fail and 32s-timeout signatures
enabled: true
tags: [nodejs, proxy, mcp, networking, bugfix]
created: 2026-04-08T10:13:12Z
updated: 2026-05-06T15:09:09Z
---

# Node.js Proxy for MCP Subprocess — Complete Fix

## Problem
Node.js does NOT read `HTTP_PROXY` / `HTTPS_PROXY` environment variables by default.
MCP servers launched via `npx` (like `@modelcontextprotocol/server-github`) fail with
`McpError: fetch failed` (0.0s instant failure) OR — worse — silently hang on
`initialize()` until MCP_CONNECT_TIMEOUT (30s) fires, in corporate proxy environments.

## Root Cause Chain
1. `npx` spawns `npm exec`, which spawns the actual `node` process
2. `NODE_USE_ENV_PROXY=1` env var works for direct `node` invocations but does NOT
   reliably propagate through the npx→npm→node chain
3. The `@modelcontextprotocol/server-github` uses native `globalThis.fetch` on Node ≥ 18
   (node-fetch import is a no-op since globalThis.fetch already exists)
4. Native fetch only respects proxy when `--use-env-proxy` flag is active
5. Even with NODE_OPTIONS set, if HTTP(S)_PROXY env vars aren't visible to the
   subprocess, `--use-env-proxy` has nothing to read.

## Two Diagnostic Signatures
- **Instant failure (0.0s)**: subprocess fails fast — no proxy reachability OR token rejected
- **32s timeout on initialize()**: `npx -y` IS spawning, but cold-installing the package
  (or making a request from inside it) exceeds MCP_CONNECT_TIMEOUT=30s through proxy.
  Error chain ends with `anyio.WouldBlock` → `asyncio.CancelledError` in
  `mcp/shared/session.py` → `send_request` → `response_stream_reader.receive()`.
  Verify by manually running the same npx command — if it succeeds in <10s but fails
  in-server, the in-server env is the culprit.

## Solution (Three-Part — apply all)

### Code Fix (`lib/mcp/client.py` → `_server_owner` stdio branch)
Set BOTH env vars before subprocess launch:
```python
env.setdefault('NODE_USE_ENV_PROXY', '1')
existing_opts = env.get('NODE_OPTIONS', '')
if '--use-env-proxy' not in existing_opts:
    env['NODE_OPTIONS'] = f'{existing_opts} --use-env-proxy'.strip()
```

`NODE_OPTIONS=--use-env-proxy` is the key — it propagates as a CLI flag to ALL child
node processes spawned by npx, unlike the `NODE_USE_ENV_PROXY` env var.

### Config Hotfix (without server restart) — RECOMMENDED for ALL npx-based MCP servers
Add explicit proxy + NODE_OPTIONS to `env` in `data/config/mcp_servers.json`:
```json
"env": {
  "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_...",
  "NODE_OPTIONS": "--use-env-proxy",
  "HTTP_PROXY": "http://<proxy-host>:<port>",
  "HTTPS_PROXY": "http://<proxy-host>:<port>",
  "NO_PROXY": "localhost,127.0.0.1,.your-corp-suffix"
}
```
Then disconnect+reconnect via API. Pinning the proxy in the config block decouples
from the running server's process env (which can lag behind UI settings changes
since `/proc/<pid>/environ` is the exec-time snapshot, not the live `os.environ`).

### Verification
```bash
# Check the node child process has the flag
NODE_PID=$(ps aux | grep "mcp-server-github" | grep node | grep -v grep | awk '{print $2}')
cat /proc/$NODE_PID/environ | tr '\0' '\n' | grep -E 'NODE_OPTIONS|HTTPS_PROXY'
# Should show: NODE_OPTIONS=--use-env-proxy and HTTPS_PROXY=http://...

# Manual standalone test (gold reference — should complete in <10s):
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_... NODE_OPTIONS=--use-env-proxy \
        HTTP_PROXY=... HTTPS_PROXY=... \
        timeout 60 npx -y @modelcontextprotocol/server-github | head -3
```

## References
- https://nodejs.org/learn/http/enterprise-network-configuration
- `NODE_OPTIONS` docs: https://nodejs.org/api/cli.html#node_optionsoptions
