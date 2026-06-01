---
name: autopilot-mode
description: Autopilot (virtual user) mode design: VU auto-replies at every natural stop and ask_human
enabled: true
tags: [autopilot, task-loop, endpoint, design]
created: 2026-05-13T16:28:32Z
updated: 2026-05-20T09:59:32Z
---

---
name: autopilot-mode
description: Autopilot (virtual user) mode design: VU auto-replies at every natural stop and ask_human, with full streaming of VU's content + tool calls
enabled: true
tags: [autopilot, task-loop, endpoint, design, streaming]
---

# Autopilot mode (virtual user)

Toggle on the conversation (`conv.autopilotEnabled`, cfg key `autopilot`)
that makes the LLM keep going without a real human. Mutually exclusive
with endpoint mode (frontend toggles auto-disable each other; backend in
`_start_task_for_conv` defensively drops `autopilot` when `endpointMode`
is also on).

## Two trigger points

1. **Natural stop** — `_finalize_and_emit_done` calls
   `maybe_run_autopilot(task)` BEFORE emitting `done`. Streaming pipeline
   (see "VU streaming" below) makes the synthetic user bubble show up
   IMMEDIATELY when the worker stops, with content + tool rounds
   streaming in live as the VU sub-task runs.
2. **`ask_human` interception** — `lib/tasks_pkg/handlers/misc.py
   ::_handle_ask_human` checks `is_autopilot_enabled` BEFORE blocking on
   `request_human_guidance`; if on, calls the VU directly and resolves
   the guidance with the synthetic answer. NO streaming pipeline yet on
   this path (the response renders inside the assistant's tool round,
   not as a separate user message).

## VU streaming (2026-05-20)

The natural-stop path streams the VU's reply + tool calls into the
synthetic user bubble live, so the user sees activity from the moment
the worker finishes — instead of waiting for the VU to fully complete
and then having the bubble "pop in" all at once.

**Backend (`lib/tasks_pkg/autopilot.py`):**
- `maybe_run_autopilot` now mints a `vu_msg_id` upfront and:
  1. `_append_empty_vu_placeholder_to_conv` — writes `{role:user,
     content:"", _msgId, _isVirtualUser:true, _streamingVu:true,
     toolRounds:[]}` to the conv DB so a refresh mid-stream still sees
     the bubble.
  2. emits `autopilot_vu_start` SSE event carrying `vuMsgId` + the
     placeholder dict.
  3. runs `run_virtual_user(task, vu_msg_id=...)` which pipes through
     `_VUEventForwarder(parent_task, vu_msg_id)`.
  4. on success: `_finalize_vu_placeholder_in_conv` (locates by
     `_msgId`, updates content + toolRounds in place, drops
     `_streamingVu`) and emits `autopilot_vu_done`.
  5. on bail-out (TASK_DONE / aborted / queued real user msg):
     `_delete_vu_placeholder_from_conv` + emit `autopilot_vu_cancel`.
- `_VUEventForwarder` now wraps every VU sub-task event of type in
  `_VU_FORWARD_TYPES` (delta, phase, tool_start, tool_result,
  tool_progress, tool_complete, tool_compacted, stdin_*,
  write_approval_request, human_guidance_*) as
  `{type:'autopilot_vu_event', vuMsgId, inner: ev}` on the parent
  stream. The parent-stream `phase: autopilot_thinking` chip is
  preserved for backward compat.
- The `done` event STILL carries `autopilotNextTaskId` +
  `autopilotVuMessage` so cold-replay / late-connect clients can
  reconcile. Streaming clients dedup by `_msgId`.

**Frontend (`static/js/ui.js`, `static/js/main.js`):**
- `_processSSELine` has 4 new branches: `autopilot_vu_start`,
  `autopilot_vu_event`, `autopilot_vu_done`, `autopilot_vu_cancel` →
  all routed to `_handleAutopilotVuEvent(convId, ev)`.
- `_findVuMsgById(conv, vuMsgId)` locates the VU msg by stable id (NOT
  by tail position).
