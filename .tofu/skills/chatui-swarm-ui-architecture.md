---
name: chatui-swarm-ui-architecture
description: chatui swarm UI: swarm panel rendered inline as a row of the unified ptool-panel; preserves chronological tool order
enabled: true
tags: [javascript, python, swarm, ui, sse, streaming, architecture]
created: 2026-03-16T08:01:15Z
updated: 2026-06-01T02:17:52Z
---

# Swarm UI Architecture in chatui (frontend, post-inline-merge)

## Tools the frontend renders specially
- `spawn_agents`     — emits `_swarm: True` on the round, upgraded to the full **swarm panel** (`_buildSwarmPanelHTML`). One panel per spawn; follow-ups (re-issuing `spawn_agents` while a session is alive) reuse the same panel via `_findSwarmRound`.
- `await_agents`     — regular tool round, no `_swarm` flag. Display label `⏳ Awaiting N agents (any|all)` via `_TOOL_DISPLAY.await_agents`.
- `get_agent_result` — regular tool round, label `📥 Fetching result for <agentId>`.

## Layout — swarm panels are inline rows of `.ptool-panel`
Since 2026-06, the swarm dashboard is NOT rendered in a separate
`.swarm-round-container`. It lives inside `.ptool-panel-body` as a
`<div data-prn="N" data-prn-kind="swarm">` slot, in the exact same
chronological order as every other tool round. That way users can
see the order in which the main agent issued `spawn_agents` →
`await_agents` → `get_agent_result` (those are just tools to it).

Key changes:
- `static/js/ui/tool_rounds.js`:
  - `renderToolRoundsHTML` no longer splits swarm vs non-swarm —
    everything goes through `_renderUnifiedGroup`.
  - `_renderUnifiedGroup` wraps each round in
    `<div data-prn="..." [data-prn-kind="swarm"]>` and dispatches
    swarm rounds to `_buildSwarmPanelHTML(r)` (full dashboard) and
    non-swarm rounds to `_renderUnifiedToolLine(r, …)`.
- `static/js/ui/streaming_ui.js::_syncToolRoundsDOM`:
  - The previous `swarmRounds` bucket + dedicated `[data-rid=swarm-N]`
    container loop is REMOVED.
  - In the unified slot loop, swarm rounds force
    `contentVisibility: 'visible'` (panels are tall, can't be 32px
    intrinsic) and ALWAYS rebuild via `slot.innerHTML =
    _buildSwarmPanelHTML(round)` — the fingerprint gate already
    guards against pointless re-renders.
  - Stale `.swarm-round-container` elements (from old caches) get
    cleaned up at sync time.
- `static/styles.css`:
  - `.ptool-panel-body > [data-prn][data-prn-kind="swarm"]` (and
    `:has(.sw-panel)` as a fallback) gets `content-visibility:visible`.
  - `.ptool-panel-body > [data-prn] > .sw-panel { margin: 4px 10px }`
    so the panel sits flush as a row of the timeline (instead of
    its old `margin: 12px 0`).

## SSE event flow (unchanged)
```
SubAgent / MasterOrchestrator
  → on_progress / on_event callback (passed through integration.py)
  → append_event(task, ev) in executor.py
  → SSE stream to frontend
```

### Key events
- `tool_start` with `_swarm: True` — creates the swarm slot inside
  `ptool-panel-body` (only `spawn_agents`)
- `swarm_phase(spawning|spawn_more)` — populates `_swarmAgents`
- `swarm_phase(complete)` — terminal: clears `_swarmActive` + `_asyncRunning`
- `swarm_agent_phase|progress|complete|error` — per-agent updates
- `swarm_inbox_inject` — emitted from
  `lib/tasks_pkg/orchestrator.py` whenever the inbox drain hook
  prepends `<swarm-update>` user messages. Frontend stamps a chip
  (`.sw-inbox-chip`) on the current assistant bubble — still
  rendered in the streaming zone `data-zone="swarmInbox"`, NOT
  inside the ptool-panel.

## Cross-message lookup (async-only quirk — unchanged)
`_findSwarmRound` / `_findOwningSwarmRound` walk `conv.messages`
backwards because `swarm_phase complete` and per-agent events can
fire after the spawning assistantMsg already finalized.

## Frontend round type detection
`_isRoundSwarm(round)` checks `round._swarm === true` AND has
`_swarmAgents.length || results.length`. `await_agents` /
`get_agent_result` deliberately fail this check so they render as
compact tool rows alongside everything else.

## Visual elements (post async migration, unchanged)
- `.sw-pill-async` — amber `⏳ N running async` pill on the panel header
- `.sw-inbox-chip` — between-message chip "📨 received N async swarm updates"
- streaming zone `data-zone="swarmInbox"` (above the ptool zone)
- `.sw-panel` itself — the agent dashboard, now rendered as a row.

## Key Files
- `static/js/ui/tool_rounds.js`:
  - `_TOOL_DISPLAY` for `spawn_agents` / `await_agents` / `get_agent_result`
  - `renderToolRoundsHTML` + `_renderUnifiedGroup` (inline swarm dispatch)
  - `_isRoundSwarm`
- `static/js/ui/streaming_ui.js`:
  - `_syncToolRoundsDOM` (single unified slot loop)
  - `_buildSwarmPanelHTML` + `_buildSwarmDoneHTML` + `_buildSwarmInboxChipsHTML`
  - SSE handlers `swarm_phase` / `swarm_agent_phase` / `swarm_inbox_inject`
- `static/styles.css`:
  - `.sw-panel`, `.sw-status-pill`, `.sw-agent-card`, etc.
  - `.ptool-panel-body > [data-prn][data-prn-kind="swarm"]` content-visibility override
- `lib/tasks_pkg/tool_display.py:_tool_display_swarm` — sets `_swarm: True` ONLY for `spawn_agents`
- `lib/tasks_pkg/orchestrator.py` — emits `swarm_inbox_inject`

