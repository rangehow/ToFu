# EPIC: Autopilot VU as an Independent Stream

Board epic: `pt_8dc030176bad450b`
Status: **DESIGN + MIGRATION-TEST PLAN complete; implementation HUMAN-GATED** (touches
the exactly-once baton delivery contract the owner personally owns).
Related, already-shipped: the *visible* "parent finish bar incomplete" bug is
root-fixed independently of this epic by projecting `parentMessage` on
`autopilot_vu_start` + a task-settled-fields fallback (commits `589cfaa` /
`b221921` / `9ce7d93`). **This epic is NOT needed to close that bug** — it is the
deeper structural cleanup ("an agent bubble is just an agent bubble").

---

## 1. What the epic asks for

> Stop the VU riding the parent SSE via `_VUEventForwarder` + the
> `_autopilot_deciding` latch; emit the parent `done` at parent-turn end and run
> the VU as its own task/connection, with the follow-up baton delivered on a
> channel independent of `done`. Preserve exactly-once handoff (poll fallback +
> SSE) — must re-verify `test_autopilot_poll_handoff.py`'s 4 race guards.

## 2. Current mechanism (code-verified)

The autopilot turn is **synchronous inside the parent's finalize**:

| Step | Site | Fact |
|---|---|---|
| Parent turn ends, done event built | `_finalize.py:834` `done_evt = build_event(DONE)` | finish fields settled on `task` (finishReason L621 / usage L622,917 / model L847 / apiRounds L925) |
| Pre-emit conv sync | `_finalize.py:1029` `_sync_result_to_conversation` | stamps `task['_committedMsg']` |
| **Decision latch ON** | `_finalize.py:1040` `task['_autopilot_deciding']=True` | gates the SSE generator's `_task_terminal()` so it will NOT synthesize a premature done |
| **VU runs SYNCHRONOUSLY** | `autopilot.py:949` `_run_single_turn(sub_task)` | the whole VU LLM turn (12–52s) blocks here |
| VU events ride parent SSE | `autopilot.py:922` `sub_task['events']=_VUEventForwarder(task, vu_msg_id)` | inner deltas wrapped as `autopilot_vu_event`, routed into the VU bubble created by `autopilot_vu_start` |
| Baton computed | `autopilot.py` returns `{next_task_id, vu_msg}` | successor task ALREADY spawned by `_start_followup_task` |
| Baton attached to done | `_finalize.py:1044-1046` `done_evt['autopilotNextTaskId']=…` | + stashed on `task['_autopilot_followup']` for the poll path |
| Late done emitted | `_finalize.py` `append_event(done_evt)` AFTER the hook | this is why `done` is "withheld" until the VU finishes |

**Why `done` is withheld is not arbitrary:** `done` must *carry the baton*, and
the baton can only be computed *after* the VU has run and decided
(continue-vs-`TASK_DONE`). The withhold is a direct consequence of running the
VU inline before emitting done.

**What is ALREADY independent:** the *successor* follow-up task. The frontend
`_attachAutopilotFollowup` (`main_send_pipeline.js:843`) opens a **fresh**
`connectToTask(convId, nextTaskId)` — the follow-up streams on its own SSE. Only
two things ride the parent stream today:
1. the **VU bubble's live streaming** (via `_VUEventForwarder`), and
2. the **baton delivery timing** (on the withheld `done`).

## 3. Why the two coupled parts are irreducible without a new channel

Releasing `done` at parent-turn end (before the VU runs) has a forced cascade:

1. If parent `done` fires early, the parent SSE **closes** → the VU bubble has no
   stream to forward onto → `_VUEventForwarder` is dead → the VU **must** become
   its own attachable task with its own SSE/push channel.
