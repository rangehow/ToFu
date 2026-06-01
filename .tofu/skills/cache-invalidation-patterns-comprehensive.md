---
name: cache-invalidation-patterns-comprehensive
description: Complete cache invalidation inventory with contention debunked and alternating HIT/MISS pattern explained
enabled: true
tags: [cache, invalidation, diagnostics, architecture, logging]
created: 2026-04-09T22:10:19Z
updated: 2026-04-10T08:30:48Z
---

# Cache Invalidation Patterns — Complete Inventory

## 10 Cache Systems in ChatUI

| # | Cache | Scope | Invalidation | Location |
|---|-------|-------|-------------|----------|
| 1 | Anthropic prompt cache | Per-model server-side | 5min/1h TTL, prefix byte mismatch | `llm_client.py` add_cache_breakpoints |
| 2 | Tool dedup cache | Per-task | Write ops, task end | `lib/tasks_pkg/tool_dispatch.py` |
| 3 | IndexedDB conv cache | Per-browser | Server writes, streaming end | `static/js/idb-cache.js` |
| 4 | Project tree cache | Global singleton | File write ops, explicit refresh | `lib/project_mod/` |
| 5 | PG schema version cache | Per-boot | Schema version increment | `lib/trading/trading_config.py` |
| 6 | Provider/model list cache | Per-server | Settings change | `lib/llm_dispatch.py` |
| 7 | Skills BM25 cache | Per-request | Skills file change | `lib/skills.py` |
| 8 | Intel crawl_log cache | Per-simulation | Monthly dedup | `lib/trading/news_apis.py` |
| 9 | Cross-DC FUSE benchmark cache | Per-boot | Startup only | `lib/cross_dc.py` |
| 10 | File reader cache | Per-request | N/A (stateless) | `lib/file_reader.py` |

## Anthropic Prompt Cache — Key Findings

### ★ Cache Contention Does NOT Exist (A/B Tested 2026-04-10)

**VERIFIED**: Two conversations on the same model with different prefixes do NOT evict
each other's cache. Per-round cache_read is identical (±0.0%) between solo and interleaved
modes. Anthropic cache is keyed on exact prefix bytes — different conversations have
different keys and cannot interfere.

The old `_count_active_on_model()` heuristic was removed from cache break detection.
Function retained for diagnostics only.

Interleaving can actually HELP: Conv B's traffic keeps the shared system+tools prefix warm,
benefiting Conv A's cache hits.

### Real Causes of Unexplained Cache Drops
1. **TTL expiry** (>5min gap between rounds)
2. **Breakpoint advancement** — BP4 moves forward each round; old position's cache
   is still valid but not at a breakpoint position in the new request, so no read
3. **Server-side capacity pressure** (rare, Anthropic-side)
4. **Compaction** — message count drops, expected cache rebuild

### Alternating HIT/MISS Pattern in Small Prompts
When system+tools < 4096 tokens, only BP4 provides cacheable segments. BP4 moves
each round, causing alternating WRITE/HIT pattern. NOT a problem in production where
system+tools >> 4096 tokens (BP1-BP3 provide stable baseline hits).

### PREFIX MUTATION Detection (BUG FIXED 2026-04-10)
Was producing 942 false positives/day due to comparing hash ranges of different sizes.
Fixed: compare same-range hash (prev_prefix_count) across rounds.

## v2 Diagnostics Additions
- Per-tool hash diffing: identifies WHICH tool definition changed
- Prefix mutation detection: catches silent in-place message edits (now fixed)
- Session-level aggregate stats: total read/write/breaks/hit-rate per session
- Stale state cleanup: `cleanup_cache_state()` on conversation delete

