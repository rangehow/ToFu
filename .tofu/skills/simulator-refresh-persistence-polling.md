---
name: simulator-refresh-persistence-polling
description: Fix for simulator losing all progress on browser refresh: convert SSE to server-side event store + polling, persist task_id in sessionStorage, resume from cursor=0 on refresh to replay all events
enabled: true
tags: [javascript, python, simulator, refresh, polling, sessionStorage, persistence, sse-replacement]
created: 2026-03-29T11:11:35Z
updated: 2026-03-29T11:11:35Z
---

# Simulator Refresh Persistence via Server-Side Event Store + Polling

## Problem
The simulator had two progress phases (fetch data + sim run). Both lost all progress on browser F5 refresh:
- **Fetch phase**: Used polling but `task_id` was stored only in a JS variable — lost on refresh
- **Sim run phase**: Used SSE streaming — connection drops on refresh, events are gone forever
- **All UI state** (`_simState`, log history, equity points, timeline) lived in JS module variables — wiped on refresh

## Solution: Server-Side Event Store + sessionStorage

### Backend Pattern
Both fetch-data and sim-run use the **same polling pattern**:
1. `POST /sim/run` → starts background thread, returns `{task_id}` immediately
2. Background thread calls `_append_event(task_id, evt)` for each event
3. Events are stored in `_tasks[task_id]['events']` list (in-memory, TTL=1 hour)
4. Frontend polls `GET /sim/run-progress/<id>?cursor=N` every 1.5s for new events
5. On refresh, frontend polls from `cursor=0` — replays ALL events to rebuild UI

### Frontend Pattern
```javascript
// Save to sessionStorage for refresh recovery
sessionStorage.setItem('sim_state', 'run');
sessionStorage.setItem('sim_run_task_id', taskId);
sessionStorage.setItem('sim_params', JSON.stringify({startDate, endDate, ...}));

// On loadSimulator(), check sessionStorage
var savedState = sessionStorage.getItem('sim_state');
if (savedState === 'run') {
  var taskId = sessionStorage.getItem('sim_run_task_id');
  if (taskId && !_activeSimPoll) {
    _resumeSimPoll(taskId);  // polls from cursor=0, replays all events
  }
}
```

### Key Design Decisions
- **Unified task store**: Both fetch and sim share `_tasks` dict + `_create_task()` / `_append_event()` / `_finish_task()` helpers
- **No SSE at all**: Polling is both proxy-safe AND refresh-safe
- **Timeline rebuild**: `sim_step_done` events are replayed to rebuild timeline DOM entries + equity chart
- **Clear on "go back"**: `goBackToSetup()` clears sessionStorage + cancels poll timers
- **Session list**: Results phase can recover by loading session from server via `viewSimSession(sessionId)`

## Files Modified
- `routes/trading_simulator.py` — Unified task store, converted sim-run from SSE to polling
- `static/js/trading/simulator.js` — sessionStorage persistence, resume polling on refresh