2. If the VU is its own task, the baton (which conceptually is "after this
   parent turn, a VU turn then a follow-up turn happen") can no longer ride the
   parent `done` — it needs an **independent delivery channel** that STILL
   satisfies exactly-once across BOTH transports (SSE + `/api/v1/chat/poll`
   fallback) AND the cold-replay/late-connect synthesis path.

So "emit done early" ⟺ "VU independent" ⟺ "new baton channel" — you cannot do one
without all three. This is why it is a contract change, not a local edit.

## 4. Target design

Introduce an **autopilot chain descriptor** as the independent, transport-agnostic
baton, decoupled from the parent `done` event:

- **`task['_autopilot_chain']`** (persisted sidecar on the parent task + mirrored
  to a conv-level field): `{vuTaskId, vuMsgId, nextTaskId?, state: 'vu_running'|'vu_done'|'spawned'|'done_no_followup'}`.
  This is the SINGLE SOURCE OF TRUTH, obeying the charter's front/back contract
  invariant (backend computes the lifecycle fact + STABLE IDs; frontend is a pure
  reducer). It replaces the `autopilotNextTaskId`/`autopilotVuMessage` fields
  riding `done`.
- **Parent `done` fires immediately** at parent-turn end, carrying only
  `autopilotChain: {vuTaskId, vuMsgId, state:'vu_running'}` when autopilot is
  armed (enough for the frontend to keep the conv "live" and attach to the VU
  task's stream). Drop `_autopilot_deciding` (no more withhold).
- **VU is a normal child task** with its own entry in the task registry, its own
  SSE stream (`/api/chat/stream/<vuTaskId>`) and poll endpoint. The frontend, on
  seeing `autopilotChain.state=='vu_running'`, calls `connectToTask(convId, vuTaskId)`
  exactly as `_attachAutopilotFollowup` already does for the successor — the VU
  bubble streams over ITS OWN connection.
- **VU's own `done`** carries `autopilotChain: {…, nextTaskId, state:'spawned'}`
  (or `state:'done_no_followup'` when the VU emitted `TASK_DONE`). The frontend
  then attaches to `nextTaskId` — same `_attachAutopilotFollowup` code path.
- **Poll fallback + cold-replay** both read `task['_autopilot_chain']` from the
  task dict (like `_autopilot_followup` is surfaced today at `chat.py:2193`), so
  a client on either transport, or a fresh connection landing mid-chain, resolves
  the exact same chain state. `_apply_autopilot_baton` becomes `_apply_autopilot_chain`.

This makes an autopilot run a **plain sequence of independent agent bubbles**
(parent → VU → follow-up), each on its own stream — literally "an agent bubble is
just an agent bubble," which is the owner's stated north star for this cleanup.

## 5. Exactly-once invariants that MUST survive (charter Pillar-#6 + poll-handoff)

The migration is only acceptable if ALL of these hold; each maps to a guard:

1. **A spawned successor is never stranded.** (poll-handoff `test_baton_surfaced_when_followup_spawned`)
2. **No premature/baton-less finalization during the decision window.** With the
   withhold removed this reframes: a client attaching mid-VU must land on the VU
   task's stream (not see the conv go idle). (reframes `test_status_gated_while_deciding`
   + `test_sse_holds_open_while_deciding_then_delivers_baton`)
3. **A plain non-autopilot done still closes promptly.** (poll-handoff `test_normal_done_task_unaffected` + `test_sse_normal_done_task_closes_promptly`)
4. **A SYNTHESIZED done (cold-replay, no buffered event) still carries the chain.** (poll-handoff `test_sse_synthetic_done_carries_baton`)

Plus the charter front/back contract: the chain descriptor uses STABLE `_msgId`/
task ids, never array indices or transient client state; when the backend cannot
resolve a chain link it OMITS the field and the frontend's last-resort branch
handles only that residual.

## 6. Migration-test plan (write these FIRST, RED before any prod edit)

New suite `tests/test_autopilot_chain_handoff.py` — the SUPERSET of the current
4 poll-handoff guards, re-expressed against `_autopilot_chain`:

- `test_chain_vu_running_surfaced_on_parent_done` — parent done with an armed
  chain carries `autopilotChain{vuTaskId,vuMsgId,state:'vu_running'}` and does
  NOT carry a `nextTaskId` yet.
- `test_parent_done_fires_immediately_no_withhold` — with the latch removed, a
  parent SSE with autopilot armed emits its `done` promptly (state snapshot →
  done, no multi-second hold), and the done carries `state:'vu_running'`.
- `test_vu_task_own_stream_carries_chain_spawned` — the VU task's OWN done
  carries `autopilotChain{nextTaskId,state:'spawned'}`.
- `test_vu_task_done_no_followup_state` — VU emitted TASK_DONE → VU done carries
  `state:'done_no_followup'`, no `nextTaskId`.
- `test_poll_surfaces_chain_each_transport` — `/api/v1/chat/poll/<vuTaskId>` and
  `/poll/<parentTaskId>` both surface the chain from the task dict.
- `test_cold_replay_synthetic_done_carries_chain` — synthesized done (no buffered
  event) still stamps `autopilotChain` from `task['_autopilot_chain']`.
- `test_plain_done_no_chain` — non-autopilot task: no chain keys, closes promptly.

Frontend jsdom `tests/test_frontend_autopilot_chain_attach.py`:
- parent done with `state:'vu_running'` → frontend calls `connectToTask(vuTaskId)`
  and keeps the conv live (no idle/sidebar-dot-off).
- VU done with `state:'spawned'` → `_attachAutopilotFollowup(nextTaskId)` fires
  once (dedup by stable id; NC = missing chain → no double-attach).

**Keep the existing `test_autopilot_poll_handoff.py` green until the cutover
commit**, then replace it in the SAME commit that removes `_autopilot_deciding`
(the two describe mutually-exclusive worlds; a strangler-fig dual-run is not
possible here because the withhold either exists or it doesn't).

## 7. Build order (each step green before the next; charter §strangler-fig ethos)

1. **Land this doc + the migration-test suite in RED** (tests express the target;
   prod unchanged; suite `@pytest.mark.skip('epic pt_8dc030176bad450b not yet cut over')`
   so the collection gate stays green). ← *safe, additive, NON-contract-touching*
2. Backend: introduce `task['_autopilot_chain']` written ALONGSIDE the existing
   baton (dual-write, both consumed) — byte-identical behavior, new field ignored
   by the frontend. Prove no regression against the CURRENT 4 guards.
3. Frontend: teach `_attachAutopilotFollowup` + the done/poll handlers to PREFER
   `autopilotChain` when present, else fall back to the legacy fields. Still
   behind the withhold.
4. **CUTOVER (the contract change — HUMAN-GATED):** run the VU as an independent
   task, remove `_VUEventForwarder`, drop `_autopilot_deciding`, emit parent done
   early, retire the legacy baton fields. Swap the test suites in the same commit.
5. Cleanup: delete `_autopilot_deciding` references, the `_task_terminal` gate
   branch, the abort-mirror thread's parent-stream assumptions; JOURNAL entry.

## 8. Why implementation is BLOCKED here (not no-op'd)

Steps 1–3 are safe and additive. **Step 4 mutates the exactly-once baton delivery
contract**, which:
- the charter marks as owner-personal (CAS/VU autopilot exactly-once delivery), and
- the owner explicitly gated earlier this conversation: *"值得做但另开 epic;若现在
  做需明确授权动 baton 契约,我会先写迁移测试再动"* (worth doing but a separate epic;
  doing it now needs explicit authorization to touch the baton contract, and I'd
  write migration tests first).

This is a genuine external gate an autonomous turn cannot clear itself: it is not
a sibling-commit dependency (no other conversation's file unblocks it) — it is a
**human authorization** to change a load-bearing delivery contract. Per the
dispatch protocol that is a `[human-gated]` block. The design + migration-test
plan (the "write migration tests first" precondition the owner named) is the
maximal safe progress; the cutover awaits sign-off.
