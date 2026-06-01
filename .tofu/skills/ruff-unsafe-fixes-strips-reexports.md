---
name: ruff-unsafe-fixes-strips-reexports
description: Bug: ruff --unsafe-fixes strips re-export imports (F401) in _core.py — fix with noqa: F401
enabled: true
tags: [ruff, export, bug]
created: 2026-04-04T04:51:57Z
updated: 2026-04-04T04:51:57Z
---

# Ruff `--unsafe-fixes` Strips Re-Export Imports

## Problem
`export.py` runs `ruff check --fix --unsafe-fixes` on the exported code.
When a module (like `lib/database/_core.py`) imports specific names from
sibling modules purely for re-export (backward compat), ruff's F401 rule
considers them "unused" and removes them.

This broke `lib/database/__init__.py` which imports `DictRow`, `PgCursor`,
`translate_sql`, `_column_exists` etc. from `_core.py`.

## Fix
Add `F401` to the `noqa` comment on re-export import lines:
```python
# Before (broken by ruff):
from lib.database._wrappers import DictRow, PgCursor  # noqa: E402

# After (safe):
from lib.database._wrappers import DictRow, PgCursor  # noqa: E402, F401
```

## When to check
- Any time you create a module that re-exports names from sub-modules
- Any time you refactor a monolith .py into a package with `_core.py` + siblings
- The `from . import module` pattern (importing whole modules) is generally safe
  because the module binding is a side effect; but `from .module import Name`
  is vulnerable to F401 removal

## Note
Whole-module imports (`from . import submodule`) in `__init__.py` files are
NOT affected because assigning the module name is treated as a side-effect.

