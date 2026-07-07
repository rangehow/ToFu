---
name: test-server-shim-load-order
description: Flask→Quart shim load order: the `g`-teardown facet is now ROOT-CAUSE FIXED (lib/database/_core.py lazy _get_g); the Blueprint.websocket collection facet still needs import-server-first in the test file
enabled: true
tags: [testing, flask-quart, shim, trap]
created: 2026-05-26T16:12:45Z
updated: 2026-06-16T12:01:43Z
---

# Test gotcha: Flask→Quart shim load order

## Symptom
A test file that imports anything pulling in `lib.database._core` BEFORE
`server.py` runs its shim causes one of two failures:
1. `RuntimeError: Working outside of application context` from the DB
   teardown, in a LATER server-backed test. **← ROOT-CAUSE FIXED (2026-06).**
2. `AttributeError: 'Blueprint' object has no attribute 'websocket'` at
   import/collection (real Flask's Blueprint lacks `.websocket`; the Quart
   shim adds it). **← still needs the per-file fix below.**

## Facet 1 (g teardown) — FIXED at the source
`lib/database/_core.py` no longer does `from flask import g` at module top;
it resolves `g` lazily via `_get_g()` (imports `flask` at call time, so it
always sees the shimmed quart). See memory `db-core-lazy-g-shim-fix`. You no
longer need the importlib pre-load shim block JUST to avoid the g-teardown
RuntimeError.

## Facet 2 (Blueprint.websocket at import) — still real
If a test imports `routes.*` (→ routes/__init__ → routes/push.py
`@push_bp.websocket('/api/push')`) before the shim, collection still crashes.
Fixes:
- In a fixture: do `import server` (installs shim) BEFORE `import routes.*`.
  This is what `tests/test_orchestrations.py::_AppFixture` now does (2026-06):
  the `import server` line moved ABOVE `import routes.api_v1.orchestrations`.
- At a test-file top, force-load server first:
  ```python
  import importlib.util as _iu
  _spec = _iu.spec_from_file_location('server_for_shim_<unique>', 'server.py')
  _mod = _iu.module_from_spec(_spec); _spec.loader.exec_module(_mod)
  del _spec, _mod, _iu
  # now safe to import lib.* / routes.*
  ```

## Known still-broken (pre-existing, separate)
- `tests/test_conversation_search.py` — Blueprint.websocket collection error
  AND references a `flask_client` fixture defined NOWHERE in tests/ (so its
  API tests cannot run as written). Two independent breakages.
- `tests/test_db_thread_conn_lifecycle.py` — 4 collection ERRORs, pre-existing.
- `tests/test_package_facades.py::...test_all_critical_routes_registered`
  — Blueprint.websocket.

## Bisection discipline
This tree has 200+ uncommitted files: `git stash` reverts everything (false
baseline). Isolate a file with `cp` backup + `git checkout -- <file>`, test,
restore backup.
