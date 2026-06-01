---
name: vscode-disconnect-recovery-network-offline
description: VSCode disconnect recovery: automatic server_offline conversation recovery via online event, visibilitychange, and periodic 15s health check polling
enabled: true
tags: [sse, recovery, network, vscode, frontend]
created: 2026-04-10T00:06:56Z
updated: 2026-04-10T00:06:56Z
---

# VSCode Disconnect Recovery System

## Problem
When VSCode port forwarding drops during a task, the SSE connection dies. The circuit breaker
in `_pollFallback` eventually marks the conversation with `finishReason='server_offline'`.
Previously, recovery only happened on page reload (via `initActiveTasks()` Case F).

## Solution (implemented 2026-04-09)

### Recovery Triggers (core.js)
1. **`window.addEventListener('online')`** — fires when browser detects network is back
2. **`document.addEventListener('visibilitychange')`** — fires when user switches to tab (e.g. after VSCode reconnects)
3. **Periodic 15s polling** (`_startOfflineRecoveryPolling()`) — for cases where neither event fires (tunnel drop ≠ real offline)
4. **Manual "🔄 Reconnect" button** — shown on server_offline finish tag (onclick calls `_recoverOfflineConversations('manual_button')`)

### Core Function: `_recoverOfflineConversations(trigger)`
- Debounced (5s cooldown)
- Finds all conversations with `finishReason='server_offline'`
- Health-checks server first
- Fetches server version of each conversation
- Adopts server content if it has more (task completed after frontend gave up)
- Clears error text, re-renders UI, shows toast

### Circuit Breaker Enhancement (ui.js `_pollFallback`)
- After initial health check failure, enters **2-minute recovery wait** instead of immediately giving up
- Checks health every 5s during recovery wait
- If server comes back → resets errors and resumes poll loop
- If still dead after 2min → force-finishes with server_offline + starts periodic recovery polling

### Key Variables
- `_lastOfflineRecoveryAttempt` — debounce timestamp
- `_offlineRecoveryInterval` — setInterval ID for periodic recovery (auto-clears when no more offline convs)
- `_OFFLINE_RECOVERY_COOLDOWN` = 5000ms
- Recovery wait: `_RECOVERY_WAIT_MS` = 120000ms (2 min), `_RECOVERY_POLL_MS` = 5000ms (5s intervals)

### Files Modified
- `static/js/core.js` — `_recoverOfflineConversations()`, `_startOfflineRecoveryPolling()`, online/visibility listeners, `_forceFinishDeadStream()` update
- `static/js/ui.js` — `_pollFallback` circuit breaker enhancement, Reconnect button in finish tag

