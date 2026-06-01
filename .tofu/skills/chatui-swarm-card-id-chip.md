---
name: chatui-swarm-card-id-chip
description: Swarm sub-agent cards now show role + 8-char ID chip; backend logs ID transitions in app.log under same token
enabled: true
tags: [swarm, ui, logging, agent-id]
created: 2026-05-28T11:59:34Z
updated: 2026-05-28T11:59:34Z
---

# Swarm Agent Card — Role + ID Chip + Log Echo

## UI (`static/js/ui/streaming_ui.js`, `_buildSwarmPanelHTML`)
Each agent card header shows, in order:
1. Status icon
2. `#N` task index (compact, mono)
3. `<span class="sw-a-role-tag">{role}</span>` — role tag, color-coded by status
4. `<span class="sw-a-id">{spec.id[:8]}</span>` — clickable; onclick copies the
   full backend grep token `agent-{role}-{spec.id[:8]}` to clipboard.
   `data-grep` carries the token; `.sw-a-id-copied` adds a transient ✓ feedback.
5. Phase pill (pushed right via `margin-left:auto`)

## CSS (`static/styles.css` — anchored after `.sw-a-role`)
- `.sw-a-num`, `.sw-a-role-tag`, `.sw-a-id`, `.sw-a-id-copied` rules added.
- `.sw-a-header .sw-a-phase-pill { margin-left:auto }` pushes it to the right
  edge in the new layout.

## Backend log echoes (`lib/swarm/master.py`)
`_on_agent_start_callback`, `_on_agent_complete_callback`, `_on_retry_callback`
each log:
```
[Master:%s] AGENT_START agent-%s-%s role=%s objective=%.120s
[Master:%s] AGENT_COMPLETE agent-%s-%s status=%s elapsed=%.1fs ...
[Master:%s] AGENT_RETRY    agent-%s-%s attempt=%d err=%.200s
```
The `agent-{role}-{spec.id}` token matches `SubAgent.agent_id` (in
`lib/swarm/agent.py:96`) so users can grep one ID across both Master and
Agent log lines.

## Why this matters
Before: cards displayed only "Task 1" / "Task 2" — frontend had `data-agent-id`
but it was invisible. Logs already used `[Agent:agent-{role}-{id}]` but the user
had no way to learn which UI card mapped to which log line. Now copying the chip
gives the exact grep token.