- `_surgicalRerenderMsg(convId, idx)` repaints just that bubble; new
  inserts go AFTER `#streaming-msg` (so DOM order stays
  parent_user → parent_assistant_streaming → VU_user → next).
- `_handleAutopilotVuEvent` for inner events:
  - `delta` → append to `vuMsg.content` / `vuMsg.thinking`
  - `tool_start` → push to `vuMsg.toolRounds`
  - `tool_result` / `tool_progress` / `tool_complete` /
    `tool_compacted` → update the matching round
  - `phase` → ignored (parent chip already covers it)
  - stdin / approval / hg → logged + ignored (VU bubble doesn't host
    interactive widgets)
- `_attachAutopilotFollowup` (in `main.js`) DEDUPS by `_msgId`: if the
  VU msg with the matching id is already in `conv.messages` (streamed
  in), it reconciles in place instead of pushing a duplicate.

## VU stop signal

The ONLY graceful stop is the VU emitting `[VU: TASK_DONE]` (constant
`_VU_DONE_SENTINEL` in `autopilot.py`). Per user spec:
- NO turn cap.
- NO state-change "no progress" watchdog.
- Empty VU output is treated as a valid "keep going" reply (NOT a stop).

Other stops are external: real user clicking Stop (`task['aborted']`),
real user sending a new message (auto-aborts via
`abort_running_tasks_for_conv`), or LLM error.

## VU has the SAME TOOLS as the worker

`run_virtual_user` runs through `orchestrator._run_single_turn` as a
**fresh sub-task** that inherits the parent's `task['config']` verbatim,
so `_assemble_tool_list` builds the SAME tool set the worker sees.
Sub-task wiring:
- `convId=''` → stays out of `_conv_latest_task` registry, out of conv
  DB sync (`_sync_result_to_conversation` short-circuits on
  `_inline_messages=True`).
- `_inline_messages=True`, `_vu_subtask=True`, `_autopilotParent=<id>`
- `sub_cfg['autopilot']=False`, `endpointMode=False`,
  `humanGuidanceEnabled=False`.
- Strips checkpoint/continue keys.
- A daemon thread `autopilot-abort-mirror-<tid>` mirrors
  `parent.aborted` onto `sub_task['aborted']`.

## VU prompt rules (`_VU_ROLE_PROMPT`)

- Code/engineering tasks → pick the most robust long-term solution; do
  NOT optimize for cost / speed of impl / backward-compat.
- Open-ended discussion → use own judgment, stay concrete.
- Reply in 1-3 sentences, same language as the assistant.
- Output ONLY the reply text, no preamble.
- Emit `[VU: TASK_DONE]` exactly when the assistant has clearly finished.
- Tool use is optional.

The role prompt is appended as a TRAILING user-turn directive
(`_isVuDirective: True`) on top of the parent's full message list —
SAME pattern as `endpoint_review._run_planner_turn` /
`_run_critic_turn`.

## Pitfalls

- DO NOT spawn the autopilot follow-up before `persist_task_result`
  finishes. Order: hook → done event → persist (inside
  `_finalize_and_emit_done`); we set
  `task['_autopilot_spawned_followup']` and the queue dispatcher defers.
- `_start_followup_task` strips checkpoint/continue cfg keys.
- Cross-talk dedup in `_sync_result_to_conversation` tolerates +2 msg
  drift.
- The VU sub-task ALSO runs `_inject_system_contexts`, but the existing
  markers trigger the idempotency guards so it's a no-op.
- `_VUEventForwarder` MUST be installed AFTER `create_task('', …)` and
  BEFORE `_run_single_turn(sub_task)`.
- IDB cache: `_handleAutopilotVuEvent` calls `ConvCache.put(conv)` on
  `start`/`done`/`cancel` (not on every inner event — too noisy).
  Mid-stream reload reads the placeholder skeleton from server DB.
- `_autopilotPending` in the `done` event is stamped on the PARENT's
  assistant message (not the VU msg). `finishStream` →
  `_findAutopilotPendingCarrier` → `_attachAutopilotFollowup` reconciles
  by `_msgId` so the streaming pipeline and cold-replay agree.

