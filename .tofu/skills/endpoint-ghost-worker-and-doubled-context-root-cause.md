---
name: endpoint-ghost-worker-and-doubled-context-root-cause
description: Bug fix: endpoint mode ghost worker after Critic STOP + doubled system context in debug panel — root-cause fixes in system_context (idempotency), endpoint._finalize (authoritative phase='done'), and frontend reconnect paths
enabled: true
tags: [endpoint, bug-fix, ghost-message, system-context, idempotency, reconnect, critic, root-cause]
created: 2026-04-24T08:39:43Z
updated: 2026-04-24T08:39:43Z
---

# Bug: Ghost worker reappears after Critic STOP & system contexts doubled in debug panel

## Symptoms
1. After Critic approved ([VERDICT: STOP]), a duplicate worker bubble
   appeared below the critic in the conversation and got auto-translated.
2. The debug panel (messages_snapshot) showed the SAME messages twice —
   project CLAUDE.md block, static guidance, memory instructions, date —
   all duplicated in the critic's snapshot vs worker's snapshot.

## Root causes (two independent bugs, same task)

### Bug 1 — Ghost worker (frontend phase guessing)
Frontend reconnect paths (`connectToTask` in `static/js/ui.js:~5286` and the
`state` snapshot handler's "else worker" branch at `~5566`) created a new
worker placeholder whenever the last message was a critic review
(`role=user`), without checking if the critic had **approved**. After
SSE reconnect / poll fallback / page-reload Case A, poll's `td.content`
(the last completed worker's content) overwrote the ghost → visible
duplicate.  The backend also left `task['_endpoint_phase']='reviewing'`
after finalize, so the state snapshot misrepresented the post-approval
state as still-reviewing.

### Bug 2 — Context doubled in debug panel
In endpoint mode, `_run_single_turn` runs 3x on the same task
(Planner → Worker → Critic).  Each orchestrator call ran
`_inject_system_contexts` on its message list.  For the Critic,
`critic_messages = [dict(m) for m in worker_messages]` where
`worker_messages` was the post-orchestrator message list with contexts
ALREADY injected.  `_prepend_to_system_message(proj_ctx)` blindly
prepended again → project CLAUDE.md + static guidance + memory +
date all appeared twice in the system message.

## Fixes (all at root cause)

### Backend: `lib/tasks_pkg/system_context.py`
Added `_system_text(messages)` helper + **idempotency guards** to every
section in `_inject_system_contexts`:
- Project context: skip if `'[PROJECT CO-PILOT MODE]' in _existing`
- Static guidance: skip if `'# Function Result Clearing' in _existing`
- Memory: skip if `'<memory_accumulation>' in _existing`
- Swarm: skip if `'<parallel_execution>' in _existing`
- Current date: skip if `f'Current date: {_date_str}' in _existing`

Refresh `_existing = _system_text(messages)` after each mutation so
later steps see the update.

### Backend: `lib/tasks_pkg/endpoint.py` `_finalize`
Set authoritative signals on completion:
```python
task['_endpoint_phase'] = 'done'
task['_endpoint_stop_reason'] = stop_reason
```

### Backend: `routes/chat.py` state-snapshot emission
Propagate `endpointStopReason` to the frontend on reconnect:
```python
if task.get('_endpoint_stop_reason'):
    state['endpointStopReason'] = task['_endpoint_stop_reason']
```

### Frontend: `static/js/ui.js`
Two reconnect paths guarded:

1. `connectToTask` (~line 5286): before pushing a ghost worker, check
   `assistantMsg._epApproved || _epNextPhase==='stop'|'planner'` — if
   any is true, do NOT create the placeholder.

2. `state` snapshot endpoint branch (~line 5547): added explicit
   `ev.endpointPhase === 'done' || ev.endpointStopReason` branch that
   points `assistantMsg` at last assistant but creates no new bubbles.
   Also in the fallback "worker in progress" branch, guard against
   approved-critic-as-last-msg.

## Why this works
- The backend now emits **authoritative phase signals** (`'done'` +
  `endpointStopReason`) that the frontend consults instead of
  guessing phase from message roles.
- `_inject_system_contexts` is now **idempotent**: calling it on an
  already-injected message list is a no-op per section. Protects
  against any future caller that accidentally reuses a mutated list.

## Files changed
- `lib/tasks_pkg/system_context.py` — `_system_text()` + idempotency
  guards in all 5 sections of `_inject_system_contexts`
- `lib/tasks_pkg/endpoint.py` — `_finalize` sets
  `_endpoint_phase='done'` + `_endpoint_stop_reason`
- `routes/chat.py` — SSE state snapshot propagates `endpointStopReason`
- `static/js/ui.js` — `connectToTask` + state-handler worker branch
  guards against ghost worker placeholder after Critic STOP

