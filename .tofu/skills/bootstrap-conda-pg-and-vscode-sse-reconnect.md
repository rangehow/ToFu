---
name: bootstrap-conda-pg-and-vscode-sse-reconnect
description: Bootstrap auto-repair: conda-based PostgreSQL install and VS Code port forwarding SSE reconnect fix for stuck Reconnecting state
enabled: true
tags: [bootstrap, conda, postgresql, vscode, sse, port-forwarding, reconnect]
created: 2026-04-02T14:39:28Z
updated: 2026-04-02T14:39:28Z
---

# Bootstrap: Conda PG Install + VS Code SSE Reconnect Fix

## Problem 1: Bootstrap can't auto-install PostgreSQL
The bootstrap auto-repair only knew `pip install`. PostgreSQL is a system binary installed via conda, not pip.
When `initdb`/`pg_ctl` are missing, the LLM would flag it as "unresolvable".

### Fix
- Added `_need_pg_install()`, `_try_conda_install_postgresql()`, `_is_pg_missing_error()` functions
- Auto-detects missing PG binaries and runs `conda install -c conda-forge postgresql>=18 -y`
- Also checks for `mamba` as an alternative to conda
- Integrated into the flow: after `_try_requirements_txt()` and when PG-specific errors detected
- LLM prompt updated to allow `conda:postgresql>=18` prefix in packages list
- `_pip_install()` handles `conda:` prefixed packages

## Problem 2: "Stuck on Reconnecting…" with VS Code port forwarding
During bootstrap repair, the status server is stopped to free the port before retrying server.py.
The browser's SSE connection drops, triggering `es.onerror` which polls `fetch('/')`.

### Root cause
- VS Code port forwarding may cache "port down" state or return proxy error pages
- `fetch('/').then(r => { if (r.ok) reload() })` would match VS Code proxy pages (200 OK)
- OR the tunnel doesn't refresh fast enough, leaving the page stuck on "Reconnecting…"

### Fix
1. **Emit `done` event BEFORE stopping status server** — browser gets clean redirect signal while SSE alive
2. **Reset `EventBus` after premature done** — when server crashes again after done was sent, new subscribers don't replay stale success
3. **Smart reconnect poll** — verify response body contains ChatUI-specific markers (`ChatUI`, `Tofu`, `bootstrap/events`), not just `r.ok`
4. **Added `global _bus`** declaration in `main()` for proper scoping when resetting EventBus
5. **60s timeout hint** — after 30 polls, shows hint to manually refresh if using VS Code forwarding

