---
name: test-server-shim-load-order
description: Test gotcha: server.py shim load order — tests that import lib.tasks_pkg before server.py boot will pollute Werkzeug context for later server-backed tests
enabled: true
tags: [testing, flask-quart, shim, trap]
created: 2026-05-26T16:12:45Z
updated: 2026-05-26T16:12:45Z
---

# Test gotcha: Flask→Quart shim load order

## Symptom
A unit test file that imports anything from `lib.tasks_pkg.*` (or any
package whose `__init__.py` chain pulls in `lib.database._core`) will:

1. Load real `flask` first (because the shim hasn't run yet).
2. Cache references to real-Flask's `g` LocalProxy in
   `lib/database/_core.py::close_db` (registered as
   `app.teardown_appcontext`).
3. When a *later* test boots `server.py` and uses `app.test_client()`,
   the Quart context's `g` proxy doesn't satisfy the cached Flask
   reference → teardown raises:

   ```
   RuntimeError: Working outside of application context.
   ```

The test file that imported `lib.tasks_pkg` passes, but EVERY
subsequent server-backed test in the same pytest invocation fails.

## Fix
Force-load `server.py` (which runs `_install_flask_shim()` at module
top) BEFORE importing any `lib.*` symbol that might transitively pull
flask:

```python
# tests/test_*.py  — at the TOP of the file
import importlib.util as _iu
_spec = _iu.spec_from_file_location('server_for_shim_<unique>', 'server.py')
_mod = _iu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _iu

# Now safe to import lib.*
from lib.tasks_pkg.tool_hooks import ...
```

## Already-broken tests with this issue (pre-existing)
- `tests/test_package_facades.py::TestFlaskRouteRegistration::test_all_critical_routes_registered`
  — `Blueprint object has no attribute 'websocket'` (real Flask doesn't
  have it; Quart's Blueprint shim does).

## Reference
Fixed in `tests/test_hook_taxonomy.py` (2026-05-26) — see the leading
`importlib.util` shim block.

