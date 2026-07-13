---
name: anthropic-cache-breakpoints-mixed-ttl
description: Cache breakpoint placement (v1-v6): BP4 tail fix, mixed TTL strategy (1h for system+tools, 5m for tail), A/B test results, Anthropic prompt_tokens convention
enabled: true
tags: [python, debugging, anthropic, cache_control, llm-client, prompt-caching, openai]
created: 2026-03-20T00:50:21Z
updated: 2026-07-08T00:00:00Z
---

# Anthropic Cache Breakpoints — Full History & Mixed TTL

## Bug History (v1-v5)
- v1: BP4 on second-to-last USER msg → oscillation in tool convos
- v2: BP4 on msg[-2] → skips last tool result, 100-400 uncached tokens/round
- v3: BP4 on msg[-1] → works! uncached tokens ≈ 1/round
- v4: Phase-0 stripping to prevent BP accumulation (HTTP 400 from >4 BPs)
- v5: Backward scan up to 5 positions to handle empty-content assistants
- v6: Mixed TTL — 1h for BP1-BP3 (system+tools), 5m for BP4 (tail)

## Mixed TTL Strategy (v6)
```python
if use_extended_ttl:
    _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}   # BP1-BP3
    _cc_tail   = {'type': 'ephemeral'}                 # BP4 (default 5m)
```

### Key constraints
- 1h entries MUST appear BEFORE 5m entries in the message array
- BP1-BP3 (system+tools) naturally precede BP4 (tail) → constraint satisfied
- Beta header required: `anthropic-beta: extended-cache-ttl-2025-04-11`
- 1h cache writes cost 2.0x (vs 1.25x for 5m)
- Cache reads cost the same 0.1x regardless of TTL

### When 1h TTL helps
- Scenario D (mixed empty-content assistants): -16.1% cost
- Long conversations (>30 min) with server-side evictions: eliminates system re-writes
- User conversations with >5 min gaps between messages

### When 1h TTL doesn't help
- Short conversations (<5 min total): no evictions to prevent
- Scenarios with very few rounds: extra 2x write cost not amortized

## A/B Test Results (2026-04-04, aws.claude-opus-4.6)

### OLD (msg[-2]) vs NEW (msg[-1])
| Scenario | OLD Cost | NEW Cost | Savings |
|---|---|---|---|
| A (single query multi-tool) | $0.5756 | $0.5142 | 10.7% |
| B (multi-turn user conv) | $0.4584 | $0.2974 | 35.1% |
| C (parallel tool calls) | $0.4498 | $0.4439 | 1.3% |
| D (mixed content assistants) | $0.5142 | $0.4307 | 16.2% |
| **OVERALL** | **$1.9980** | **$1.6862** | **15.6%** |

### NEW vs NEW+1h (extended TTL)
| Scenario | NEW | NEW+1h | Delta |
|---|---|---|---|
| A | $0.5142 | $0.5131 | -0.2% ➖ |
| B | $0.2974 | $0.3073 | +3.3% ➖ |
| C | $0.4439 | $0.4582 | +3.2% ➖ |
| D | $0.4307 | $0.3614 | **-16.1%** ✅ |

### Key metric: uncached tokens per round (after cache hit)
- OLD: 161-369 tokens/round
- NEW: 1.0 tokens/round
- Reduction: 99.4-99.7%

## Anthropic prompt_tokens Convention
Anthropic API returns `prompt_tokens` as **uncached only** (NOT total).
- `total input = prompt_tokens + cache_write_tokens + cache_read_tokens`
- Detection heuristic in frontend: `if (inp <= cacheWrite + cacheRead)` → Anthropic convention
- Frontend `calcCostCny()` handles both conventions correctly

## Cache activation threshold
- Opus: 4096 tokens minimum per cache block
- System prompt ~2500 tokens: cache doesn't activate until R3-R4 (when total > 4096)
- Production system prompt ~14K tokens: cache hits from R1

## Server-side TTL evictions
- Anthropic's cache has a 5-minute TTL by default
- In long conversations, cache drops to ~13,988 tokens (system+tools prefix only)
- Mixed TTL (1h for system+tools) eliminates these re-writes for the stable prefix
- Tail evictions (BP4) still happen but cost less since tail is smaller

## Files
- `lib/llm/cache.py`: `add_cache_breakpoints()` — mixed TTL + 4-marker RESERVATION model (was `lib/llm_client.py` pre-`llm/`-package refactor; path corrected 2026-07-08)
- `lib/__init__.py:195`: `CACHE_EXTENDED_TTL` setting (default ON) — still valid
- `routes/common.py`: `/api/features` GET/POST for hot-reload
- `tests/test_cache_breakpoints.py`: mixed-TTL + reservation tests
- `debug/test_cache_validation.py`: A/B test with `--ttl` flag for 3-arm comparison

## Reservation model (the current shape — see `cache-breakpoint-tail-starvation-system-blocks`)
`add_cache_breakpoints` RESERVES 1 marker for the last tool def + 1 for the
conversation tail UP FRONT (`_MAX_CACHE_BP=4`, `_system_bp_budget = 4 -
_reserve`); the system phase gets only the leftover (≤2). All 4 markers are
consumed in the production shape (system×2 + tool×1 + tail×1). There is NO
idle 5th slot.

## P5 (open design note, not implemented — see JOURNAL 2026-07-08)
The tail marker anchors the growing body incrementally but at 5m TTL, so a
turn-gap > tail TTL (overnight resume) expires it → whole-body re-bill. Because
no marker is free, P5 = RE-PURPOSE the tail marker's TTL/placement (gap-gated
1h at the immutable-prefix bound), NOT add one. Blanket 1h-tail is REJECTED by
the A/B data above (hurts fast turns B +3.3% / C +3.2%, only wins on long-gap
D −16.1%, since 1h writes cost 2.0×). Any change must benchmark net-neutral on
the fast-turn arms via `debug/test_cache_validation.py --ttl`. Ordering rule:
1h entries MUST precede 5m entries or Anthropic returns HTTP 400.

