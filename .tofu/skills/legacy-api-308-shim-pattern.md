---
name: legacy-api-308-shim-pattern
description: Pattern for 308-redirect shims that quiet stale-tab polling against retired /api/* endpoints
enabled: true
tags: [routes, api-migration, legacy, redirect]
created: 2026-05-29T09:21:29Z
updated: 2026-05-29T09:21:29Z
---

# Stale-tab compat: 308 shim pattern for retired /api/* endpoints

## When to use
After migrating `/api/<feature>/*` → `/api/v1/<feature>/*`, long-lived
browser tabs running the pre-migration JS bundle keep polling the old
URL on a timer. The 404 errorhandler logs each one at WARNING in
`logs/error.log` (`server.py:_handle_404`), which can spam the log
forever for high-frequency pollers (optimizer panel polls every 60s).

## Pattern
Add a single catch-all route in `routes/legacy_redirects.py` that
issues a **308 Permanent Redirect** (preserves method + body, unlike
301/302) to the v1 path. Preserve the query string explicitly.

```python
@legacy_redirects_bp.route('/api/<feature>/<path:rest>',
    methods=['GET','POST','PUT','PATCH','DELETE'])
def _redirect_<feature>(rest):
    target = '/api/v1/<feature>/' + rest
    qs = request.query_string.decode('latin-1') if request.query_string else ''
    if qs:
        target = target + '?' + qs
    return redirect(target, code=308)
```

Log at DEBUG, not INFO — the whole point is silencing noise.

## Required follow-ups
1. Register `legacy_redirects_bp` in `routes/__init__.py:ALL_BLUEPRINTS`.
2. Register it in the `tests/test_api_v1_integration.py` `_AppFixture`
   too (the fixture lists every bp explicitly).
3. Add the rule string `/api/<feature>/<path:rest>` to
   `ALLOWED_NON_V1` in `tests/test_legacy_api_removed.py` with a
   comment pointing back to the shim file.
4. Update any `test_legacy_<feature>_is_404` test in
   `tests/test_api_v1_integration.py` to assert 308 + Location.

## Scope discipline
Only add a shim when 404 noise is actually observed (i.e. real stale
tabs in the wild). Adding shims for every migrated endpoint defeats
the migration. Default behavior remains: legacy URL = 404.

## Verified
2026-05-29: Applied for `/api/optimizer/*` after `optimizer.js` was
seen 404-polling once a minute. All 90 tests in
test_api_v1_integration + test_legacy_api_removed +
test_frontend_api_isolation pass.

