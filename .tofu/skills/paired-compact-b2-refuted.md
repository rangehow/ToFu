---
name: paired-compact-b2-confirmed
description: Phase B2 (paired interstitial co-compact) A/B CONFIRMED via live API: -1.7% cost, cache-neutral
enabled: true
tags: [compaction, cache, a-b-test, confirmed-hypothesis]
created: 2026-04-26T23:34:45Z
updated: 2026-04-27T00:01:49Z
---

# Phase B2 paired co-compact — LIVE A/B CONFIRMED (2026-04-27)

## Finding: Phase B2 is cache-neutral-or-better AND saves extra tokens

When Phase B compacts a cold `tool` message at idx N, co-compacting
the paired `assistant(tool_calls).content` at idx N-1 is a net win,
despite moving the first-break index earlier by 1.

## Live test results (8 rounds × 2 arms, Opus 4.7)

`debug/test_paired_compact_live.py --rounds 8 --interval 10 --model aws.claude-opus-4.7`
(artifacts: `debug/paired_compact_live_20260427_075349.json`)

| Metric | BASELINE | PAIRED | Δ |
|---|---|---|---|
| Est tokens saved | 2,439 | 2,596 | **+6.4%** (more) ✅ |
| Cache writes | 32,402 | 31,942 | **-1.4%** (fewer) ✅ |
| Output tokens | 1,701 | 1,636 | -3.8% |
| **Total cost** | **$0.1613** | **$0.1586** | **-1.7%** ✅ |

Round-by-round cache_write deltas (PAIRED - BASELINE):
```
R4: -26   R5: +12   R6: -171   R7: -126   R8: -148
```
Consistently negative/near-zero after Phase B activates at R4.

## Why the local byte-hash analysis was misleading

The earlier local analysis correctly predicted that PAIRED's
first_break_idx moves from N to N-1 (earlier in prefix). But it
equated "earlier first_break" with "larger cache write" without
accounting for:

- The extra invalidated bytes at idx N-1 = ~110 tokens of interstitial
- But the same content, once compacted, is ~10 tokens — saving ~100
- Net per round: ~26-148 fewer cache_write tokens than BASELINE

Phase B2 is NOT structurally isomorphic to Phase D (which DID blow
up cost +57%). Phase D mutates assistants in non-tool-round contexts
where there's no offsetting savings — just pure added invalidation.

## Rollout state

- Flag `enable_paired_assistant_compact` in `micro_compact()` —
  still default OFF for caution (synthetic data; real interstitials
  vary in length and economics may shift).
- Safe to enable in `reactive_compact` / `force_compact` paths where
  cache is being rebuilt wholesale anyway — extra savings free there.
- To enable unconditionally after one more round of production
  observation, set the default to True in `micro_compact`.

## Reproducer

- Local byte-hash analysis: `debug/test_paired_compact_ab.py --local --rounds 15`
- Live API test: `debug/test_paired_compact_live.py --rounds 8 --interval 10 --model aws.claude-opus-4.7`
- Incremental-conversation design — each round appends to the running
  prefix (vs rebuilding from scratch) so cache continuity is real.

## Cache-invariant notes for this gateway (Sankuai + Claude)

- `add_cache_breakpoints` moves BP4 marker each round
- Each round's cache_write is the FULL new prefix (not delta)
  when any mutation happens in the cached range
- When no mutation, cache_read hits the previous prefix cleanly
  (verified with probe test)
- `sankuai_key_0` = App:**8427, per-minute rate limit (2 rpm floor for
  opus-4.7 at 30 rpm)
- `sankuai_key_1` = App:**4861, daily quota (resets midnight Beijing)

