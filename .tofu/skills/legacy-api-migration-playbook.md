---
name: legacy-api-migration-playbook
description: Per-domain checklist for migrating legacy /api/* → /api/v1/* (no shims, single feature branch); blueprint-alias / endpoint= name disambiguation; post-commit URL re-grep
enabled: true
tags: [api, v1, migration, playbook]
created: 2026-05-29T02:36:45Z
updated: 2026-05-29T09:17:08Z
---

# Legacy `/api/*` → `/api/v1/*` migration — per-commit checklist

Reference: `docs/legacy_api_migration.md` (full inventory + carve-out list).

End-state assertion: `tests/test_legacy_api_removed.py` enforces zero
`/api/<x>` route registrations outside the `ALLOWED_NON_V1` allow-list
(32 carve-outs as of 2026-05-29).

## Per-commit recipe (one domain per commit)

1. **Read the legacy handler(s)** — note auth requirements, request/response shape.

2. **Create `routes/api_v1/<domain>.py`** with:
   - `from .auth import require_auth, require_scope`
   - `@api_meta(summary=..., description=..., tags=[...], scope=...)` for OpenAPI 3.1.
   - `from lib.api_response import api_ok, api_bad_request, ...`
   - `from lib.request_parser import parse_body, ...` (raises `BadRequest` → auto-400).
   - `logger = get_logger(__name__)`; log on errors with `exc_info=True`.

3. **Wire into `routes/api_v1/__init__.py`**: import + append to `ALL_V1_BLUEPRINTS`.

4. **Delete the legacy module + unwire** OR keep as carve-out shell.

5. **Flip frontend URLs in `static/js/api.js`** under the matching `Api.<domain>` block.

6. **Bump cache busters in `index.html`** for any JS file you changed.

7. **Tests**:
   - Add to `tests/test_api_v1_integration.py`: register the new bp,
     test 401 without token, success with token, legacy URL is 404.

8. **Update `docs/legacy_api_migration.md`** + per-module §3.x section.

9. **Run tests**: `python -m pytest tests/test_api_v1_integration.py tests/test_frontend_api_isolation.py tests/test_legacy_api_removed.py -q`

10. **Smoke-test blueprint registration** (Quart shim required because
    `routes/push.py` uses `@push_bp.websocket(...)`).

11. **POST-COMMIT VERIFICATION (added 2026-05-29 after a real bug):**
    Grep ONE MORE TIME for the legacy paths in the frontend:
    ```bash
    grep -rEn "'/api/<feature>" static/js --include='*.js' | grep -v "/api/v1/"
    ```
    Mechanical search-and-replace can miss URLs that don't exactly
    match the pattern (e.g. when commit 18 migrated `Api.pdf.vlmPoll`
    via the URL with a path param but missed `Api.pdf.vlmTasks`,
    `Api.images.generate`, `Api.images.models` — all silently 404'd
    in production until the audit caught it). The frontend isolation
    test catches NEW raw fetches outside `api.js` but NOT stale URL
    strings inside `api.js` itself.

## Blueprint-alias pattern (when legacy module has carve-outs)

For modules where SOME routes are JSON REST verbs (migrate) and OTHERS
are multipart / SSE / static-asset (stay legacy):

1. Create `routes/api_v1/<domain>.py` defining `api_v1_<domain>_bp`.
2. In `routes/<domain>.py`:
   - Keep the legacy `<domain>_bp` Blueprint creation.
   - Right after, add: `from routes.api_v1.<domain> import api_v1_<domain>_bp`.
   - For each route handler:
     - **v1**: rewrite `@<domain>_bp.route('/api/<x>')` →
       `@api_v1_<domain>_bp.route('/api/v1/<x>')`
     - **Carve-out**: leave on `<domain>_bp` with original path
3. Both blueprints register at boot.

Used for: paper, upload, artifacts, browser, oauth, desktop, common, chat.

### Endpoint-name collision (chat-domain quirk)

When `routes/api_v1/<domain>.py` defines functions with names the legacy
module also uses (e.g. `chat_abort` in BOTH files), Flask raises:

  ```
  AssertionError: View function mapping is overwriting an existing
  endpoint function: api_v1_chat.chat_abort
  ```

Fix: add `endpoint='ui_<func_name>'` to the legacy decorator so the two
share a URL but get unique endpoint names:

```python
@api_v1_chat_bp.route('/api/v1/chat/abort/<task_id>',
                       methods=['POST'], endpoint='ui_chat_abort')
def chat_abort(task_id): ...
```

## Blueprint-import-alias pattern (whole module migrates)

When EVERY route can move to v1 AND the legacy module's helper functions
are imported from elsewhere by name:

1. Create `routes/api_v1/<domain>.py` defining `api_v1_<domain>_bp`.
2. In legacy `routes/<domain>.py`, replace
   `<domain>_bp = Blueprint(...)` with
   `from routes.api_v1.<domain> import api_v1_<domain>_bp as <domain>_bp`.
3. Rewrite every route path to `/api/v1/<x>`.
4. Drop `<domain>_bp` from `ALL_BLUEPRINTS` in `routes/__init__.py`
   (it's now an alias of a bp already registered via `ALL_V1_BLUEPRINTS`).
5. Convert the `from .<domain> import <domain>_bp` import to a side-effect
   `from . import <domain>` so handler decorators still execute.

Used for: conversations, config, all 7 trading_* modules.

### Trading-module twist (conditional registration)

The 7 `trading_*` modules use blueprint-import-alias BUT register
conditionally on `lib.TRADING_ENABLED`. The aliased v1 blueprints live
in `routes/api_v1/trading/<short>.py` (sub-package) and are imported
inside the `if TRADING_ROUTES_REGISTERED:` block so they don't hit
`ALL_V1_BLUEPRINTS` (which is unconditional). They land in
`ALL_BLUEPRINTS` instead, alongside the legacy import side-effects.

## Public path policy

`/api/v1/*` routes default to **auth-required**. The hard-coded
`_PUBLIC_EXACT` allow-list in `routes/api_v1/auth.py` is the source
of truth — `@api_meta(public=True)` ONLY affects OpenAPI doc rendering,
not runtime auth.

## Tests: auth_mode marker (conftest 2026-05-29 onwards)

`tests/conftest.py` ships an `auth_mode` pytest marker:

```python
import pytest
pytestmark = pytest.mark.auth_mode('open')   # whole file
@pytest.mark.auth_mode('private')
def test_x(): ...
```

The autouse `_auth_mode_override` fixture swaps `TOFU_AUTH_MODE`,
clears `lib.auth_mode`'s cache, runs the test, restores both. Default
without a marker is `private` (the conftest default).

USE THIS for any new test module that:
- Instantiates `server.app` directly (not the lightweight `_AppFixture`).
- Doesn't supply a Bearer token on every call.

## End-state assertion: `tests/test_legacy_api_removed.py`

Two assertions:
- `test_no_legacy_api_routes_remain`: every `/api/<x>` rule must be
  `/api/v1/<x>` OR in `ALLOWED_NON_V1` (32 entries: liveness, OpenAPI
  viewers, telemetry, push WebSocket, OAuth browser-redirect, SSE
  streams, multipart uploads, static asset serving, artifact binary/HTML,
  Bridge-Secret RPC).
- `test_carve_out_list_is_exhaustive`: every `ALLOWED_NON_V1` entry
  must actually exist on the app (catches stale carve-out entries).

## Common gotchas

- Frontend bundler hashes content automatically; `?v=...` cache busters
  in `index.html` must be bumped manually for any JS file you changed.
- `BadRequest` from `lib.request_parser` is auto-converted to a 400 by
  `@safe_route` and `_normalize_error`.
- `request_parser.parse_body()` is sync-callable from sync handlers
  thanks to `_install_flask_shim` in `server.py`.
- Trading routes are conditional on `lib.TRADING_ENABLED`.
- For URL-builder methods on `Api.<domain>` (carve-out paths used as
  iframe.src / anchor.href), use `_resolve('/api/...')` to build the
  absolute URL.
- **Never trust mechanical search-and-replace** — after every commit,
  re-grep the frontend for legacy paths to catch URL strings that
  didn't match the pattern. Step 11 above.

