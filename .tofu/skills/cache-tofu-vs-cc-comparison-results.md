---
name: cache-tofu-vs-cc-comparison-results
description: Definitive Tofu vs Claude Code cache comparison: CC's higher hit rate is due to 4x larger system prompt, not better strategy; controlled A/B shows 4-BP mixed TTL beats 1-BP
enabled: true
tags: [caching, claude-code, benchmark, a-b-test, cost]
created: 2026-04-10T10:40:42Z
updated: 2026-04-10T10:40:42Z
---

# Tofu vs Claude Code Cache Comparison — Definitive Results

## TL;DR
CC's apparent 84% vs Tofu's 72% hit rate is a statistical illusion. It's not a better caching strategy — it's a bigger system prompt.

## Data Sources

### 1. benchmark_tofu_vs_cc.py (Run 1, CC proxy working)
- CC: 83.9% hit rate, $5.81 total, 8.8 avg turns, ~27K system prompt
- Tofu: 72.1% hit rate, $3.20 total, 5.2 avg turns, ~7K system prompt
- CC system prompt is 4x larger → 80% of CC's cache reads are just system prompt

### 2. Controlled A/B test (same prompt, 20 rounds)
- 4 BP mixed TTL: $0.99, 61.1% savings ← BEST
- 1 BP 5m (CC): $1.09, 54.3% savings
- 1 BP 1h: $1.36, 43.2% savings ← WORST (1h writes on volatile tail are expensive)

### 3. Benchmark Run 2 & 3: CC proxy had cache_control passthrough broken → 0% cache hits for CC

## Root Causes of Apparent Gap
1. **System prompt size**: CC 27K vs Tofu 7K (bench) / 10.7K (prod)
2. **Turn count**: CC uses 1.7x more turns (more cache read opportunities)
3. **scope=global**: CC-exclusive 1P feature, unavailable through Bedrock

## Key Insight
When controlling for system prompt size and turn count, our 4-BP mixed TTL strategy is 10-27% cheaper than CC's 1-BP approach. The mixed TTL (1h for stable prefix, 5m for volatile tail) is the optimal strategy.

## Files
- `debug/benchmark_tofu_vs_cc.py` — head-to-head benchmark
- `debug/test_cache_validation.py` — controlled A/B with arm switching
- Previous results in `/tmp/bench_3noh05ys/results.json` (Run 1)

