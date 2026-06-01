---
name: headless-api-v1-architecture
description: Headless API v1: tri-state auth_mode (open/private/multi-user), unified gate, OpenAPI 3.1, SDKs E2E verified
enabled: true
tags: [api, auth, openapi, compat, architecture]
created: 2026-05-25T07:46:13Z
updated: 2026-05-27T03:12:31Z
---

# Headless API v1 — Architecture (post-unification)

The headless API is mounted at four prefixes:

* `/api/v1/*`       — Tofu native (full feature parity with the UI)
* `/v1/...`          — OpenAI compat (chat/completions, models, embeddings)
* `/v1/messages`     — Anthropic compat (Messages API)
* `/metrics`         — Prometheus exposition (admin-scoped)

Plus self-description:
* `/api/openapi.json` / `/api/openapi.yaml` — OpenAPI 3.1 spec
* `/api/docs` (Swagger UI) / `/api/redoc`

## Auth modes (2026-05-27 refactor)

`lib/auth_mode.py` is the single source of truth for whether the gate
requires a credential. Three documented modes, persisted at
`data/config/auth.json`:

| Mode | Gate | Use case |
|---|---|---|
| `open` (DEFAULT) | Pass-through; synthetic local-admin AuthContext | Personal install, frontend-only |
| `private` | Bearer/cookie required; HTML hint page on `/` | Single multi-device operator |
| `multi-user` | Same gate as private | Relay station for many users |

Override priority: `TOFU_AUTH_MODE` env var > file > `open` default.
The env var LOCKS the mode — UI cannot override it.

`AuthContext` got a new `via_open_mode: bool` flag. When True, every
`require_scope(...)` call passes (synthetic admin), but rate limits +
idempotency keying treat it as "no real principal" (bypass like the
tunnel context).

## Auth — single unified gate

ONE `before_request` hook in `routes/api_v1/auth.py:auth_before_request`
resolves `g.auth_ctx` for every request. Token transports (priority):

1. `Authorization: Bearer <token>`
2. `x-api-key: <token>` (Anthropic SDK)
3. `tofu_session` HttpOnly cookie (set on first browser visit)
4. `?token=<token>` query string (first-link convenience; redirects + cookies)
5. `X-Tunnel-Token` / `TUNNEL_TOKEN` (DEPRECATED — back-compat shim only)

In `open` mode the middleware short-circuits BEFORE any of the
private-mode rejection paths: tokens are still honoured if presented
(so the same Bearer header keeps working when an operator switches to
`private` later) but missing/invalid ones do NOT 401.

### Public allow-list (everything else 401s when mode != open)

`_PUBLIC_EXACT` / `_PUBLIC_PREFIXES` in `routes/api_v1/auth.py`:
- `/static/*`, `/favicon.*`, `/.well-known/*`, `/robots.txt`
- `/api/health`, `/api/openapi.json|yaml`, `/api/docs`, `/api/redoc`
- `/api/v1/capabilities`, `/api/v1/keys/whoami`, `/api/v1/auth/mode`

### Mode admin endpoint

`GET  /api/v1/auth/mode` — public read; UI uses to render the mode card.
`PUT  /api/v1/auth/mode` — `@require_scope('admin')`; in `open` mode
the synthetic local-admin satisfies that, in `private/multi-user` you
need a real admin key. 409 + `error_kind=env_locked` if `TOFU_AUTH_MODE`
is set.

### First-boot bootstrap (private/multi-user only)

`lib/api_keys.bootstrap_personal_key()` runs at server startup. ONLY
when mode != `open`, the api_keys store is empty, AND `TUNNEL_TOKEN`
is unset. Mints one `tofu_admin_<hex>` key, prints plaintext + a
one-shot `?token=` URL to stderr, writes plaintext to
`data/config/.first_run_token` (chmod 0600).
Disable with `TOFU_AUTO_KEY=0`. In `open` mode this is a no-op.

### Default bind: 127.0.0.1

`server.py --host` defaults to `127.0.0.1` (was `0.0.0.0`). Networked
exposure requires explicit `--host 0.0.0.0` or `BIND_HOST=0.0.0.0`.
Combined with the open-by-default mode, "personal use just works
locally" without exposing the API to the LAN. If host != loopback AND
mode == open, the boot banner prints a loud warning recommending
private mode.

## ⚠️ SECURITY INVARIANTS

1. In `private` / `multi-user` modes, every `/api/*`, `/v1/*`,
   `/metrics` not in the public allow-list returns 401 without a
   credential. NO env var changes this within those modes.
2. `open` mode is a deliberate, documented, persisted policy choice
   stored in `data/config/auth.json`. The default is `open`.
