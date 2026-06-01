---
name: cdp-phase-a-self-match-bug
description: Critical CDP bug: Phase A in batch_extract_pooled assigns self-match singletons (count=1) without checking min_count, causing min_count>=2 to produce 99.9% one-hot distributions identical to min_count=1. Fix: wrap Phase A in `if (min_count_ <= 1)` guard. Affects v11/v12/v13.
enabled: true
tags: [cpp, cdp, critical-bug, self-match, min-count, phase-a, singleton, batch-sweep]
created: 2026-03-23T16:49:41Z
updated: 2026-03-23T16:49:41Z
---

# CDP Phase A Self-Match Bug

## Bug Location
`batch_extract_pooled()` in `cdp_v11_engine.cpp`, `cdp_v12_engine.cpp`, `cdp_v13_engine.cpp`

## Root Cause
Phase A pre-assigns positions with unique suffixes (`full_depth > interval_depth`) as singletons
WITHOUT checking `min_count_`. These self-matches (count=1) bypass Phase B's `count >= min_count_`
gate entirely via `uf[i] = i + 1`.

## Impact
- `min_count=1` and `min_count=2` produce **bit-identical results** (the bug)
- 99.7-99.9% of distributions are one-hot singletons (self-matches at depth ~1000-4000)
- The ∞-gram degenerates to memorized next-token labels — zero distributional information

## Fix
```cpp
// Phase A: Pre-assign singletons (only when min_count <= 1)
if (min_count_ <= 1) {
    for (int i = 0; i < n; i++) { ... }
}
```

## Impact After Fix (FineWeb-Edu-10B, min_count=2)

| Scale | Singleton → | Multi-token → | Avg Depth → |
|------:|---:|---:|---:|
| 500K | 99.9% → **13.5%** | 0.1% → **86.5%** | 1893 → **2.7** |
| 50M  | 99.7% → **14.5%** | 0.3% → **85.5%** | 3283 → **4.4** |
| 200M | ~99.7% → **15.5%** | ~0.3% → **84.5%** | ~3000 → **5.4** |

## Detection
Any ∞-gram system reporting >95% singletons on real text is likely self-matching.
Real text at 500K+ tokens should have 80-90% multi-token distributions with depth 2-5.

## FM-Index engines (v6-v9) are NOT affected
They do per-position backward extension queries that naturally back off to shallower
depths when `min_count >= 2`, never hitting a "Phase A" shortcut.

