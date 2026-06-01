---
name: cdp-min-count-2-real-data-benchmark-1B
description: CDP benchmark on real FineWeb-Edu-10B: min_count=2 mandatory (min_count=1 gives 100% useless one-hot singletons via Phase A self-match bypass), verified correct via brute-force at 100K and InfiniGram API, scaled to 1B tokens (2440s, 0.41 Mtps, 78.7% singleton, 21.3% multi-token avg size 6.9)
enabled: true
tags: [cdp, benchmark, min-count, real-data, fineweb, 1B-scale, self-match, phase-a, distribution-quality]
created: 2026-03-23T23:56:36Z
updated: 2026-03-23T23:56:36Z
---

# CDP Benchmark on Real FineWeb-Edu-10B Data

## Critical Finding: min_count=2 is MANDATORY

With `min_count=1`, Phase A of `batch_extract_pooled` pre-assigns every position as a singleton
(self-match at maximum depth), producing **100% one-hot distributions** that are useless for training.

The guard `if (min_count_ <= 1)` in Phase A of v11/v12/v13 engines disables this when `min_count >= 2`.

### Real Data Results (FineWeb-Edu-10B, LLaMA-2 tokenizer)

**Bayesian mode, min_count=2, top_k=200:**

| Scale | Singleton | Multi-token | Avg Dist Size | Mean Depth | Throughput | Memory |
|------:|----------:|------------:|--------------:|-----------:|-----------:|-------:|
| 100K  | 73.9%     | 25.8%       | 13.7          | 2.3        | 1.63 Mtps  | 24 B/tok |
| 1M    | 75.9%     | 24.1%       | 8.1           | 3.3        | 1.16 Mtps  | 24 B/tok |
| 10M   | 76.2%     | 23.8%       | 7.3           | 3.8        | 0.87 Mtps  | 24 B/tok |
| 100M  | 77.2%     | 22.8%       | 7.1           | 4.9        | 0.61 Mtps  | 12 B/tok |
| 500M  | 78.3%     | 21.7%       | 6.9           | 7.2        | 0.49 Mtps  | 12 B/tok |
| 1B    | 78.7%     | 21.3%       | 6.9           | 8.7        | 0.41 Mtps  | 12 B/tok |

### Correctness Verification
- **200/200 positions bit-exact** vs brute-force O(N²D) ground truth (top_k=2000)
- InfiniGram API cross-check: all engines agree (L1=0.86 reflects corpus size difference)

### Python Default Fixes Applied
- `cdp/shard_processor.py`: min_count default changed from 1 → 2
- `bench_fineweb_complete.py`: hardcoded min_count changed from 1 → 2
- `cdp/engine.py`: already had min_count=2 default

### Key Dataset Paths
- 10B: `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/BERT_TRAINING_SERVICE/platform/dataset/EleutherAI/fineweb-edu-dedup-10b/main/data/`
- Cached tokenized: `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/CDP/cache_10b/`

