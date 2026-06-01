---
name: cdp-self-match-singleton-bug
description: Critical CDP bug: querying the same corpus with min_count=1 produces 100% one-hot singleton distributions because every position matches itself at maximum depth — fix with min_count=2, self-exclusion, or separate index/query corpora
enabled: true
tags: [python, cpp, cdp, suffix-array, infgram, self-match, singleton, critical-bug, min-count]
created: 2026-03-23T15:32:52Z
updated: 2026-03-23T15:32:52Z
---

# CDP Self-Match Bug: 100% Singleton Distributions

## The Problem
When the CDP engine indexes corpus C and queries the SAME corpus C with `min_count=1`:
- Every position `pos` trivially matches itself at maximum depth (full document prefix)
- At maximum depth, the context is globally unique → only 1 match (self)
- Distribution = `{corpus[pos]: 1.0}` → 100% useless one-hot singletons
- Coverage is 100% but distributions have ZERO information content

## Why It Happens
The ∞-gram uses the **longest matching suffix** of the left context. At depth d:
- `E[matches] ≈ N / V^d` — drops exponentially with depth
- For V=32K, N=1B: even at depth 2, most contexts are unique
- The SA always finds at least 1 match at ANY depth: the position itself
- With `min_count=1`, the engine uses maximum depth → self-match → singleton

## Evidence
- 50K real tokens: avg depth = 1.9 for shared contexts, but engine uses depth ~4,000
- 1B real tokens: avg depth reported as 3,953 — virtually all self-matches
- 30-gram overlap at 50K: 0 positions share context (0%)
- InfiniGram (1.5T token index) returns 90-300+ unique tokens per context

## Three Fixes

### Fix 1: `min_count=2` (simplest, recommended)
```python
engine.set_min_count(2)
```
Forces at least 2 matches (self + 1 other). Engine backs off to deepest shared depth.
- 50K corpus: 94% coverage, avg 303 unique tokens per distribution
- 10B corpus: >99% coverage projected

### Fix 2: Self-exclusion in SA sweep
```cpp
// Skip self-match in collect_forward
for (j = lo; j < hi; j++) {
    if (sa[j] == target_pos) continue;  // exclude self
    if (bwt_next[j] != SEPARATOR) c[bwt_next[j]]++;
}
```

### Fix 3: Separate index/query corpora (production)
Index corpus A, query corpus B. No self-match possible.

## Quality Metrics to Add
- **Singleton %** (should be <50%, not 100%)
- **Avg distribution size** (should be >5, not 1)
- **Distribution entropy** (should be >1.0 bits, not 0.0)

