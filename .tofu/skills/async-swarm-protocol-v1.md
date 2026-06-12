---
name: async-swarm-protocol-v1
description: Async swarm v2 (Option A): conv-keyed sessions survive turn end. Phase1: swarm events mirrored to /api/push so panel settles. Phase2: on_settled hook auto-continues main agent (create_task) when swarm settles with pending inbox + idle conv, guarded by chain ceiling + latch
enabled: true
tags: [swarm, architecture, convention]
created: 2026-05-27T16:21:46Z
updated: 2026-06-08T05:30:57Z
---

## v2 — Conversation-scoped sessions (Option A, 2026-06-05)
- `swarm_key_for(task)` = `task['convId'] or task['id']` — SINGLE key for session+inbox. Sessions keyed by swarm_key in `_active_sessions`; `_key_aliases{task_id→key}` for route callers.
- `MasterOrchestrator.inbox_key = inbox_key or conv_id or task_id`; all inbox ops use it.
- `_key_is_live(key)` scans chat task registry for any non-terminal task with convId==key. orchestrator.py finalization: abort+remove+clear ONLY on `task['aborted']` or `sess.is_terminated`; else **DETACH**.
- `await_agents`/`get_agent_result` resolve `_get_session(swarm_key) or _get_session(task_id)`; `_await_from_disk` + `_read_agent_log` cross-task glob fallback.

## Phase 1 (2026-06-08) — cross-turn panel settle via /api/push (fixes stuck "N running async" badge)
Root cause: swarm events emitted ONLY on spawning turn's SSE stream, which closes at turn end → terminal `swarm_phase:complete` never reaches browser.
- `integration._handle_spawn_agents._emit(ev)` ALSO `push_event('swarm', push_conv_id, ev)`. push_conv_id = task['convId'] or cfg['convId'].
- NEW `static/js/ui/swarm_push.js`: `pushSubscribe('swarm','*', fn)`. Skip if `activeStreams.has(convId)` (SSE authoritative). Else replay frame through EXISTING `_handleSwarmPhase`/`_handleSwarmAgent` with synthetic ctx `{convId, taskId:convId, assistantMsg:<swarm-owner>, buf:null, epCritic*}`, then `renderChat(conv,false)` (committed panel, twUpdate only repaints streaming zone).
- Registered in `_BUNDLE_FILES` after `ui/sse_poll_fallback.js`; `<script>` in index.html.

## Phase 2 (2026-06-08) — auto-wake main agent when swarm settles unattended (fixes wasted inbox)
Root cause: swarm finishing AFTER spawning turn ended leaves `<swarm-update>`s in inbox until user sends another msg → sub-agent work sits unseen.
- `MasterOrchestrator.__init__(on_settled=...)`; driver `finally` fires `self.on_settled()` AFTER the terminal `swarm_phase:complete` (so panel shows complete first). Wrapped in try/except — never raises into driver.
- `_handle_spawn_agents` passes `on_settled=lambda k=swarm_key: _maybe_autocontinue(k)`.
- `integration._maybe_autocontinue(swarm_key)`: bails unless `SWARM_AUTOCONTINUE_ENABLED` (env `TOFU_SWARM_AUTOCONTINUE`, default ON); no-op if `_key_is_live(key)` (a turn will drain naturally) or inbox empty. Latch `_autocontinue_inflight` + per-conv counter `_autocontinue_chain` (ceiling `SWARM_AUTOCONTINUE_MAX_CHAIN`, env `TOFU_SWARM_AUTOCONTINUE_MAX` default 3) prevent runaway. On failed start, rolls back the chain increment.
- `_start_autocontinue_turn(conv_id)`: loads conv messages+settings from DB, appends a placeholder assistant msg tagged `_swarmAutoContinue`, writes back + FTS, builds config from settings (swarmEnabled default True so model can await/fetch), `create_task` + sets activeTaskId + `spawn_task`. Injects NO user message — the orchestrator's round-0 inbox-drain hook prepends the `<swarm-update>`s exactly like a human "continue" turn. Emits push frame `{type:'swarm_autocontinue_started', convId, newTaskId}` (NOT 'taskId' — hub frame is `{channel, taskId:<routing=conv_id>, **payload}` so a payload taskId would clobber routing).
- `reset_autocontinue_chain(key)` called from `run_task` start when `not cfg.get('_swarmAutoContinue')` (human turn resets counter; auto-continue turns don't, so the ceiling bounds only unattended loops). `_cleanup_stale_sessions` drops chain/inflight for reaped convs (lock order: `_sessions_lock`→`_autocontinue_lock`).
- Frontend `swarm_push.js::_attachAutoContinue(convId, newTaskId)`: opens SSE via `connectToTask` for the backend turn the browser didn't POST; pushes a trailing placeholder if needed (Case-A pattern); sets conv.activeTaskId. `chat_render.js` renders a `↻ Continued automatically after sub-agents finished` banner for `msg._swarmAutoContinue` (next to proactive banner).

## Key files
- `lib/swarm/integration.py` — _emit dual-emit, _maybe_autocontinue, _start_autocontinue_turn, reset_autocontinue_chain, SWARM_AUTOCONTINUE_*.
- `lib/swarm/master.py` — on_settled param + driver finally hook.
- `lib/tasks_pkg/orchestrator.py` — chain reset on human turns + DETACH teardown + inbox drain hook (round-0 prepends <swarm-update>).
- `static/js/ui/swarm_push.js` — push subscriber + _attachAutoContinue.
- `static/js/ui/chat_render.js` — _swarmAutoContinue banner.

## Tests
`tests/test_swarm_async.py` — 45 pass (TestAutoContinueGuardrails: fires-when-idle, skips-live, skips-empty, chain-ceiling, failed-start-rollback, disabled-noop, reset-clears). Run `python3 -m unittest tests.test_swarm_async` (system pytest broken). After editing swarm src: `find lib/swarm/__pycache__ -name '*.pyc' -delete`. Rebuild bundle: `python3 -c "from lib.js_bundler import build_bundle; build_bundle()"`.
