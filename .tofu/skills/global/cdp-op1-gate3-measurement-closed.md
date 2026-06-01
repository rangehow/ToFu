---
name: cdp-op1-gate3-measurement-closed
description: CDP OP-1 Iron Rule 8 is measurement-closed; four iteration axes exhausted
enabled: true
tags: [cdp, op-1, scope-decision, iron-rule-8, conventions]
created: 2026-04-25T08:47:27Z
updated: 2026-04-25T08:47:27Z
---

# CDP OP-1 / Iron Rule 8 — Measurement-Closed (as of 2026-04-25)

Iron Rule 8 = 1 T tokens in 48 h on one 500 GB / 64-core machine with
KL = 0 to the PoE-fused oracle. **Currently unreachable.** Do NOT
scaffold another v3x C++ engine hoping to close the 7.6× gap without
first getting a scope-decision code from the user.

## Status ledger (measured)

- **v29g** (champion): 364 h / 1 T, 244 B/tok, KL = 0. 7.6× over Gate 3.
- v30 (RLBWT): retired 2026-04-24 — 3600× slower on 20K slice, 66.7% byte-exact.
- v31 γ1 (FM+sampled SA): Python prototype on this host's Qwen3 —
  **1.5-4.75× slower than v29g already**, scaling worse. Measurement-rejected.
- v31 γ2 (bounded depth-promotion): re-hits v29c's D*=75 OOM on FineWeb boilerplate.
- v31 γ3 (GPU): outside Iron Rule 8 hardware envelope.
- v31 δ (compressed LCP): LCP is only 3.3% of engine B/tok — even free lcp_get doesn't change shard count (still 4).

## Why nothing inside the current family closes 7.6×

Three independent walls, each must be breached:
1. **Memory wall** — SA+ISA+text floor ~100 B/tok caps shards at 4-5. Need ≤40 B/tok for 12+ shards.
2. **Throughput wall** — need 12× speedup; SIMD+prefetch headroom is 2-3×; v29g already took 1.37×.
3. **Algorithmic wall** — RLBWT/FM gives memory but loses O(1) LCP-walk; per-position cost explodes.

No public data structure simultaneously provides: (a) ≤ 40 B/tok, (b) O(1)
per-depth transition, (c) distinct_next enumeration without O(width) scan.

## Decision codes (§6)

`[A-96h | A-168h | A-364h | B-2node | B-1TB | C-gamma | D-100M | D-ship-v29g | E-v30-gate2-stub]`

Planner's recommendation: **A-168h** or **D-ship-v29g**. Others need infra
commitment (B, C) or corpus-cap acceptance (D-100M).

## Key convention (Iron Rule 9 sharpened)

Before scaffolding any new C++ engine variant:
1. Component-cost the primitive against v29g on THIS host's Qwen3 corpus (Python prototype OK).
2. If the primitive alone is slower per-position than v29g, or its B/tok win doesn't change shard count, STOP — skip steps 3-7, go to step 8 (honest stop).
3. Literature constants (e.g. Infinigram "20-50× slower") are NOT a substitute for in-tree measurement; measured on our corpus γ1 was 6.6-8.7×, milder than lit, but still insufficient.

## Honest stop is valid

Per v31_prior_art.md §5: if no breakthrough is plausible in one session,
skip to "Iron Rule 8 needs scope decision, not more engine code." Default
remains v29g @ 364 h / 1 T with Gate 3 FAIL documented.

