---
name: cdp-min-count-2-mandatory-real-data-benchmark
description: CDP benchmark on FineWeb-Edu real datasets: min_count=2 is MANDATORY (min_count=1 gives 100% one-hot self-match), Phase A guard in batch_extract_pooled bypasses min_count check, verified on 1B and 10B at 500M-1B scale with brute-force correctness
enabled: true
tags: [cdp, benchmark, min-count, self-match, real-data, fineweb, singleton, distribution-quality]
created: 2026-03-23T21:41:11Z
updated: 2026-03-23T21:41:11Z
---

# CDP Real-Data Benchmark: min_count=2 Is Mandatory

## Critical Finding
With `min_count=1`, **100% of distributions are one-hot** (singletons) because every 
position matches itself at maximum depth. Phase A in `batch_extract_pooled` pre-assigns 
singletons for positions whose full context depth exceeds the LCP interval depth — these 
are self-matches. The guard `if (min_count_ <= 1)` on Phase A exists in v11/v12/v13.

## Verified Results (min_count=2, Bayesian, v13, top_k=200)

| Dataset | Scale | Singleton | Multi-tok | AvgDist | AvgDepth | Time | RSS |
|:--------|------:|:---------:|:---------:|:-------:|:--------:|-----:|----:|
| 1B-LLaMA | 10M | 71.9% | 28.0% | 3.47 | 3.4 | 22s | 2.3GB |
| 1B-LLaMA | 500M | 73.7% | 26.2% | 3.21 | 6.5 | 1323s | 108GB |
| 1B-LLaMA | 903M | 74.0% | 25.9% | 3.21 | 7.4 | 2689s | 194GB |
| 10B-GPT2 | 500M | 73.8% | 26.2% | 3.21 | 6.5 | 1338s | 110GB |
| 10B-GPT2 | 1.0B | 74.1% | 25.9% | 3.20 | 7.6 | 2799s | 218GB |

## Distribution Quality (multi-token positions)
- Mean entropy: 1.0 bit
- 69.6% have max_prob < 0.9 (genuinely uncertain)
- 13.6% have max_prob < 0.5 (truly uncertain)

## Brute-Force Verification
- 200/200 singletons verified as GENUINE (count≥2, same next token)
- 47/50 multi-token distributions match brute-force (3 mismatches from top-K truncation at depth-1)
- 0 self-matches detected with min_count=2

## Key Property
Singleton rate (~74%) is INDEPENDENT of dataset — identical between 1B-LLaMA and 10B-GPT2 
at same shard size. It's determined by corpus size, not content.

