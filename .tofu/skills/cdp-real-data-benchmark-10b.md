---
name: cdp-real-data-benchmark-10b
description: CDP benchmark on FineWeb-Edu-10B: min_count=2 mandatory, 72% singleton + 28% multi-token (avg 9.5), fusion is 67-73% bottleneck at 500M scale, InfiniGram tokenizer mismatch (GPT-2 vs LLaMA)
enabled: true
tags: [cdp, benchmark, real-data, fineweb, min-count, fusion, infgram, 10B]
created: 2026-03-24T01:07:37Z
updated: 2026-03-24T01:07:37Z
---

# CDP Real Data Benchmark (FineWeb-Edu-10B)

## Key Configuration
- `min_count=2` is **mandatory** — min_count=1 produces 100% one-hot self-match singletons
- `fusion_mode=6` (Bayesian), `top_k=200`
- GPT-2 tokenizer (V=50,256)

## Results at Scale (Bayesian mode, min_count=2)

| Scale | Singleton% | Multi% | Avg Dist (multi) | Avg Depth | Mtps | RSS B/tok | Fusion % |
|------:|----------:|-------:|-----------------:|----------:|-----:|----------:|---------:|
| 10M | 71.95% | 28.05% | 9.80 | 3.4 | 0.493 | 112 | 73% |
| 100M | 72.75% | 27.25% | 9.49 | 4.2 | 0.455 | 107 | 71% |
| 500M | 73.79% | 26.21% | 9.44 | 6.5 | 0.391 | 106 | 67% |

## Critical Findings
1. **Fusion is 67-73% of total time** with min_count=2 (unlike min_count=1 where singleton fast-path made it trivial)
2. **Singleton% is stable at ~72%** regardless of scale — intrinsic property of the text
3. **Depth grows with scale** (3.4→6.5 as corpus increases from 10M→500M) — larger corpus = longer shared contexts
4. **Coverage is 100%** of non-separator positions (Bayesian mode always has either fwd or bwd context)

## InfiniGram Comparison
- Available indices use LLaMA tokenizer, our corpus uses GPT-2
- Token strings with `▁` prefix (LLaMA) ≠ ` ` prefix (GPT-2) → 81.6% false negatives
- Where tokens are byte-identical (punctuation, suffixes): **exact probability match** confirmed
- Fair comparison requires same tokenizer index

## T-Level Projection (500M shards)
- 2,000 shards for 1T tokens
- 21.3 min/shard, 53 GB/shard
- 8 machines (768GB each): ~6.3 hours
- Fusion parallelization is the #1 optimization target

## Data Files
- `CDP/cache/fresh_{10M,100M,500M}_mc2.json` — Bayesian results
- `CDP/cache/fresh_10M_mc1_control.json` — Self-match control
- `CDP/cache/infgram_comparison.json` — InfiniGram API comparison

