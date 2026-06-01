---
name: chatui-intel-cache-and-news-apis
description: Intel backfill caching via crawl_log + structured news APIs (Eastmoney np-listapi, Sina Finance, Investing.com RSS) in lib/trading/news_apis.py — repeat simulations skip already-crawled category+query+month combos
enabled: true
tags: [python, trading, intel, cache, news-api, eastmoney, sina, rss, simulation]
created: 2026-03-29T10:52:29Z
updated: 2026-03-29T10:52:29Z
---

# Intel Caching & Structured News APIs

## Problem
- Every simulation run re-crawled ALL intel categories × months × queries via web search, even when the same data was already in DB
- Intel sources were limited to web search scraping (Google News RSS, DDG, CLS)

## Solution

### 1. Cache-Aware Backfill (`lib/trading/historical_data.py`)
- `backfill_historical_intel()` now checks `trading_intel_crawl_log` before each query
- Key: `(category, source_key=md5(query)[:12], crawl_date)` — matches what `record_crawl()` writes
- Pre-scan counts cached vs needing-fetch queries, reports cache hit rate in progress
- Repeat simulations with same date range are near-instant for intel phase

### 2. Structured News APIs (`lib/trading/news_apis.py`)
Three new sources called BEFORE search-based crawling:

1. **Eastmoney News API** (`np-listapi.eastmoney.com/comm/web/getNewsByColumns`)
   - Paginated JSON, precise `showTime` timestamps, article `code` for dedup
   - 8 column IDs mapping to intel categories (macro, market, sector, global, bonds, funds)
   - No API key needed; no JSONP wrapper when callback param omitted

2. **Sina Finance** (`feed.mix.sina.com.cn/api/roll/get`)
   - JSON API with Unix epoch timestamps, multiple category feeds
   - 4 feeds: 财经要闻, 股票要闻, 基金要闻, 美股快报

3. **Investing.com RSS** (Chinese edition)
   - Standard RSS/XML, 7 feeds covering macro/bonds/forex/commodities
   - May be blocked from some datacenter IPs (ConnectionError handled gracefully)

### Key Integration Points
- `fetch_structured_news_sources()` is called in Phase 0 of `backfill_historical_intel()`
- Uses existing `deduplicate_intel()` (3-layer: URL → SimHash → title prefix)
- Stores in `trading_intel_cache` with `date_source='source_api'`

