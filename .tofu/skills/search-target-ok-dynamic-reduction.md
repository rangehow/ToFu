---
name: search-target-ok-dynamic-reduction
description: Dynamic target_ok reduction in search orchestrator prevents 90s as_completed ceiling when candidate pool is small
enabled: true
tags: [search, performance, race-to-n, orchestrator]
created: 2026-04-20T04:41:41Z
updated: 2026-04-20T04:41:41Z
---

# Dynamic target_ok reduction in search orchestrator

## Problem
`lib/search/orchestrator.py` perform_web_search uses Race-to-N exit where
`target_ok = FETCH_TOP_N * 2` (default 16). When the kept-URL candidate pool
after content dedup is too small (e.g. 13 URLs for target 16), Race-to-N can
NEVER exit — even if all fetches succeed, `kept_ok < target_ok` forever.
The pipeline then falls back to the hard `as_completed(timeout=90)` ceiling,
logging `[Fetch] as_completed timeout (90s)` and TOTAL ≈ 93s.

## Fix (2026-04-20)
Right before step 4, after content dedup and `kept_urls` is built:

```python
_original_target_ok = target_ok
if len(kept_urls) < target_ok * 1.5:
    target_ok = max(max_results, int(len(kept_urls) / 1.5))
    if target_ok < _original_target_ok:
        logger.info('[Fetch] target_ok reduced %d → %d ...', ...)
```

- `1.5x` headroom ensures target is achievable even with some fetch failures
- Floor at `max_results` so we never ship fewer pages than the user asked for
- Triggered only when needed; logs the adjustment for diagnosis

## Related
- Two concurrent `web_search` calls each spawn their own `ThreadPoolExecutor(max_workers=16)`,
  so 32 total fetch workers compete for the 3-slot browser semaphore.
- `as_completed(timeout=90)` is the hard ceiling — NOT a stack of per-URL timeouts.
- `target_ok` itself (FETCH_TOP_N * 2) is §10.1 hyperparameter; this patch only
  reduces it dynamically when the candidate pool can't support it.

