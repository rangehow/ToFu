---
name: separation-of-concerns-directive
description: User directive: strict frontend/backend separation; chatInner settled state is backend-authoritative — completion-workflow consolidation (2026-07-03) made backend the sole WRITER (committedMessage on done) and CLASSIFIER (reconcile.py); finishStream is render-only
enabled: true
tags: [architecture, convention, frontend, backend, separation-of-concerns]
created: 2026-04-14T16:11:41Z
updated: 2026-07-03T13:16:36Z
---

# Separation of Concerns — Frontend vs Backend

**Mandatory directive from user (2026-04-14):**

1. **Do NOT implement logic in the frontend that belongs on the backend.** Business logic, data transformation, validation, and complex decision-making live in Python, not JS.
2. **When moving logic backend-side, clean up the frontend thoroughly.** No dead code / unused vars / orphaned handlers.
3. **Frontend role**: UI rendering, user interaction, API calls, display formatting only.
4. **Backend role**: Business logic, data processing, validation, persistence, LLM orchestration, security.

## Chat-Inner Bubble = Pure Backend Projection (mandatory, 2026-07-02)

A `chatInner` bubble must render from ONE backend-authoritative record. Draw the line at **transient vs settled**:
- **Transient (in-flight stream) — OK.** Accumulating streaming deltas into `streamBufs` / `twUpdate` to paint an in-progress turn.
- **Settled — MUST be a verbatim projection of the backend record.** The moment a turn settles, render from the single authoritative dict the backend committed, verbatim.

Two bans: (1) NO local-buffer fallback for settled state (`= finalMsg.content || buf.content || ...` forbidden — use the backend record directly; empty-when-shouldn't = a BACKEND bug). (2) NO frontend-only lifecycle heuristics (no `_isContentBearing` guess); lifecycle asserted by explicit backend events (`*_start/*_done/*_cancel`).

## COMPLETION-WORKFLOW CONSOLIDATION (2026-07-03) — the big one

Root cause of "too many task-completion mechanisms" (~17 backend + 8 frontend): the frontend was BOTH a writer AND a classifier of settled state, racing the backend. Fixed in 3 phases (docs/CHATINNER_COMPLETION_REFACTOR.md):

- **Phase 1 — single committed dict, shipped verbatim.** The settled record was built 4× (DB dict / `done` event / `state` / `/poll`) synced "by convention"; worse, `append_event(done)` ran BEFORE `persist_task_result` (emit-before-commit). Fix: `_sync_result_to_conversation` stamps `task['_committedMsg']` with the EXACT written tail (re-SELECT-post-CAS, or fresh tail on frontend-won skip); orchestrator hoists the sync BEFORE the done emit for ALL paths; `done_evt['committedMessage']` ships it; the SSE done handler projects it verbatim (content/thinking via `_snapshotLonger` belt-and-braces, toolRounds+meta verbatim). Absent on skip paths → client keeps transient buffer (offline fallback). Tests: test_committed_message_parity.py, sse_dispatch #28/#29.
- **Phase 2 — finishStream render-only.** Removed the full-conv PUT + its 3 skip-guards (queue-race/autopilot-inbound/server_offline) at the finishStream call site ONLY (kept `syncConversationToServer` the function — ~39 other legit callers: edit/branch/regen/image-gen/folder/create). All 3 guards existed SOLELY to suppress the PUT where it raced a backend write → removing the PUT makes them dead. No settled field was carried only by the PUT (messages+activeTaskId+lastMsg* written by `_sync_result_to_conversation`; autopilotSummaries durably written server-side; toggles have own call sites). Kept `ConvCache.put` (local cache) + offline `streamBufs` fallback. Test: test_frontend_finishstream_no_put.py.
- **Phase 3 — backend ghost reconcile.** The 3 JS classifiers (`_classifyGhostTail`/`_isBuriedEmptyGhost`/`_sweepBuriedGhostAssistants`) + Case-D were frontend lifecycle INFERENCE (source of resurrect + auto-fire bugs). Ported to pure `lib/conversations/reconcile.py::reconcile_conversation_messages(messages)->(cleaned,changed)`; wired into `recover_stale_tasks_on_startup` (persists cleaned list in the SAME commit + stamps `settings._reconciledAt`); frontend Case-D gated on `!conv._reconciledAt` (defers). Resurrect+auto-fire structurally impossible on the crash path (persisted server-side, no frontend pop). JS classifiers KEPT as fallback for convs the recovery loop doesn't touch. **PARKED:** move reconcile onto the conversation GET path to retire the JS classifiers entirely. Tests: test_reconcile_conversation.py, test_startup_reconcile.py, test_frontend_reconcile_defer.py.

**Guardrail:** any settled-render change ships with a byte-revert NC test. When frontend inference is a regression source, port the VERDICT to a PURE backend function run where the backend already owns the write (terminal commit / startup recovery), persisted in the SAME transaction — a pure verdict function can't auto-fire, and same-commit persistence can't be lost by a dropped frontend PUT.

## Completed earlier
- Autopilot VU settled render (2026-07-02): verbatim from `ev.vuMessage`, no `|| buf` fallback.
- Message Queue Decision (2026-04-16): frontend always POSTs /chat/send; backend returns queued|taskId.
- Env test quirk: DB-backed tests (test_queue_redispatch, test_startup_reconcile) pass run DIRECTLY (`python tests/x.py`, warms DB) but fail under bare `pytest` with `no such table: conversations`. Not a logic failure.

