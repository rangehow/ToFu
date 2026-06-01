---
name: retry-budget-envelope
description: Per-task wall-clock retry deadline + per-phase counter + zero-byte force-rotate (PR3b / C1, 2026-05-20)
enabled: true
tags: [retry, reliability, dispatch, §10.1, stream_handler]
created: 2026-05-20T09:04:19Z
updated: 2026-05-20T09:04:19Z
---


# C1 retry-budget envelope (2026-05-20)

Outermost guard around the existing per-signature retry caps. Three additions; per-signature caps (`_PREMATURE_RETRY_MAX_ZERO_BYTE=16`, `_PREMATURE_RETRY_MAX_CLASSIC=2`, `_EMPTY_STOP_RETRY_MAX=2`, `MAX_STREAM_RETRIES=4`, `_MAX_429_CYCLES=0`) are unchanged.

## 1. Per-task wall-clock deadline

`task['retry_deadline_at']` is set in `manager.create_task()`:
```python
_budget_s = int(getenv_compat('TOFU_TASK_RETRY_BUDGET_S', 'CHATUI_TASK_RETRY_BUDGET_S') or '300')
task['retry_deadline_at'] = (time.time() + _budget_s) if _budget_s > 0 else 0
```

Consulted in three places:
- `analyse_stream_result` short-circuits the zero-byte / empty-stop retry to `break` with `last_finish_reason='retry_budget_exhausted'` and `loop_exit_reason='retry_budget_exhausted_<bucket>_round_<N>'`.
- `stream_llm_response` wraps `abort_check` so dispatcher's 429 cycle exits cleanly when deadline elapses (same code path as user abort). One-time WARN per task via `task['_retry_deadline_logged']`.
- `llm_fallback._llm_call_with_fallback` re-raises before starting a reactive-compact attempt if the deadline passed.

`TOFU_TASK_RETRY_BUDGET_S=0` disables the deadline (legacy behaviour).

## 2. Per-phase counter scope

`task['_premature_retry_count_phase']` replaces the per-round local counter. `analyse_stream_result` reads/writes the task field when present; legacy callers (paper reports / swarm) without the field fall back to the old local-variable behaviour.

Reset points (in `endpoint.py`):
- Initial Planner turn boundary
- Each Worker phase entry (per iteration)
- Each Critic phase entry (per iteration)
- Replan Planner turn boundary

NOT reset: across rounds within the same phase. So a phase that hits 16 zero-bytes is exhausted for that phase; replan or next iteration gets a fresh budget.

One-time `audit_log('config_change', param='premature_retry_scope', old='per_round', new='per_phase', approved_by='user')` via `_AUDIT_LOGGED` flag — fires the first time `task['_premature_retry_count_phase']` is consulted.

## 3. Force-rotate on zero-byte (mirrors gateway-5xx-treated-as-429)

After a zero-byte `'continue'` decision, `analyse_stream_result` writes `task['_force_rotate_pair'] = (key_name, model)` from `usage._dispatch`. `stream_llm_response` consumes (pops) the signal and passes `{('key', 'model')}` as `avoid_pairs` to `dispatch_stream`.

`dispatch_stream(..., avoid_pairs=...)` seeds `exclude_pairs |= avoid_pairs` so the very first `pick_and_reserve` already steers around the offending slot. Last-resort relax: when no slot is available AND `_initial_avoid <= exclude_pairs`, drop the avoid set so we retry the bad slot rather than failing the task.

Force-rotate is zero-byte-only — classic premature close (model produced thinking, was then cut) doesn't trigger rotation because the slot already produced output. Without `_dispatch` metadata (older path), force-rotate is silently skipped; existing 429-style cooldown still rotates naturally.

## Why these particular defaults

- 300s wall-clock: full zero-byte cap-16 with backoff is ~75s, so ~4 phases of cap-16 fit before deadline. Endpoint mode `MAX_REPLANS=3` (4 planner phases × ~75s = 300s) is the binding constraint.
- Force-rotate beats threshold-quarantine because the existing 5xx code already proves the rotation pattern works; quarantine adds bookkeeping for marginal benefit when the wall-clock deadline already closes runaway.

## Tests
- `tests/test_retry_budget_envelope.py` (10 tests): deadline short-circuit (zero-byte + empty-stop), deadline disabled fallback, per-phase counter override + persistence + cap exhaustion + legacy fallback, force-rotate signal write + classic-doesn't-rotate + missing-dispatch-skip.
- All existing `tests/test_zero_byte_*` and `test_stream_anomaly_retry_widening.py` (18 tests) still pass.

## Pre-existing test caveat
`tests/test_endpoint_messages.py::TestEndpointMultiIteration::test_multi_iteration_message_shapes` times out at 60s. **NOT caused by PR3b** — verified by reverting endpoint.py changes and running test in isolation; same TimeoutError. Root cause: missing SQLite `conversations` table when run standalone + mock recorder running out of canned responses. Pre-existing flakiness.

