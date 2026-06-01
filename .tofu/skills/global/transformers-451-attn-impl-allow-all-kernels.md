---
name: transformers-451-attn-impl-allow-all-kernels
description: Bug fix: transformers>=4.51 added allow_all_kernels kwarg to _check_and_adjust_attn_implementation; custom overrides must accept **kwargs or will crash at model init
enabled: true
tags: [python, transformers, huggingface, bug-pattern, attention]
created: 2026-03-26T07:47:53Z
updated: 2026-03-26T07:47:53Z
---

# transformers ≥4.51: `_check_and_adjust_attn_implementation` new `allow_all_kernels` kwarg

## Problem
In `transformers>=4.51`, `PreTrainedModel.__init__` calls:
```python
self._check_and_adjust_attn_implementation(
    ..., allow_all_kernels=hub_kernels.ALLOW_ALL_KERNELS
)
```

If a model (e.g. ModernBERT) overrides `_check_and_adjust_attn_implementation` with only the old signature `(self, attn_implementation, is_init_check=False)`, it crashes:
```
TypeError: _check_and_adjust_attn_implementation() got an unexpected keyword argument 'allow_all_kernels'
```

## Fix
Add `**kwargs` to the override signature and forward to `super()`:
```python
def _check_and_adjust_attn_implementation(
    self, attn_implementation: Optional[str], is_init_check: bool = False, **kwargs
) -> str:
    # ... custom logic ...
    return super()._check_and_adjust_attn_implementation(
        attn_implementation=attn_implementation, is_init_check=is_init_check, **kwargs
    )
```

This is forward-compatible with any future parameter additions.

## Also in same version
`_tied_weights_keys` changed from list to dict format `{target: source}`.

