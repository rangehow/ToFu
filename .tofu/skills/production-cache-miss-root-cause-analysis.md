---
name: production-cache-miss-root-cause-analysis
description: Root cause of production cache misses: proxy-level server-side inconsistency, NOT contention or BP4 advancement
enabled: true
tags: [caching, anthropic, production, debugging]
created: 2026-04-10T09:32:27Z
updated: 2026-04-10T09:32:27Z
---

# Production Cache Miss Root Cause Analysis (2026-04-10)

## Key Findings

### What causes the ~15% base miss rate in production?
**Server-side inconsistency in the sankuai → AWS Bedrock proxy layer.**

### Evidence:
1. **Identical prompt test**: Sending the EXACT same prompt 10 times (2s gaps), calls 2 and 4 got cache MISS despite identical bytes. After stabilization, calls 5-10 all HIT.
2. **Not contention**: A/B test proved different conversations don't evict each other (per-round cache_read identical ±0.1%).
3. **Not BP4 advancement**: Test showed advancing BP4 forward retains incremental cache hits (T3-T5 all hit).
4. **Not TTL expiry**: 57% of drops happen within 10 seconds (well within 5min TTL).
5. **Random, not periodic**: Miss pattern is random with clusters, consistent with load-balanced backends.

### Most likely cause:
Proxy load-balances across multiple Anthropic/Bedrock instances. Each instance has its own cache. When a request lands on a different instance, it's a cache miss.

### The "drop to baseline" pattern:
- Misses always drop cache_read to a FIXED value per conversation (e.g., 12,508 or 21,185)
- This baseline = system + tools tokens (cached with 1h TTL at BP1-BP3)
- These survive even 5+ hour gaps (confirmed: 318 min gap still had cache hit)
- Only the conversation tail (BP4, 5m TTL) is lost on server-side miss

### Stats:
- Total API calls: 2,973
- Overall cache hit rate: 84.2%
- Tokens wasted on server-side misses: ~26M (12.1% of total)
- Estimated extra cost: ~$97/day
- Per-conversation miss rate: consistent 10-17%

### What we CANNOT fix:
- Server-side routing is outside our control
- The proxy's internal load balancing behavior

### What we CAN do:
- Accept ~15% miss rate as infrastructure baseline
- The 1h TTL on BP1-BP3 IS working (protects system+tools across tasks)
- The mixed TTL strategy is correct (1h stable prefix, 5m volatile tail)

