---
name: strict-model-dispatch-prevents-silent-switching
description: Dispatch strict_model=True prevents silent model switching for user-facing requests: 429 retries stay within alias group (different keys), error exclusion uses pair-level not model-level, score=inf check blocks cooldown'd slots"
enabled: true
tags: [python, dispatch, strict-model, user-facing, 429, alias-group, pair-exclusion, architecture]
created: 2026-03-30T04:08:55Z
updated: 2026-03-30T04:08:55Z
---

# strict_model Dispatch — No Silent Model Switching

## Problem
When a user explicitly selects a model (e.g. "opus" preset), 429 retries or error fallback
could silently switch to a cheaper/different model. The user expects to get the model they chose.

## Solution: `strict_model` parameter
Added to `_pick()`, `pick_slot()`, `pick_and_reserve()`, `dispatch_chat()`, `dispatch_stream()`.

### strict_model=True (user-facing)
- `stream_llm_response()` passes `strict_model=True`
- Only picks within preferred model's **alias group** (same model, different keys/deployments)
- Returns `None` when all matching slots are in cooldown → retry loop waits 0.3s
- On non-429 errors, excludes only the **(key, model) pair**, not the whole model
- Prevents silent model switching

### strict_model=False (default, backend tasks)
- compaction, daily reports, swarm agents, analysis, feishu
- Cross-model fallback allowed
- On generic errors, excludes entire model

## Key Implementation Details

### cooldown_until vs is_available
`is_available` is admin disable flag. Cooldown lives in `score()` returning `inf`.
Must check `chosen.score() == float('inf')` under strict_model, not `is_available`:
```python
if strict_model and chosen.score() == float('inf'):
    return None
```

### Error Exclusion
```python
# strict_model=True: pair-level exclusion only
exclude_pairs.add((slot.key_name, slot.model))  # other keys still tried
# strict_model=False: model-level exclusion
exclude.add(slot.model)  # all keys of that model skipped
```

### _NON_CHAT_CAPS Guard
`_pick()` guard that skips image_gen/embedding-only slots must be bypassed
when `capability='image_gen'` is explicitly requested:
```python
if capability not in self._NON_CHAT_CAPS and not self._is_chat_compatible(slot):
    continue
```

## Files
- `lib/llm_dispatch/dispatcher.py` — core `_pick()` logic
- `lib/llm_dispatch/api.py` — `dispatch_chat/stream` passthrough
- `lib/tasks_pkg/manager.py` — `stream_llm_response(strict_model=True)`

