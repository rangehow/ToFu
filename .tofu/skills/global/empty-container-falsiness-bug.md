---
name: empty-container-falsiness-bug
description: Bug pattern: Python objects with __len__ returning 0 are falsy — 'if not obj' fails even when obj is a valid instance; always use 'is None' for None-checks on container-like objects
enabled: true
tags: [python, debugging, truthiness, container, __len__, None-check]
created: 2026-03-17T06:38:20Z
updated: 2026-03-17T06:38:20Z
---

# Empty Container Falsiness Bug

## Pattern
When a class defines `__len__`, instances with zero items are **falsy** in Python:
```python
class Store:
    def __len__(self): return len(self._items)

store = Store()
bool(store)      # False  ← because len == 0
not store        # True   ← DANGEROUS
store is None    # False  ← correct check
```

## The Bug
Code like this silently fails:
```python
if not self.artifact_store:
    return "Error: no artifact_store"  # WRONG — triggers on empty store!
```

## The Fix
Always use explicit `is None` checks:
```python
if self.artifact_store is None:
    return "Error: no artifact_store"
```

## Real-World Instance (chatui)
- `ArtifactStore` in `lib/swarm/protocol.py` has `__len__` returning number of stored artifacts
- `SubAgent._handle_read_artifact()` etc. in `lib/swarm/agent.py` used `if not self.artifact_store`
- Every agent hit the error path because the store was empty (0 artifacts) → `bool(store) == False`
- All 3 artifact tool handlers needed fixing: `store_artifact`, `read_artifact`, `list_artifacts`

