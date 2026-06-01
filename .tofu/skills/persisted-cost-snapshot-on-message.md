---
name: persisted-cost-snapshot-on-message
description: Persist computed cost on assistant message + apiRounds at sync time so renders are zero-network
enabled: true
tags: [frontend, performance, cost, persistence, convention]
created: 2026-05-30T03:17:33Z
updated: 2026-05-30T03:17:33Z
---

# Persisted cost snapshot on assistant messages

## What
Cost is **stamped onto the assistant message and onto each apiRounds
entry** at sync time, not lazily fetched per render.

* `m.cost` — `compute_cost(m.usage, m.model, m.provider_id)` dict.
* `m.apiRounds[*].cost` — same shape, per-round.

Computed in:
* `lib/tasks_pkg/orchestrator.py::_finalize_and_emit_done` — stamps on
  the SSE `done` event so live clients get it without reload.
* `lib/tasks_pkg/manager.py::_sync_result_to_conversation` — stamps
  onto the persisted message so reload paths get it from the DB.

Frontend readers (in priority order):
* `static/js/ui/finish_info.js::renderFinishInfo` →
  `msg.cost || calcCostCny(...)` and same for `rd.cost`.
* `static/js/core/cost.js::calcConversationCost` →
  `m.cost || calcCostCny(...)`.

## Why
Before this, `calcCostCny` always went through
`POST /api/v1/messages/cost` with a client-side cache. On large
conversations every `renderChat()` triggered N parallel cost
requests (one per assistant message + one per apiRounds entry) — see
the ``cost-prefetch-must-include-apiRounds`` memory.

The data dependencies (usage + model + provider + pricing table) are
**final at the moment the LLM call returns**. There is nothing to
recompute later. Persisting the result is strictly less work than
recomputing on demand.

## Pricing-change semantics
Cost is "as of message time" (option B in the design). If the pricing
table changes next week, **historical costs do not retroactively
update** — they reflect what was actually charged at request time.
This matches what billing/auditing wants and means **no invalidation
is ever needed**.

If you ever DO want a re-price feature (probably never), build a
separate "preview" route that runs `compute_cost` on stored usage
without touching `m.cost`.

## Backward compat
Legacy messages (persisted before this change) have no `m.cost`.
The renderers fall back to the existing `calcCostCny` lazy fetch path
+ `_prefetchConvCosts` batch — which still works. New messages skip
all of that.

## Net result
Zero per-render `/cost` requests for any message persisted after this
change. The endpoint stays as a fallback for legacy + SDK callers.

