---
name: dispatch-permission-error-pair-exclusion
description: Bug fix: LLM dispatch must use (key,model) pair exclusion for 401/403 permission errors, not whole-model exclusion, so other keys with access to the same model can still be tried
enabled: true
tags: [python, debugging, llm-dispatch, api-key, permission]
created: 2026-03-16T14:50:14Z
updated: 2026-03-16T14:50:14Z
---

# Dispatch Permission Error: Pair Exclusion Fix

## Problem
When an API key lacks permission for a model (e.g. 401/403), the dispatcher was excluding the **entire model** from retry attempts. This blocked other keys that DO have access to the same model.

## Root Cause
In `dispatch_chat()` and `dispatch_stream()`, the error handler had only two categories:
- 429 → exclude key
- everything else → `exclude.add(slot.model)` ← BUG: kills model for all keys

## Fix: Three-tier exclusion
Add `exclude_pairs: set[tuple[str,str]]` to track `(key_name, model)` combinations:

```python
exclude = set()           # models to exclude entirely (hard model errors)
exclude_keys = set()      # keys to exclude entirely (429 rate limits)  
exclude_pairs = set()     # (key_name, model) pairs to exclude (permission errors)
```

Error classification:
```python
is_429 = '429' in err_str or 'rate' in err_lower
is_perm = ('401' in err_str or '403' in err_str
           or 'unauthorized' in err_lower
           or 'forbidden' in err_lower
           or 'permission' in err_lower
           or 'access denied' in err_lower)

if is_429:
    exclude_keys.add(slot.key_name)
elif is_perm:
    exclude_pairs.add((slot.key_name, slot.model))  # only this combo
else:
    exclude.add(slot.model)  # model itself is broken
```

The `_pick()` method also needs the `exclude_pairs` filter:
```python
if exclude_pairs and (slot.key_name, slot.model) in exclude_pairs:
    continue
```

## Files Changed
- `lib/llm_dispatch.py`: `pick_slot()`, `pick_and_reserve()`, `_pick()`, `dispatch_chat()`, `dispatch_stream()`

