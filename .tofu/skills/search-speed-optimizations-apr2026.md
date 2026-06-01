---
name: search-speed-optimizations-apr2026
description: Search speed optimizations: streaming pipeline, browser fallback semaphore (3 concurrent, 15s timeout), SearXNG 2×2s, paywall domain skip list
enabled: true
tags: [performance, search, browser-fallback, searxng, optimization]
created: 2026-04-13T02:40:56Z
updated: 2026-04-13T02:40:56Z
---

# Search Speed Optimizations — April 2026

## Problem
Search was taking 70-100s per call. Root causes:
1. Sequential pipeline: engines (11s) → dedup → fetch (60s) — no overlap
2. SearXNG always fails from datacenter (302→homepage bot block), wastes 10-17s
3. Browser fallback floods: 19+ concurrent 25s-timeout requests to browser extension
4. Paywall domains (Medium, IEEE, Springer) always fail via browser, each wastes 25s

## Fixes Applied

### 1. Streaming Pipeline (orchestrator.py)
- Engines + page fetch now overlap: as each engine returns, URLs immediately submitted to fetch pool
- First fetch starts at ~0.7s (when DDG returns) instead of ~11s (waiting for SearXNG)
- Content dedup runs after all engines, Race-to-N still applies

### 2. Browser Fallback Concurrency Cap (http.py)
- `_browser_semaphore = Semaphore(3)` — max 3 concurrent browser fetches
- Non-blocking acquire: excess requests skip instantly instead of queuing
- Timeout reduced from 25s → 15s

### 3. Paywall Domain Skip List (http.py)
- `_BROWSER_SKIP_DOMAINS` frozenset with 16 domains (medium.com, ieee, springer, etc.)
- Browser fallback skipped entirely for these domains — saves 15-25s each

### 4. SearXNG Speed (searxng.py)
- Reduced from 3 instances × 5s → 2 instances × 2s
- Added `allow_redirects=False` to detect 302→homepage bot blocks instantly
- Down from 11-17s to ~3s (still fails, but faster and overlapped)

## Expected Impact
Before: ~70-100s per search call
After: ~45-60s (streaming saves ~10s, browser cap saves ~15-20s, SearXNG saves ~8s)

