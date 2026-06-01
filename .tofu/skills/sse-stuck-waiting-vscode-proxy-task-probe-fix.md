---
name: sse-stuck-waiting-vscode-proxy-task-probe-fix
description: Fix for 'stuck waiting' when VS Code port forwarding / proxy silently swallows SSE done event: stream timer probes task completion via /api/chat/poll when SSE silent 30s+ but server alive, aborts stale SSE with _probeAbort flag to trigger _pollFallback recovery
enabled: true
tags: [javascript, sse, proxy, vscode, bug-fix, streaming, poll-fallback, stuck-waiting, task-probe]
created: 2026-03-30T16:47:59Z
updated: 2026-03-30T16:47:59Z
---

# SSE Stuck Waiting — VS Code Port Forwarding Proxy Fix

## Problem
When using VS Code port forwarding (or any HTTP proxy), the proxy layer can silently
swallow the SSE `done` event. The server has already generated the response, but the
frontend never receives the completion signal. The UI shows "Waiting…" indefinitely.

## Root Cause
The architecture had safety nets for **server death** (health check → force finish) and
**SSE premature close** (stream end detection → poll fallback), but NOT for the case where:
- Server is **alive** (health check passes)
- SSE connection is **open** (no stream-end detected)
- But events are **silently lost** by the proxy

This "alive but silent" gap meant the timer UI showed "Xs no update" + "Force Finish"
button, but never auto-recovered.

## Fix: Task Completion Probe (core.js `_updateStreamTimerUI`)

When SSE is silent for 30+ seconds AND server health check passes:
1. **Probe the task** via `GET /api/chat/poll/{taskId}` 
2. If task status ≠ 'running' → task is done but SSE pipe is stale
3. Set `stream._probeAbort = true` flag (to distinguish from user stop)
4. Abort the SSE controller → `_trySSE` catch block detects `_probeAbort`
5. Returns `false` → `connectToTask` falls through to `_pollFallback`
6. `_pollFallback` retrieves the completed result from the server

## Key Design Decisions

### Why `_probeAbort` flag?
The `_trySSE` catch block for `AbortError` has different handling:
- `!gotData` → initial timeout, fall through to poll (existing)
- `_probeAbort` → timer probe, fall through to poll (**new**)  
- `gotData && !_probeAbort` → user clicked Stop, rethrow to `connectToTask`
  which sets `finishReason='aborted'` and calls `finishStream`

Without the flag, aborting with `gotData=true` would be treated as user stop,
calling `finishStream` with partial SSE content instead of recovering the full
server result via poll.

### Why not skip SSE and always use polling?
SSE provides real-time token-by-token streaming (< 1ms latency per token).
Polling every 500ms would add noticeable choppiness. SSE is the correct choice;
the proxy issue is the exception that needs the safety net.

## Files Changed
- `static/js/core.js` — `_updateStreamTimerUI()`: added task probe after health check
- `static/js/ui.js` — `_trySSE()` catch block: handle `_probeAbort` flag

