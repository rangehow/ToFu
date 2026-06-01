---
name: flask-quart-async-migration
description: Complete async architecture: Quart+Hypercorn, spawn_task (asyncio.to_thread), WebSocket, async_stream_chat (httpx), VS Code auto-detect
enabled: true
tags: [quart, flask, async, http2, migration, architecture]
created: 2026-05-22T03:58:20Z
updated: 2026-05-22T06:55:53Z
---

# Server Architecture (2026-05-22) — Async Migration Complete

## Entry Point: server.py (Quart + Hypercorn)

### Shim: `sys.modules['flask'] = quart`
All `from flask import X` resolves to Quart. Patched methods:
`request.get_json()`, `get_data()`, `form`, `files`,
`send_from_directory`, `send_file`, `make_response`.

### Auto-TLS + VS Code Detection
- Detects `VSCODE_PROXY_URI` → disables TLS (proxy provides HTTPS)
- Otherwise: `cryptography` generates self-signed cert
- `TOFU_TLS=0` / `TOFU_TLS=1` / `--no-tls`

### Critical Timeouts
```python
app.config['RESPONSE_TIMEOUT'] = None  # was 60s!
app.config['BODY_TIMEOUT'] = None
hconfig.keep_alive_timeout = 600
```

## Task Spawning: `spawn_task()` (lib/tasks_pkg/__init__.py)
- SINGLE entry point replacing all 6 `threading.Thread(target=run_task)` sites
- Inside event loop: `asyncio.ensure_future(asyncio.to_thread(run_task))`
- Outside event loop: falls back to daemon thread
- Call sites converted: routes/chat.py (×3), lib/message_queue.py,
  lib/agent_backends/builtin.py, lib/tasks_pkg/autopilot.py

## Streaming Stack

### Backend Transports
1. **WebSocket** `/api/chat/ws/<task_id>` — bidirectional, proxy-immune
2. **SSE** `/api/chat/stream/<task_id>` — async generator
3. **Poll** `/api/chat/poll/<task_id>` — sync fallback

### Frontend (ui.js)
- `_tryWebSocket()` → `_trySSE()` → `_pollFallback()`
- WS URL from `window.location` + `apiUrl()`
- 3s connect timeout, seamless fallback

## LLM HTTP Client

### lib/llm/stream.py (sync — called from thread pool)
- `stream_chat()` — `requests.post(stream=True)`
- Used by the orchestrator (runs in `asyncio.to_thread`)

### lib/llm/astream.py (async — native event loop)
- `async_stream_chat()` — `httpx.AsyncClient.stream()`
- Same retry, error classification, diagnostics
- Ready for when orchestrator converts to native async

### lib/llm/_transport.py
- `async_abortable_sleep()` alongside sync version

## Dependencies
quart>=0.20, hypercorn>=0.17, cryptography>=42.0, httpx>=0.28

