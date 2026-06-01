---
name: shared-infrastructure-migration
description: Three new shared modules: api_response.py (446 sites), json_store.py (3 sites + 1 binary helper), ttl_cache.py (5 caches migrated)
enabled: true
tags: [architecture, modularization, refactor, infrastructure]
created: 2026-05-22T12:36:17Z
updated: 2026-05-22T12:36:17Z
---

# Shared Infrastructure Modules (2026-05-22)

Three new lib/* modules built and fully migrated to delete duplication.

## 1. lib/api_response.py — 446 sites consolidated

Helpers: ``api_ok(data, **extras)``, ``api_created``, ``api_no_content``,
``api_error(error, status=, **extras)``, ``api_bad_request``,
``api_unauthorized``, ``api_forbidden``, ``api_not_found(what)``,
``api_conflict``, ``api_payload_too_large``, ``api_method_not_allowed``,
``api_internal_error(exc=, log_traceback=True)``, ``@safe_route``.

Migration approach: ``tests/_migrate_api_response.py`` regex script,
single-line patterns only, automatically inserts/extends imports.
- 96 ``{'error': str|literal}, status`` rewrites
- 53 ``{'ok': False, 'error': ...}, status`` rewrites
- 75 ``{'ok': True, key: val, ...}`` → ``api_ok({...})``
- 32 ``{'ok': True}`` → ``api_ok()``
- 19 ``{'error': f'...'}, status`` rewrites
- 8 502/503/504 rewrites via ``api_error(..., status=N)``

44 multi-line / extra-key responses left as-is (not boilerplate).

Tests: ``tests/test_api_response.py`` — 28 tests including
@safe_route, request_id propagation, exception classification.

## 2. lib/json_store.py — 3 atomic-write impls + 1 helper consolidated

Helpers:
- ``read_json(path, default=None, jsonc=False)``
- ``write_json_atomic(path, data, fsync=True, indent=2)``
- ``update_json_atomic(path, mutator, default=, jsonc=)`` — locked
  read-modify-write for safe concurrent updates
- ``read_text(path, default='')`` / ``write_text_atomic(path, text)``
- ``_strip_jsonc(text)`` — string-aware comment stripper

Migrated:
- ``lib/optimizer/actions/block_search_domain.py::_atomic_write``
- ``lib/file_history/store.py::_atomic_write_json`` (delegated; binary
  ``_atomic_write_bytes`` kept inline since json_store is JSON-only)
- ``lib/code_server_excludes.py::_atomic_write`` → ``write_text_atomic``

Tests: ``tests/test_json_store.py`` — 20 tests including atomicity-on-
serialise-failure, JSONC string-awareness, thread-safe update_json_atomic
under 8×25 concurrent increments, Unicode/emoji preservation.

## 3. lib/ttl_cache.py — 5 caches migrated

API: ``TTLCache(ttl, max_size=None, name='')`` with
``get/set/has/invalidate/clear/cleanup_stale/get_or_compute/stats``.

Features:
- LRU eviction on size overflow (touched on get())
- Per-key locking in ``get_or_compute`` (concurrent missers serialised)
- ``ttl=0`` disables expiry (size-bounded only)
- Lazy expired-entry eviction on access; eager via ``cleanup_stale()``
- ``__len__`` and ``__contains__`` supported

Migrated:
- ``lib/database/_sql_translate.py::_translate_sql_cache``
  (size-bounded only, ``ttl=0, max_size=1024``)
- ``lib/feishu/events.py::_processed_msgs`` (5min dedup, 10k max_size)
- ``lib/trading/market.py::_market_cache`` → 6 per-prefix instances
  (indices/sectors/breadth/northbound/trend/top_assets — each with its
  own TTL of 30/120/60/300/30/120 seconds)
- ``lib/trading/screening.py::_screen_cache`` (10min)
- ``lib/trading/nav.py::_nav_cache`` (L1, 30min) — multi-layer DB/holdings
  fallback unchanged; only L1 primitives now back onto TTLCache.
  ``_nav_from_memory`` synthesises a ``ts`` field for legacy callers.

Tests: ``tests/test_ttl_cache.py`` — 20 tests including LRU recency,
get_or_compute concurrency (1 fn() call from 2 threads), exception
propagation without caching, stats accuracy.

## Tests now in the suite (157 total, all passing)
- test_task_runtime: 28
- test_trading_simulator_migration: 12
- test_translate_migration: 10
- test_paper_migration: 14
- test_chat_manager_migration: 17
- test_api_response: 28 (NEW)
- test_json_store: 20 (NEW)
- test_ttl_cache: 20 (NEW)
- test_server_async: 8

## Caches NOT migrated (deliberately)
- ``lib/token_counter/usage_cache.py`` — has structured ``_UsageEntry``
  dataclass + signature-based staleness + model-family tokenizer check.
  Wrapping it in TTLCache would not save real code.
- ``lib/pdf_parser/vlm.py::_vlm_tasks`` — full task lifecycle
  (status/progress/result/error/find-by-criteria), correct migration
  target is ``lib.task_runtime.TaskRuntime``, not TTLCache. Future work.

## Key trap discovered
Migrating ``_market_cache`` required keeping per-prefix TTL semantics
(indices: 30s vs northbound: 300s). Solution: dict of `TTLCache`
instances keyed by prefix, with a default fallback. Single TTLCache
would have made all data 60s-stale.

