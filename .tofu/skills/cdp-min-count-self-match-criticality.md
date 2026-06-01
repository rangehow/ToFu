---
name: cdp-min-count-self-match-criticality
description: Critical CDP config: min_count=1 produces 100% one-hot distributions (self-match) making CDP useless for training; min_count=2 is required for meaningful distributional signal (26% multi-token at 1B scale). Phase A in batch_extract_pooled has guard `if (min_count_ <= 1)` that prevents this.
enabled: true
tags: [cdp, critical-bug, min-count, self-match, singleton, training, distributional-signal]
created: 2026-03-23T19:29:31Z
updated: 2026-03-23T19:29:31Z
---


# CDP min_count Self-Match Criticality

## The Problem
With `min_count=1` (the C++ engine default until this fix), every position matches itself at 
maximum depth in the suffix array, producing a trivial one-hot (singleton) distribution.
This makes CDP training equivalent to standard next-token prediction — the distributional 
signal is completely lost.

## Evidence (FineWeb-Edu-10B, V=50257, Bayesian mode)
| min_count | Singleton % | Multi-token % | Avg Dist Size | Avg Depth |
|:---------:|:-----------:|:-------------:|:-------------:|:---------:|
| 1         | 99.9%       | 0.0%          | 1.00          | 2,792     |
| **2**     | **73.7%**   | **26.2%**     | **3.21**      | **6.5**   |

## Root Cause: Phase A in `batch_extract_pooled`
```cpp
// Phase A: Pre-assign singletons (ONLY when min_count <= 1)
if (min_count_ <= 1) {
    for (int i = 0; i < n; i++) {
        if (full_depth > interval_depth && full_depth >= min_ctx_) {
            pos_interval[rp] = pool.add_singleton(target[i], d);
            uf[i] = i + 1;  // skip in Phase B
        }
    }
}
```
When `min_count >= 2`, Phase A is disabled, and positions fall through to Phase B which
requires `count >= min_count` in the LCP interval.

## Fix Applied
- C++ default changed from `min_count_ = 1` to `min_count_ = 2` in v11/v12/v13
- Python wrapper `CDPEngineWrapper` already defaulted to `min_count=2`
- All benchmark scripts updated to use `min_count=2`

## Scaling Behavior (min_count=2, Bayesian)
Multi-token fraction is stable at 26-28% from 10M to 903M tokens.
Average forward depth grows from 3.4 (10M) to 7.4 (903M).
RSS is ~220 B/tok (47% more than min_count=1 due to larger DistPool).

