---
name: api-response-migration
description: api_response.py unified helper migration: 446 ad-hoc jsonify calls converted; only 44 multi-line/extra-key cases remain
enabled: true
tags: [architecture, api-response, modularization, refactor]
created: 2026-05-22T12:23:10Z
updated: 2026-05-22T12:23:10Z
---

# Unified API Response Helper (2026-05-22)

## What it is
`lib/api_response.py` — single source of truth for HTTP response shape.

## Helpers
- `api_ok(data=None, **extras)` — 200, `{ok: True, ...}`
- `api_created(data=None, **extras)` — 201
- `api_no_content()` — 204
- `api_error(error, status=400, **extras)` — generic, status-pluggable
- `api_bad_request(error, **extras)` — 400
- `api_unauthorized(error='Unauthorized', **extras)` — 401
- `api_forbidden(error='Forbidden', **extras)` — 403
- `api_not_found(what='not_found', **extras)` — 404
- `api_conflict(error, **extras)` — 409
- `api_payload_too_large(max_bytes, **extras)` — 413 with friendly MB hint
- `api_method_not_allowed(error, **extras)` — 405
- `api_internal_error(exc, context=, source=, **extras)` — 500 with auto-traceback log
- `@safe_route` — decorator that catches uncaught exceptions

## Response shape contract
ALWAYS includes `ok` (True or False). Errors ALWAYS set `error` (string OR
envelope dict — NOT auto-wrapped to envelope, to preserve legacy frontend
parsing). Extra fields can be passed via `**extras`.

## What was migrated
446 mechanical rewrites across 31 files via `tests/_migrate_api_response.py`:
  - 96 single-line `{'error': str/literal}, status` → typed helpers
  - 53 `{'ok': False, 'error': ...}, status` → typed helpers
  - 19 `{'error': f-string}, status` → typed helpers
  - 32 `{'ok': True}` → `api_ok()`
  - 75 `{'ok': True, key: val, ...}` → `api_ok({key: val, ...})`
  - 8 502/503/504 cases → `api_error(..., status=N)`

## What remains (44 sites)
Multi-line dict literals or patterns with extra computed keys
(`{'error': 'x', 'detail': str(e), 'msgCount': N}`). These are not
"boilerplate" — they're producing distinct response shapes and do not
benefit from migration. Left as-is.

## Migration script
`tests/_migrate_api_response.py` — dry-run by default, --apply to write,
--file BASENAME to restrict. Also detects which helpers each file needs
and inserts/extends `from lib.api_response import …` automatically.

## Tests
`tests/test_api_response.py` — 28 unit tests covering every helper,
shape contract, error normalization, request_id propagation, decorator
behavior. Resolves Quart's async `response.get_data()` correctly.

## Key trap
Quart's `response.get_data()` is async. Test helper must `await` it.
The migration script regex is single-line only and conservative — it
SKIPS any pattern it doesn't fully understand (multi-line, complex
dict, unknown status). Never re-runs a partial pattern.

