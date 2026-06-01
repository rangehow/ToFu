---
name: fdp-project-overview
description: FDP project rules — forward-only sibling of CDP, fidelity-to-oracle gates
enabled: true
tags: [fdp, cdp, conventions, rules]
created: 2026-04-24T06:16:28Z
updated: 2026-04-24T06:16:28Z
---


# FDP Project — Key Rules (Summary)

FDP = Forward Data Prior. Sibling to `../CDP`. Drops backward SA + PoE fusion;
produces forward-only next-token prior aligned 1:1 with autoregressive LM training.

## Core Identity
- `P_fdp(tok | left_ctx) = P_fwd(tok | left_ctx)` at deepest `d*_fwd` where `distinct_next ≥ 2`.
- **Not** "CDP with backward disabled" — architecturally no backward SA, no reverse n-gram table, no `P_bwd`.
- Non-one-hot criterion: `distinct_next(c) ≥ 2`. No count threshold. `ngram_min_count` defaults to 1.

## Canonical Corpus
- Reuse `../CDP/_tok_cache/fineweb_1B.bin` (uint32 tokens, SEPARATOR=0xFFFFFFFF). Do not re-tokenize.

## Public API
```python
from fdp import create_engine, available_engines
engine = create_engine("v1", top_k=16)
engine.build(corpus_tokens)
result = engine.sweep(corpus_tokens)  # result.offsets / tokens / probs
```

## Quality = Fidelity to Forward Oracle (NOT entropy)
Ranked metrics:
1. `KL(P_true ‖ P_engine) ≤ 0.05 nats` (primary)
2. `support_exact ≥ 80 %`
3. `top1_overlap_weighted ≥ 80 %` (also k=4, 16)
4. Depth gap (diagnostic)
5. Non-one-hot % (floor only)

NOT metrics: entropy, avg support size, top-1-vs-corpus accuracy, FDP↔LM cross-entropy.

## Mandatory Acceptance Gate
`scripts/fdp_acceptance_gate.py` — 6 dims: correctness, memory, speed, scaling, quality-floor, fidelity.
Default scales: 2M, 10M, 50M, 200M (200M mandatory).

## Hardware Target
1T tokens in 48 h on one machine (500 GB RAM, 5 TB disk).
FDP expected ~2× faster than CDP v28 (one SA instead of two). CDP v28 projected 503 h → FDP ~250 h. Still 5× over → OP-1.

## Canonical Tests
- `tests/test_engine.py` — BaseEngine contract
- `tests/test_full_pipeline.py` — build → sweep → training label
- `scripts/fdp_validation_suite.py` — cross-engine benchmark
- `scripts/fdp_acceptance_gate.py` — 6-dim gate
- `fdp/diagnose.py` — forward-only `oracle_topk()` (to implement)

## Build
`build_all.sh` only. No per-version build scripts. C++ via pybind11 for hot paths.

## Training-label convention
`final_label = (1-λ) × one_hot(actual_token) + λ × FDP_distribution`
1-shift: position A's FDP dist is label at `<BOS>` output. Same as CDP.

## Open Problems
- **OP-1** (inherited): unbounded-depth precise forward fallback — ~250 h/1T projection, 5× over 48 h budget.
- **OP-2** (FDP-specific): Does FDP beat what LM learns internally? A/B vs one-hot & label smoothing.
- **OP-3** (FDP-specific): FDP as pragmatic hedge against CDP OP-1 intractability.

