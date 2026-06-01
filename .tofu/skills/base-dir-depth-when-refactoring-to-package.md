---
name: base-dir-depth-when-refactoring-to-package
description: Bug: when refactoring a module (lib/database.py) into a package (lib/database/_core.py), BASE_DIR = dirname(dirname(__file__)) goes up wrong number of levels, causing pgdata/logs to be created in wrong directory
enabled: true
tags: [python, bug-pattern, refactoring, database]
created: 2026-04-01T18:29:11Z
updated: 2026-04-01T18:29:11Z
---

# BASE_DIR Depth Bug When Refactoring Module → Package

## Pattern
When a Python module like `lib/database.py` is refactored into a package `lib/database/__init__.py` + `lib/database/_core.py`, any `os.path.dirname()` chain that computes a project root path becomes wrong because there's now one extra directory level.

## Example
```python
# lib/database.py — 2 levels up reaches project root ✓
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# lib/database/_core.py — 2 levels up only reaches lib/ ✗
# Need 3 levels:
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

## Symptoms
- New data directories created in wrong location (e.g. `lib/data/pgdata` instead of `data/pgdata`)
- Database "lost" — app bootstraps a fresh empty database
- Log files appearing in unexpected locations (`lib/logs/` instead of `logs/`)

## Prevention
- After any module→package refactor, search for `__file__` and `BASE_DIR` in the moved code
- Consider using a more robust approach: `BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__).rstrip(os.sep).rsplit('lib', 1)[0]))` or importing BASE_DIR from a top-level config

