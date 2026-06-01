---
name: web-search-fetch-url-batch-prefetch-display-bug
description: Batch web_search/fetch_url via prefetch cache lost per-result display rows (showed "1 result" in UI)
enabled: true
tags: [bug-fix, web_search, fetch_url, batch-mode, prefetch, streaming-tool-executor, ui]
created: 2026-04-21T03:59:21Z
updated: 2026-04-21T03:59:21Z
---

# Batch web_search / fetch_url prefetch display bug

## Symptom
When the model emits a batch `web_search` / `fetch_url` (queries/urls array),
the tool-round UI badge shows a wrong count like "1 result" and the collapsible
panel collapses all sub-searches into a single generic row, even though the
display header still correctly shows "2 searches: Q1 | Q2".

A "Prefetch" source label is visible, indicating the prefetch cache path
(not the regular batch handler) produced the rendering.

## Root cause
The streaming prefetch path in `lib/tasks_pkg/streaming_tool_executor.py`
`_execute_one` had separate batch branches for `web_search` and `fetch_url`
that returned a plain joined `str` **without `display_results` attached**.

Then `inject_into_cache` stored `_disp=None` in the dedup cache, and when
`tool_dispatch.execute_tool_pipeline` hit that cache entry it fell through
to `_build_cache_hit_meta()` which produced a single meta dict per call —
hence "1 result".

Additional gaps:
- `tool_dispatch.py` cache-hit branch only honored `cached_display` for
  `fn_name == 'web_search'`, not `fetch_url`.
- Cache-populate after normal (non-prefetch) execution also only captured
  display_results for web_search, so dedup replay of batch fetch_url also
  collapsed.

## Fix (all in this change)
1. `lib/tasks_pkg/streaming_tool_executor.py` `_execute_one`:
   - web_search batch: collect `results_per_q` alongside formatted text,
     merge into `all_display_results`, wrap in `_ContentWithDisplayResults`.
   - fetch_url batch: build one `display_result` dict per URL (title, url,
     source, fetched, fetchedChars) and wrap in `_ContentWithDisplayResults`.
2. `lib/tasks_pkg/tool_dispatch.py`:
   - Cache-hit branch: use `cached_display` for `fn_name in ('web_search',
     'fetch_url')` (added fetch_url).
   - Cache-populate after execution: capture display_results + engineBreakdown
     for both `web_search` and `fetch_url`.
   - `_build_cache_hit_meta`: add batch-mode fallback for fetch_url so a
     batch miss shows `"{n} URLs"` instead of empty-url single-page card.

## Invariant going forward
Any future batch tool whose frontend depends on a per-item `results` array
must attach `display_results` to the prefetch cache value (via
`_ContentWithDisplayResults` or equivalent) AND be enumerated in both
cache-hit / cache-populate branches in `tool_dispatch.py`.

