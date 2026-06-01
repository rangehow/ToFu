---
name: fund-intel-v6-multi-source-crawler
description: Fund intel crawler v6 architecture: multi-source (Google News RSS + CLS telegraph + DDG time-filtered) with 5-layer date extraction, PORTAL_BLOCKLIST, concurrent fetching, and cross-category URL dedup
enabled: true
tags: [python, fund, crawling, news, multi-source, architecture, date-extraction]
created: 2026-03-23T08:57:01Z
updated: 2026-03-23T08:57:01Z
---

# Fund Intel Crawler v6 — Multi-Source Architecture

## File Layout
- `lib/fund/sources.py` — Multi-source fetchers (Google News RSS, CLS, DDG)
- `lib/fund/intel.py` — Crawl orchestration, date extraction pipeline, dedup
- `routes/fund_intel.py` — `_do_intel_crawl()` (background worker), API routes

## Sources (in priority order)
1. **Google News RSS** — `news.google.com/rss/search?q=...&hl=zh-CN` — up to 100 items, precise `<pubDate>` in RFC 2822 format, parsed via `email.utils.parsedate_to_datetime`
2. **CLS Telegraph** — `www.cls.cn/nodeapi/updateTelegraphList` — real-time flash news with unix `ctime` timestamps, filtered by category keywords
3. **DDG time-filtered** — `html.duckduckgo.com/html/?q=...&df=w` — past week filter for freshness

All run concurrently via `ThreadPoolExecutor(max_workers=4)`.

## 5-Layer Date Extraction Pipeline
- **Layer 0**: Source-provided dates (pubDate, ctime) — **most items stop here**
- **Layer 1**: Regex from title/snippet/URL (Chinese relative time, ISO dates, URL paths)
- **Layer 2**: HTML meta tags (`article:published_time`, JSON-LD `datePublished`)
- **Layer 3**: LLM batch extraction (cheap model, batches of 10)
- **Layer 4**: Web search verification (searches article title for date clues)
- **Layer 5**: Fallback to crawl date (`date_source='fetched_at_fallback'`)

## Critical Bug Pattern
The condition for triggering Layers 3-4 must be `date_source == 'fetched_at_fallback'`, NOT `not published_date` — because Layer 5 always sets published_date before this check.

## PORTAL_BLOCKLIST
39 URL patterns for data portal homepages, index pages, PDF reports that aren't actual news. Checked via `_is_blocked_url()`.

## Performance
- v5: 8-13 items/crawl (DDG only)
- v6: ~400-500 items/crawl (multi-source)
- ~85% of items get dates directly from source (no expensive extraction needed)

