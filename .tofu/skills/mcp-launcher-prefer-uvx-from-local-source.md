---
name: mcp-launcher-prefer-uvx-from-local-source
description: Long-term MCP launcher pattern: uvx --from /local/path → isolated venv per server, immune to host-PATH/env drift
enabled: true
tags: [mcp, deployment, uvx, convention]
created: 2026-05-10T07:08:23Z
updated: 2026-05-10T07:08:23Z
---

# MCP launcher pattern: prefer `uvx --from <path>`

## Why
Hosting an MCP server's entrypoint as a bare PATH binary (`hope-mcp`,
`xuecheng-mcp`, …) means the launcher's shebang pins the **host** Python
env. If that env doesn't have all of the server's deps (or has the wrong
version of `mcp`), startup fails with `ModuleNotFoundError`. We hit this
with `hope-mcp` resolving to the `sglang` env which had no `mcp`.

`uvx --from <pypi-or-path> <entrypoint>` instead:
- Builds/installs into a **fresh isolated venv** per server, cached.
- Resolves deps from the server's own `pyproject.toml` — no contamination
  from whatever the user's shell env happens to be.
- Works for both PyPI packages and local source dirs (just give the path).

## Recommended config (data/config/mcp_servers.json)

```json
"hope": {
  "command": "uvx",
  "args": [
    "--from", "/abs/path/to/hope-mcp",     // local source dir
    "hope-mcp"                             // [project.scripts] entry
  ]
}
```

For a published package, drop the path:
```json
"args": ["--from", "overleaf-mcp-plus[compile]>=0.1.3", "overleaf-mcp"]
```

## Caveats / lessons

1. **uvx inherits PATH** for child subprocesses but **not site-packages**.
   So if the MCP server itself shells out to a CLI binary (e.g. hope-mcp
   shells out to the `hope` CLI), that binary must still be on the host PATH.

2. **Declare ALL direct imports** in the server's pyproject.toml. We
   discovered hope-mcp imported `requests` directly with a comment
   "transitively provided by the installed hope package" — that comment
   was wrong. Under uvx isolation, transitive deps from sibling packages
   are invisible. Fix: add `requests>=2.28` to `dependencies`.

3. **`uvx --refresh`** rebuilds from a local source dir. Without
   `--refresh`, uv caches by content hash so editing the source AND
   bumping the version (or `pyproject.toml` mtime) is required for the
   cache to invalidate. For dev iteration, `--refresh` is the safe choice.

4. **First-run latency**: uvx may build a wheel + resolve deps on first
   launch (~5-30s). Chatui's `MCP_CONNECT_TIMEOUT * 2 + 5` budget
   accommodates this.

## Example fix log

`/mnt/.../chatui/data/config/mcp_servers.json` — `hope` entry switched
from `"command": "hope-mcp"` to `uvx --from /abs/path/to/hope-mcp hope-mcp`.
Coupled with `requests>=2.28` added to `hope-mcp/pyproject.toml`
dependencies, and the misleading "transitively provided" comment removed
from `hope_api.py`.

