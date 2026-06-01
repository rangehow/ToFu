---
name: sync-dedup-drops-user-followup-after-endpoint-mode
description: Bug fix: _sync_result_to_conversation dedup destroys user's short follow-up question after endpoint mode ends — misinterprets critic+user as consecutive same-role anomaly
enabled: true
tags: [python, bug-fix, endpoint, sync, dedup, data-loss, race-condition, manager]
created: 2026-04-22T07:48:30Z
updated: 2026-04-22T07:48:30Z
---

# Bug: Dedup in _sync_result_to_conversation drops user follow-up after endpoint mode

## Symptom
Conversation `mo8ljy63cr5mex`: after endpoint mode completed (4 planner+worker+critic iterations), user turned endpoint mode OFF and sent a short question `"What is the current status of v29? Is it working now?"` (53 chars). The assistant's answer was generated, but on refresh the user's own question had disappeared from the conversation — answer visible, prompt gone.

## Root Cause
`_sync_result_to_conversation` in `lib/tasks_pkg/manager.py` has a "MESSAGE COUNT ANOMALY" guard that:
- Compares `_initial_msg_count` (snapshot sent to create_task, AFTER `_collapse_historical_endpoint_sessions`) to the current DB msg count.
- If delta > 2 AND any consecutive-same-role pair exists in the extras → "auto-deduplicate" by dropping the shorter of each adjacent same-role pair.

After endpoint mode, the DB legitimately contains:
- Consecutive `role=assistant` pairs: planner + worker (both assistants)
- Consecutive `role=user` pairs: critic review (`role=user`, `_isEndpointReview:true`) + new real user message

The dedup misinterpreted these as corruption and dropped workers in favor of planners AND dropped the short new user question (53 chars) in favor of the preceding 5385-char critic. Log evidence:
```
⛔ MESSAGE COUNT ANOMALY with consecutive same-role: DB has 15 messages but task started with 3 — 12 extra.
Removed duplicate user message (kept 5385 chars, dropped 53 chars)   ← the user's real question lost!
```

## Fix (lib/tasks_pkg/manager.py, _sync_result_to_conversation)
Two-part defense:

**1. Skip dedup entirely when endpoint history is present.** Detect any message with `_isEndpointPlanner`, `_isEndpointReview`, `_epIteration`, `_epIter`, `_epPlannerIteration`, or `_epNextPhase`. Endpoint mode's planner+worker (both assistants) and critic+next-user (both users) are by-design same-role pairs, not corruption.

**2. Tail protection in the dedup loop.** Even for non-endpoint dedup, never drop the last 2 messages (`_tail_protect_idx = len(messages) - 2`). A short new user follow-up must always win over any earlier same-role message it sits next to.

```python
has_endpoint_history = any(
    (m.get('_isEndpointPlanner') or m.get('_isEndpointReview')
     or m.get('_epIteration') is not None or m.get('_epIter') is not None
     or m.get('_epPlannerIteration') is not None or m.get('_epNextPhase'))
    for m in messages
)
if has_consecutive_same_role and has_endpoint_history:
    # Log info and skip dedup — planner+worker and critic+user are expected.
elif has_consecutive_same_role:
    # Run dedup but with tail protection:
    _tail_protect_idx = max(0, len(messages) - 2)
    for idx, m in enumerate(messages[1:], start=1):
        if (m.get('role') == deduped[-1].get('role')
                and idx < _tail_protect_idx):
            # ... drop shorter
```

## Why the backend message builder collapses history but DB keeps it
`conv_message_builder._collapse_historical_endpoint_sessions()` collapses endpoint iterations for the LLM context (keeps only the last worker) — that's why `_initial_msg_count` is small (e.g. 3). But the PERSISTED conversation keeps all planner/worker/critic records for display. The two must not interfere; dedup at sync-time conflated them.

## Files Changed
- `lib/tasks_pkg/manager.py` — endpoint-history bypass + tail protection in dedup block (~line 383-460)

