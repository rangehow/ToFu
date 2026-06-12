---
name: tofu-search-standalone-extraction
description: tofu-search v0.2.0 standalone lib + chatui INVERTED to consume it: lib/search+lib/fetch DELETED, redirected to tofu_search via lib/search_bridge.py provider/LLM seams
enabled: true
tags: [architecture, tofu-search, extraction, standalone]
created: 2026-04-06T08:20:23Z
updated: 2026-06-10T11:01:40Z
---

# tofu-search Standalone Library + chatui Dependency Inversion

## Location / publish
`/mnt/.../ruanjunhao04/tofu-search/` (sibling to chatui). GitHub `rangehow/tofu-search` (force-pushed v0.2.0, commit 137f05a). PyPI dist `tofu-search` 0.2.0 (LIVE), import `tofu_search`. PyPI token persisted at `/mnt/.../ruanjunhao04/.secrets/{pypirc,env.sh}` (UV_PUBLISH_TOKEN). GitHub PAT lives in chatui `data/config/mcp_servers.json` (ghp_...).

## v0.2.0 standalone lib (31 modules)
Full re-extraction of chatui lib/search+lib/fetch at 100% parity. Decoupling: `_lib.FETCH_*`→`config.get_config()`; `lib.log`→`log.py`; `dispatch_chat`→`llm_adapter.call_llm`; `lib.http_client`→`http_client.py`; `lib.pdf_parser`→self-contained `fetch/pdf_extract.py`. content_filter gates on `config.has_llm()`. NEW `providers.py` seam: `BrowserProvider`/`AuthSourceProvider` (+register/get) — default no-op; restores the 3 dropped chatui-only features (browser fetch/search fallback, xhs auth-search) without importing a host. Public API 18 exports incl. vertical (detect_vertical_intent/search_vertical/search_vertical_domain/list_domains) + provider register fns.

## chatui INVERTED to consume tofu-search (DONE)
lib/search + lib/fetch DELETED from chatui. All ~15 consumers redirected to `from tofu_search ...`. Key piece: **`lib/search_bridge.py`** installs chatui behavior into tofu-search seams at startup, preserving 100% parity:
- `_chatui_llm` → `llm_function` wrapping `dispatch_chat(capability='cheap', prefer_model=FETCH_FILTER_MODEL, extra={'stop':[§§IRRELEVANT§§]})`; on `ContentFilterError` (HTTP 450) RETURNS the placeholder string (filter treats str as success) instead of re-raising — preserves "don't re-feed 450 text to main model".
- `_ChatuiBrowserProvider` → wraps `lib.browser` (is_extension_connected / fetch_url_via_browser / DDG-HTML search-and-parse).
- `_ChatuiAuthSourceProvider` → wraps `lib.auth_sources.match_source/get_source`.
- `install_search_bridge()` called in server.py BEFORE `register_all(app)`. `sync_search_config()` re-syncs on `lib.reload_config()` (hot-reload) and on config.py filter-toggle save.

## Parity-critical details
- content-filter toggle: OLD code mutated `lib.fetch.content_filter.FILTER_ENABLED`. NOW `lib.__init__` exposes resolved `LLM_CONTENT_FILTER_ENABLED` (env FETCH_LLM_FILTER > saved `search.llm_content_filter` > default True); bridge `sync_search_config` reads it into tofu `config.filter_enabled`. routes/config.py read+write sites updated to use `_lib.LLM_CONTENT_FILTER_ENABLED`.
- `interactive_login.capture_login_cookies` in tofu RETURNS cookies (doesn't persist); routes/api_v1/auth_sources.py login route now persists them via `lib.auth_sources.upsert_source(dom, enabled=True, cookies=...)`.
- `artifacts/pdf_export.py` pushes a `pdf_render` task onto the playwright pool — that wiring WAS carried into tofu_search's playwright_pool.py, so just redirect the import.
- requirements.txt has `tofu-search>=0.2.0`. server.py dev fallback: `TOFU_SEARCH_PATH` env adds sibling checkout to sys.path (pip blocked in this env's venv).

## Consumers redirected (the full list)
routes/config.py, routes/api_v1/agents.py, routes/api_v1/auth_sources.py, lib/paper/tools.py, lib/tasks_pkg/executor.py, lib/tasks_pkg/streaming_tool_executor.py, lib/tasks_pkg/handlers/search.py, lib/tasks_pkg/handlers/browser.py, lib/browser/fetch.py, lib/browser/handlers.py, lib/artifacts/pdf_export.py. Plus test/doc updates: tests/test_smoke.py, lib/tests/validate_imports.py, server.py `_validate_imports` + `_CRITICAL_IMPORTS`, lib/protocols.py + lib/auth_sources.py docstrings.

## Verification
`PYTHONPATH=<tofu-search>:<chatui>`: validate_imports 24/24; pytest test_smoke+test_review_fixes+test_async_handler_integrity+test_server_async = 81 passed. Bridge 450-placeholder path unit-verified. Residual `lib.search`/`lib.fetch` grep = only `lib.search_bridge` + cosmetic docstrings.

## Gotchas
- `routes/push.py` `@push_bp.websocket` AttributeError under bare `python -c`/import is the Flask→Quart shim (installed only at server boot) — NOT a regression; don't import the `routes` package in smoke scripts.
- pip refuses without activated venv here; use PYTHONPATH. `uv build/publish` needs UV_CACHE_DIR + UV_PYTHON_INSTALL_DIR + UV_NO_MANAGED_PYTHON redirected off `$HOME` (perm denied).
- engines: bing/brave/ddg/searxng/marginalia always; xhs only when auth provider supplies a connected source.
