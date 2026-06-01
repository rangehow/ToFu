---
name: backend-task-survives-frontend-disconnect
description: Invariant: backend tasks run to completion on daemon threads regardless of SSE/HTTP disconnect
enabled: true
tags: [architecture, sse, backend, invariant, task-lifecycle]
created: 2026-04-23T05:56:27Z
updated: 2026-04-23T06:10:30Z
---

---
name: backend-task-survives-frontend-disconnect
description: Invariant: backend tasks run to completion on daemon threads regardless of SSE/HTTP disconnect
enabled: true
tags: [architecture, sse, backend, invariant, task-lifecycle]
created: 2026-04-23T05:56:27Z
updated: 2026-04-23T06:30:00Z
---

# Backend Task Survives Frontend Disconnect — Invariant

## Contract
Once `/api/chat/start` or `/api/chat/send` returns `{taskId}`, the task MUST
continue in a daemon thread and persist its result to `task_results` +
`conversations.messages` **regardless** of whether the originating frontend is
still connected. Disconnect (tab close, refresh, network drop, SSE timeout)
MUST NOT abort the task.

## Verification status (2026-04-23)
- **Code audit:** ✅ complete. See `a.md` §1–§5 (all 5 backend `task['aborted']=True`
  setters B1–B5 and all 9 frontend `/abort*` sites F1–F9 verified bound to explicit
  user intent, not connection lifecycle).
- **Empirical E2E run:** ⏳ **pending manual execution**. No live server was
  detected on ports 5000/5001/8000/8080/8888/15000 during the audit session, and
  the protocol forbids auto-spawning `server.py` (port conflict / DB lock risk).
  Manual steps documented in `a.md` section "## 手动验证步骤 (Manual Verification)":
  ```bash
  python server.py                                      # shell 1
  python debug/test_backend_task_independence.py \
         --base http://127.0.0.1:15000                  # shell 2
  # Expected: exit 0, "✅ PASS", status=done, finishReason != aborted,
  # and post-disconnect /api/chat/active shows aborted=False.
  ```
  Fail codes: 1=aborted-on-disconnect, 2=status≠done, 3=finishReason==aborted,
  4=empty content. Any FAIL ⇒ do NOT patch the abstraction; `git log` for a
  recent commit introducing a reverse pattern (see below) and revert.

## Audit summary — all paths that set `task['aborted']=True`

All 9 frontend `/api/chat/abort*` call sites + 5 backend setters are bound to
**explicit user intent**:
- Stop button (ui.js:7138/7163/7194/7207)
- deleteConversation (main.js:1098)
- AbortError during translate (main.js:1743/2207, ui.js:3823)
- `_hardCancelActiveStream` in edit/regen (ui.js:707)
- `chat_abort` / `chat_abort_conv` endpoints
- `abort_running_tasks_for_conv` (called by new-task dispatch)
- `chat_send` with `abortTaskId` body param (Stop→immediate resend race)
- endpoint.py:1001 run_task_sync timeout (tests/swarm only)

**NO path originates from connection close.** No `beforeunload` / `pagehide` /
`unload` listener exists anywhere. `visibilitychange` only *recovers*.

## Key mechanisms

- `routes/chat.py:1578` `generate_with_disconnect_log`: `GeneratorExit` → `logger.debug` only, no state mutation.
- `routes/chat.py:1521` `_MAX_SSE_DURATION=7200`: sends `sse_timeout` event (NOT `done`), returns — does NOT abort.
- `lib/tasks_pkg/manager.py:1181` `_STREAM_CHECKPOINT_INTERVAL=5`: persists every 5s during streaming.
- `persist_task_result()` → `_sync_result_to_conversation()` runs at task end (no frontend dependency).
- `_dispatch_queued_message()` runs unconditionally — queue survives disconnects.
- `server.py:1161` `app.run(..., threaded=True)` — Python daemon threads outlive HTTP request.

## Verification artifact
`debug/test_backend_task_independence.py` (172 lines) — POST /send → hard-close SSE → poll until done.

## ❌ Reverse patterns — DO NOT INTRODUCE
- Do NOT set `task['aborted']=True` in `GeneratorExit`, `finally`, or SSE timeout.
- Do NOT add `beforeunload` / `pagehide` / `unload` listeners that call `/abort*`.
- Do NOT introduce worker timeout / recycle config in `app.run` or gunicorn that would kill mid-flight daemons.
- Do NOT make `request_human_guidance` react to SSE disconnect — it polls `task['aborted']` which is the correct decoupled signal.

## Key files
- `routes/chat.py` — `chat_start`/`chat_send`/`chat_stream`/`chat_abort*`, SSE generator.
- `lib/tasks_pkg/manager.py` — `create_task`, `abort_running_tasks_for_conv`, `persist_task_result`, `_sync_result_to_conversation`, `checkpoint_task_partial`, `_STREAM_CHECKPOINT_INTERVAL`.
- `lib/tasks_pkg/orchestrator.py` — `run_task` + per-round checkpoint.
- `lib/tasks_pkg/human_guidance.py` — legal blocking wait.
- `static/js/{main,ui,core}.js` — all `/abort*` call sites are user-intent only.

## Full audit report
`a.md` at project root (includes §1–§5 audit + "## 手动验证步骤" section with run/expected/fail commands).

