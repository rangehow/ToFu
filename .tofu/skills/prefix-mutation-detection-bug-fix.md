---
name: prefix-mutation-detection-bug-fix
description: Bug: PREFIX MUTATION detection compared growing hash ranges — fixed to use same-range comparison
enabled: true
tags: [cache, bugfix, cache_tracking]
created: 2026-04-10T07:59:27Z
updated: 2026-04-10T07:59:27Z
---

# PREFIX MUTATION Detection Bug (Fixed 2026-04-10)

## Bug
`detect_cache_break()` in `cache_tracking.py` computed `_prefix_count = prev.message_count - 2`
which GROWS each round as messages are added. The hash comparison was:
- Previous saved: `hash(messages[0:N])` 
- Current computed: `hash(messages[0:N+2])` (because prefix grew by 2)

These are different ranges, so hash always differs → **942 false positive warnings per day**.

## Fix
- Compare `hash(messages[0:prev_prefix_count])` against saved hash (same range = apples-to-apples)
- Save both `prefix_content_hash` AND `prefix_content_count` for next round
- Added `prefix_content_count` field to `CacheState.__slots__`

## Key insight
The `_prefix_count` calculation must use the SAVED prefix count from the previous round for 
COMPARISON, and the current round's `msg_count - 2` only for SAVING to state.

