---
name: search-step5-noop-cost-when-filter-disabled
description: Search pipeline step5_llm_filter cost 1-3s even with FETCH_LLM_FILTER=0 due to unconditional tuple build + batch dict round-trip; fixed with FILTER_ENABLED short-circuit
enabled: true
tags: [performance, search, content-filter, bug-pattern]
created: 2026-04-18T06:44:56Z
updated: 2026-04-18T06:44:56Z
---

# step5_llm_filter 1-3s cost even when filter disabled

## Symptom
`logs/app.log` shows `step5_llm_filter=2.0s` / `3.5s` in pipeline timing
even though `FETCH_LLM_FILTER=0` and `BATCH skipped (filter disabled)`
appears immediately after `BATCH start`.

## Root cause (lib/search/orchestrator.py step 5)
Even when disabled, the orchestrator unconditionally:
1. Built `to_filter = [(r['url'], r['full_content']) for r in unique_results …]`
   — copies large page strings into new tuples.
2. Called `filter_web_contents_batch(...)` which returned
   `{url: text for url, text in items}` — another full dict copy.
3. Looped `unique_results` again to reassign `r['full_content'] = val`
   (same value).

Under 3 concurrent searches + FUSE log writes, this "no-op" costs 1–3s
per pipeline. The `step5_llm_filter` timer brackets all of it.

## Fix
Import `FILTER_ENABLED` from `lib.fetch.content_filter` and skip the
whole block when disabled:

```python
from lib.fetch.content_filter import FILTER_ENABLED as _FILTER_ENABLED
if not _FILTER_ENABLED:
    logger.debug('[Search] step5 skipped — FETCH_LLM_FILTER disabled')
else:
    # ... original tuple build + batch call ...
```

## Takeaway
When a feature is togglable via env var, the "disabled" path must
short-circuit at the caller — don't rely on the downstream function
to "just do nothing fast". Even trivial Python copy work adds up
when run concurrently on slow filesystems.

