---
name: chatui-multi-engine-search-architecture
description: ChatUI streaming search pipeline: engines + page fetch overlap (fetches start at ~0.7s when first engine returns, not ~11s waiting for all). Race-to-N, BM25 reranking.
enabled: true
tags: [python, search, brave, bing, searxng, ddg, multi-engine, architecture, streaming, performance]
created: 2026-03-27T15:23:47Z
updated: 2026-04-13T02:36:26Z
---

# ChatUI Search Pipeline — Streaming Architecture (v2)

## Key Design: Engine + Fetch Overlap
- **Old**: Step 1 (all engines) → Step 2 (dedup) → Step 4 (fetch) — sequential, SearXNG blocks
- **New**: Engines fire in parallel; as each engine returns results, URLs are immediately URL-deduped and submitted to a shared fetch ThreadPoolExecutor(16). First fetch starts at ~0.7s when DDG returns.

## Pipeline
1. **Engine pool** (5 workers): DDG-HTML(20), Brave(20), Bing(20), DDG-API(6), SearXNG(6)
2. **URL dedup** — incremental, runs as each engine batch arrives (thread-safe via `_lock`)
3. **Fetch pool** (16 workers) — starts receiving URLs as soon as first engine completes
4. **Content dedup** (Jaccard) — runs once after all engines done, on unique_results
5. **Race-to-N** — in step 4 wait loop, counts only `kept_urls` (post-content-dedup). Cancels remaining fetches once `target_ok = FETCH_TOP_N * 2` pages have content.
6. **LLM content filter** — relevance + cleaning (parallel)
7. **BM25 rerank** → top-N

## Key Files
- `lib/search/orchestrator.py` — `perform_web_search()` (streaming pipeline)
- `lib/fetch/core.py` — `fetch_page_content()` (individual URL fetch), `fetch_contents_for_results()` (legacy batch, unused by orchestrator now)
- `lib/search/dedup.py` — `dedup_by_content()` (Jaccard on title+snippet shingles)
- `lib/search/engines/` — individual engine implementations

## Thread Safety
- `_lock` (threading.Lock) protects: `seen_urls`, `all_results`, `unique_results`, `fetch_futs`
- Engine completion callbacks call `_submit_fetches_for_batch()` which acquires lock for dedup, then submits to fetch pool outside lock

## Logging
- `[Search] ⚡ First fetch submitted at +X.Xs` — shows when overlap starts
- `[Search] ⚡ Pipeline overlap saved ~X.Xs` — shows time saved vs old sequential approach
- Standard engine timings, fetch timings, Race-to-N cancellation logs preserved

