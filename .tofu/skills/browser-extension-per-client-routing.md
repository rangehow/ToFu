---
name: browser-extension-per-client-routing
description: Browser extension per-client command routing: clientId in extension, target_client filtering, thread-local _active_client propagation, /api/v1/browser/clients
enabled: true
tags: [browser-extension, per-client, routing, thread-local, architecture, cross-device]
created: 2026-03-27T02:18:58Z
updated: 2026-05-29T03:06:51Z
---

# Browser Extension Per-Client Command Routing

## Problem
The browser extension bridge used a **global singleton command queue** — all connected extensions
poll the same queue, causing commands intended for device A to be executed on device B.

## Solution Architecture

### 1. Extension (`browser_extension/background.js`)
- Generates a **stable UUID `clientId`** on first install, persisted in `chrome.storage.local`
- Sends `clientId` with every poll request body: `{ results: [...], clientId: CLIENT_ID }`
- Exposes `clientId` in popup status for visibility

### 2. Server Backend (`lib/browser.py`)
- **`_clients` dict**: tracks `client_id → {last_poll, first_seen, name}` with `_clients_lock`
- **`target_client` field** on each command: routes to specific client or `None` for any
- **`get_pending_commands(client_id=)`**: filters commands — only returns commands where
  `target_client` is None OR matches the requesting client
- **Thread-local `_active_client`**: set via `_set_active_client(cid)`, auto-read by
  `send_browser_command()` when no explicit `client_id` is passed

### 3. Thread-Local Propagation Points
Since tasks run in background threads and tools may spawn additional ThreadPoolExecutor workers:
- **`orchestrator.py` → `run_task()`**: sets `_set_active_client(cfg.get('browserClientId'))`
- **`executor.py` → `_execute_tool_one()`**: re-sets thread-local for worker threads
- **`swarm/agent.py` → `run()`**: sets thread-local for sub-agent threads
- **`swarm/agent.py` → `_dispatch_tool()` cfg**: includes `browserClientId` from parent task

### 4. Frontend (`static/js/main.js`)
- `_checkBrowserStatus()` captures first connected client's ID into `window._browserClientId`
- `startAssistantResponse()` sends `browserClientId` in task config when `browserEnabled` is true

### 5. API Endpoints (post-migration 2026-05-29)
- `GET /api/v1/browser/status` — overall connection state + queue counts
- `GET /api/v1/browser/clients` — connected clients list (per-client routing)
- `GET /api/v1/browser/test?clientId=...` — synthetic round-trip probe
- `POST /api/browser/poll`, `GET /api/browser/commands`, `POST /api/browser/result`,
  `GET /api/browser/download` — **carve-outs**, stay at legacy paths (Bridge-Secret-authenticated
  long-poll RPC + binary zip download, not JSON REST verbs)

### Key Design Decisions
- **Backward compatible**: if `clientId` is not sent (old extension), commands are unrouted (any client)
- **Thread-local pattern**: avoids modifying every tool handler's signature — handlers call
  `send_browser_command()` which auto-reads from thread-local
- **Stale client cleanup**: clients not polling for >5 minutes are removed from `_clients`

