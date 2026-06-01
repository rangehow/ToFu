---
name: http-and-parser-migration
description: Two new shared modules: http_client (sync+async with auto-proxy) and request_parser (typed body parsing); 124 sites migrated
enabled: true
tags: [architecture, modularization, refactor, http, request-parsing]
created: 2026-05-23T15:34:49Z
updated: 2026-05-23T15:34:49Z
---

# HTTP Client + Request Parser Migration (2026-05-22)

## 1. lib/http_client.py — sync + async HTTP with auto-proxy

Replaces ad-hoc ``requests.X(url, ..., proxies=_proxies_for(url), timeout=...)``
patterns. Auto-applies ``proxies_for(url)`` (which now also bypasses
``localhost``/``127.0.0.1``/``0.0.0.0`` so ``httpx`` doesn't forward
loopback traffic through the corporate proxy).

### Sync helpers (requests-backed)
- ``http_get(url, *, timeout=30, headers=None, params=None, **extra)``
- ``http_post(url, *, timeout=30, headers=None, json=None, data=None, files=None, **extra)``
- ``http_put / http_delete / http_head``
- ``http_request(method, url, **kw)``
- ``http_stream(method, url, **kw)`` — context manager for ``stream=True``

### Async helpers (httpx-backed)
- ``await async_http_get / async_http_post / async_http_request``
- ``async with async_http_stream(...) as resp: async for line in resp.aiter_lines(): ...``

### Out of scope (kept independent)
- ``lib/fetch/utils.py`` — specialised circuit breaker + multi-session SSL
  fallback pool. Different requirements.
- ``lib/llm/stream.py`` / ``lib/llm/astream.py`` — custom SSE retry +
  cache breakpoints + 429 cycling.

### Migrated 23 call sites across 11 files
- lib/embeddings (3 post)
- lib/image_gen (2 get, 3 post)
- lib/llm/chat (1 post)
- lib/llm_dispatch/discovery (3 get)
- lib/llm_dispatch/health_local (1 get)
- lib/mt_provider (2 post)
- lib/oauth/claude (2 post)
- lib/oauth/codex (2 post)
- lib/pricing (2 get)
- lib/token_counter/anthropic_api (1 post)
- lib/token_counter/gemini_api (1 post)

### Key fix during migration
``lib/proxy.py::proxies_for()`` did NOT bypass localhost — only
``_bypass_domains`` (env PROXY_BYPASS_DOMAINS) and ``_registered_hosts``.
``requests`` honoured the no_proxy env var so this was invisible, but
``httpx`` does not — async calls to ``127.0.0.1`` would route through
the corp proxy and 403. Added ``_ALWAYS_BYPASS`` (localhost / 127.0.0.1
/ 0.0.0.0) check at the top of ``proxies_for``.

## 2. lib/request_parser.py — typed JSON body extraction

Replaces 91+ ``data = request.get_json(silent=True) or {}`` + manual
field extraction patterns.

### API
- ``parse_body(force=False)`` — always returns a dict; raises BadRequest on
  top-level non-dict (list/string).
- ``require_str(body, field, *, strip=True, max_len=None, allow_empty=False)``
- ``optional_str(body, field, *, default='', strip=True, max_len=None)``
- ``require_int(body, field, *, min=None, max=None)``  / ``optional_int``
- ``require_bool / optional_bool`` — coerces ``true``/``yes``/``on``/``1``/etc.
- ``require_list / optional_list`` with ``item_type=`` validation
- ``require_dict / optional_dict``
- ``BadRequest(ValueError)`` — carries ``.field`` attribute

### Wired through @safe_route
``lib/api_response.py::safe_route`` catches ``BadRequest`` specially and
returns ``api_bad_request(str(e), field=e.field)`` — auto-converts to
400 with field name in the response body.

### ``api_error(BadRequest)`` → string error (not envelope)
Custom-cased in ``_normalize_error`` so frontend ``data.error`` reads as
a human-readable string. All other Exception types → typed envelope.

### Migrated 101 call sites across 30 files (all in routes/)
Mechanical regex rewrite: ``data = request.get_json(silent=True) or {}``
→ ``data = parse_body()``. ``force=True`` variant also handled.
Migration script: ``tests/_migrate_request_parser.py``.

## Key trap (parse_body in tests)
Quart's ``request.get_json()`` is async. The Flask shim in ``server.py``
patches it sync-safe, BUT the shim's ``_run_coro_sync`` uses
``asyncio.run()`` which fails inside a running event loop. Solution:
tests use ``app.test_client()`` (production code path), NOT bare
``app.test_request_context()``. The shim's sync wrapper relies on
threadpool execution — async test contexts must use the client.

## Tests (69 new, 206 total all passing)
- ``tests/test_request_parser.py`` — 34 tests (every accessor + integration)
- ``tests/test_http_client.py`` — 15 tests (in-process mock server)
- Migration scripts: ``tests/_migrate_request_parser.py``,
  ``tests/_migrate_http_client.py``

## Migration-script lesson
``tests/_migrate_http_client.py`` originally used ``os.walk`` which hit
a symlink loop in the corporate FUSE mount (caused 30s timeout).
Switched to ``glob('lib/**/*.py', recursive=True)`` — same coverage,
no walk-into-bad-symlink risk. Also added docstring detection so we
don't rewrite ``requests.post(...)`` examples in module headers.