3. Bootstrap NEVER mints a key when `TUNNEL_TOKEN` is set or when the
   mode is `open` (operator's intent is clear).
4. `?token=` is consumed + stripped before any route sees it
   (private mode only).
5. Rate limit + idempotency cache are bypassed for the synthetic
   open-mode context (no real principal to bill); private-mode keys
   still get the per-key bucket.

Tests:
- `tests/test_auth_mode.py` (8) — unit + open-mode E2E
- `tests/test_e2e_headless_api.py` (36) — private-mode E2E
- `tests/test_api_keys.py` (12)
- `tests/test_rate_limit_api.py` (9)

`tests/conftest.py` pins `TOFU_AUTH_MODE=private` so suites that
expect 401 keep working under the new open-by-default. Individual
tests that need open mode (e.g. `test_chat_streams_via_http_endpoints`)
call `lib.auth_mode.set_mode('open', ...)` and restore on teardown.

## OpenAPI spec cache

`routes/api_docs.py:_cached_specs` is keyed by `id(app)`, NOT a single
module-global — different test fixtures register different blueprint
subsets and a single cache would leak the wrong spec across tests.

## Idempotency

`@idempotent_post()` from `lib.idempotency`. Caches successful
responses (2xx) for 24h keyed by `(auth_principal, Idempotency-Key)`.
Async-aware — supports both `def` and `async def` handlers.
The cache key is salted with `g.auth_ctx.key_id`, so two principals
sending the same `Idempotency-Key` get isolated tasks. Open-mode
contexts use the salt prefix `open:` so they share one bucket per
instance (matches "one local user").

## Rate limits

`lib/rate_limit_api.py` token-bucket — RPM and TPD per key. Standard
headers (`X-RateLimit-*`, `Retry-After`). Buckets reconfigure
automatically when admin updates a key. Cookie-auth, tunnel-auth,
AND open-mode contexts bypass.

Public paths run pre-flight too so the standard headers appear, but
never enforce 429 — public means public.

## Usage tracking + observability

* `lib/usage_tracker.py` — per-key daily counters (requests, tokens,
  by_model). Backing store: `data/config/usage.json`. Flush every 30s
  (in-memory hot path, atomic write). Retention 90 days. Thread-safe.
* `record_usage(key_id, request_count=0)` for token-only updates from
  routes (request was already counted by middleware).
* `GET /api/v1/usage` — own analytics (or admin → any key via
  `?key_id=`). `GET /api/v1/usage/summary` — admin-only aggregate.
* `GET /metrics` — Prometheus text format (admin-scoped). No
  `prometheus_client` dependency; emit by hand.

## Settings UI for API keys + auth mode

`static/js/api-keys.js` — pure fetch+render layer. Hooks into
`switchSettingsTab` via monkey-patch. Must be in `_BUNDLE_FILES`
AFTER `settings.js`.

Tab UI in `index.html` between `mcp` and `advanced`. Provides:
- Auth-mode card (radio: open/private/multi-user) at top of tab.
  Switching to `open` requires a confirm dialog. Banner appears
  when `source=env` to explain why the radios are disabled.
- Create key (name, scopes, rpm, tpd, admin checkbox)
- One-shot plaintext token reveal
- List + revoke + toggle (disable/enable)
- Per-key usage chart (last 30 days bar graph)

## Compat translators

Pure functions in `lib/compat/openai.py` and `lib/compat/anthropic.py`.
Routes in `routes/compat_openai.py` / `routes/compat_anthropic.py`.

When the caller supplies explicit `tools=`, the auto-injected Tofu
tools (search/fetch/memory/mcp) are disabled in `cfg`.

OpenAI streaming uses `chat.completion.chunk` SSE frames with a `tofu`
envelope on non-text events. Anthropic streaming uses named events
(`message_start`, `content_block_delta`, etc.).

## Caller-supplied tools precedence

`lib/tasks_pkg/model_config.py:_assemble_tool_list` checks for
`cfg['tools']` FIRST. If a non-empty list is present, it's returned
verbatim and auto-derivation skipped.

## Client SDKs

* **Python** (`clients/python/`) — sync `Tofu` class + `tofu` CLI.
  Auth: CLI flags → `TOFU_API_KEY`/`TOFU_BASE_URL` env →
  `~/.tofu/config.toml`. **E2E verified against real Hypercorn.**
* **TypeScript** (`clients/typescript/`) — works in Node 18+,
  browsers, Cloudflare Workers, Vercel Edge, Deno, Bun. No external
  runtime deps. Mirrors the Python SDK 1:1.

## Gotchas

* **Don't use `quart.make_response(...)` from sync server middleware**
  — server.py wraps it in a sync-safe shim that uses
  `run_coroutine_threadsafe(...)`. Calling it from inside an `async def`
  before_request hook deadlocks (waits for itself). Use `Response(...)`
  or `redirect(...)` directly.
* `usage_tracker` setUp must `os.remove(_STORE_PATH)`.
* `lib.idempotency._cache.clear()` in setUp.
* OpenAPI spec is cached per-app-id, not module-global.
* Tempdir-isolate `lib.api_keys._STORE_PATH`,
  `lib.auth_mode._STORE_PATH`, and `lib.usage_tracker._STORE_PATH`
  in tests.
* `lib.auth_mode.reset_for_tests()` clears both the in-process cache
  AND the on-disk file. Call after env-var manipulation in fixtures.

## Frontend/Backend boundary (CLAUDE.md §16)

Every UI feature MUST land on `/api/v1/*` first; the UI becomes a
client of that endpoint. Business logic in `static/js/*` is a leak —
see `docs/JS_LEAK_AUDIT.md`.

`static/js/api-keys.js` is the model for compliant code: pure fetch
+ render, zero local state, server is the source of truth for
everything (mode vocab, scope vocab, rate limits, validation).

