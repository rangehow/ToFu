---
name: chatui-swarm-dashboard-v2
description: Swarm duplicate-agent-card bugs: agent_id mismatch (spec.id vs agent-{role}-{id}), double AGENT_START emission (scheduler + SubAgent), objective truncation mismatch (120 vs 200), stale .pyc bytecode, TOCTOU in add_specs dedup
enabled: true
tags: [swarm, ui, dashboard, javascript, python, bug-fix, agent-id, dedup, sse, duplicate-cards, lifecycle-events]
created: 2026-03-16T10:58:15Z
updated: 2026-03-19T19:37:45Z
---

# Swarm Dashboard Duplicate Agent Card Bugs

## Root Cause: Dual Identity System
SubAgent has TWO identities:
- `self.agent_id` = `f"agent-{role}-{spec.id}"` (for logging/display)
- `self.spec.id` = `uuid` (the canonical event ID)

Bug: `_emit_event()` used `self.agent_id` → `agentId: "agent-general-XXX"` 
But spawning events use `spec.id` → `agentId: "XXX"`
Frontend creates cards from spawning, can't match by ID → creates duplicates.

## Fix Architecture: Single Authoritative Event Source
The scheduler's `_on_agent_start_callback` is the ONLY source for AGENT_START events.
SubAgent.run() should NOT emit AGENT_START — it causes:
1. Duplicate cards (if IDs mismatch)
2. Phase regression from 'running' → 'starting' (even if IDs match)

## Objective Truncation
ALL event sources must use the SAME truncation length (200 chars):
- `integration.py` spawning events: `[:200]`
- `master.py` _on_agent_start_callback: `[:200]`
- `master.py` _on_agent_complete_callback: include `objective[:200]`
- `agent.py` _emit_event: `[:200]`

Frontend fallback matching: use bidirectional `startsWith()` instead of exact `===`.

## Stale .pyc Bytecode
After editing source, `.pyc` in `__pycache__` may be stale.
Always run: `find lib/swarm/__pycache__ -name '*.pyc' -exec rm {} +`

## Thread Safety: add_specs() TOCTOU
The dedup check (read _completed/_pending/_running) and the add-to-pending 
must happen under a SINGLE `with self._lock:` acquisition.
Two separate lock acquisitions allow a _run_one thread to modify state between reads.

## Event Flow (Correct):
1. integration.py → `spawning` phase event with agents array (spec.id as agentId)
2. scheduler._run_one → _on_agent_start_callback → `swarm_agent_phase` running
3. agent._emit_event → progress/tool updates only (NOT start/complete lifecycle)
4. scheduler._run_one → _on_agent_complete_callback → `swarm_agent_complete`

