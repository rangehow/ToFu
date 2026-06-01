---
name: anthropic-cache-breakpoints-mixed-ttl
description: Cache breakpoint strategies: 5-arm A/B test results (OLD/NEW/NEW_1h/SINGLE/SINGLE_1h), mixed TTL, prompt_tokens convention fix
enabled: true
tags: [cache, anthropic, optimization, a/b-test]
created: 2026-04-04T21:29:24Z
updated: 2026-04-04T21:29:24Z
---

# Anthropic Cache Breakpoints — Complete Strategy Guide

## 5-Arm A/B Test Results (2026-04-04, aws.claude-opus-4.6, 4 scenarios × 12 rounds)

### Overall Ranking

| Rank | Strategy | Total Cost | Wins | Savings vs Worst | Description |
|---|---|---|---|---|---|
| 🥇 | **NEW_1h** | $1.5242 | 2/4 | **27.2%** | 4 BPs, BP4=msg[-1], mixed TTL (1h+5m) |
| 🥈 | **NEW** | $1.5746 | 1/4 | 24.8% | 4 BPs, BP4=msg[-1], 5m TTL |
| 🥉 | SINGLE_1h | $1.7552 | 0/4 | 16.2% | 1 BP on msg[-1], 1h TTL |
| 4th | SINGLE | $1.9431 | 1/4 | 7.2% | 1 BP on msg[-1], 5m TTL (Claude Code style) |
| 5th | OLD | $2.0938 | 0/4 | 0% | 4 BPs, BP4=msg[-2], old buggy version |

### Per-Scenario Winners

| Scenario | Winner | Gap vs 2nd | Key Insight |
|---|---|---|---|
| A (single query multi-tool) | NEW_1h | 6.1% vs NEW | Mixed TTL helped system prefix stay cached |
| B (multi-turn + tools) | NEW | 5.9% vs NEW_1h | Low CW ratio (4%) — NEW's 1.25x write rate wins |
| C (parallel tool calls) | NEW_1h | 1.4% vs OLD | High CW ratio (48%) — 1h TTL reduced evictions |
| D (mixed content assistants) | SINGLE | 4.1% vs NEW_1h | Single BP excelled with empty-content assistants |

### Key Finding: 4 BPs > 1 BP (except Scenario D)

Despite Claude Code's rationale about Mycro KV page waste, our 4-BP strategy (NEW/NEW_1h) beat Claude Code's single-BP strategy (SINGLE/SINGLE_1h) in 3/4 scenarios. The 4-BP approach gives finer cache granularity for the system prompt and tools, which persist across rounds even when the tail changes.

SINGLE only won Scenario D (mixed content assistants) where the single breakpoint on msg[-1] happened to align better with the conversation structure.

### Recommendation: Use NEW_1h (our current default)

- **NEW_1h** is the overall winner — 3.3% cheaper than NEW, 27.2% cheaper than OLD
- The mixed TTL strategy (1h for BP1-BP3 stable content, 5m for BP4 tail) works
- Enable via `CACHE_EXTENDED_TTL = True` (default ON) in lib/__init__.py

## Implementation Details

### Mixed TTL Strategy (add_cache_breakpoints in lib/llm_client.py)
```python
if use_extended_ttl:
    _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}   # BP1-BP3: system + tools
    _cc_tail   = {'type': 'ephemeral'}                 # BP4: conversation tail (5m default)
```

### Beta Header Auto-Injection (lib/llm_client.py _stream_chat_single_attempt)
```python
if CACHE_EXTENDED_TTL:
    extra_headers['anthropic-beta'] = 'extended-cache-ttl-2025-04-11'
```

### Anthropic prompt_tokens Convention (static/js/core.js)
Anthropic API returns `prompt_tokens` as **uncached only** (NOT total).
Detection: `if (inp <= cacheWrite + cacheRead)` → Anthropic convention.
Fix: `totalInput = inp + cacheWrite + cacheRead` for display and no-cache baseline.

## Test Infrastructure

### Unit Tests: tests/test_cache_breakpoints.py (41 tests)
- TestMixedTTLStrategy: 9 tests for TTL ordering, consistency, non-Claude passthrough

### Live A/B Test: debug/test_cache_validation.py
```bash
# Full 5-arm test
python debug/test_cache_validation.py --arms OLD,NEW,NEW_1h,SINGLE,SINGLE_1h --scenario all

# Quick 2-arm comparison  
python debug/test_cache_validation.py --arms NEW,NEW_1h --scenario A --rounds 8

# Dry-run validation
python debug/test_cache_validation.py --dry-run --arms all
```

Arms: OLD, NEW, NEW_1h, SINGLE, SINGLE_1h (or "all")
Scenarios: A (multi-tool), B (multi-turn), C (parallel), D (mixed content)

## Pricing Notes

| TTL | Cache Write Rate | Cache Read Rate |
|---|---|---|
| 5 min (default) | 1.25x base input | 0.1x base input |
| 1 hour | 2.0x base input | 0.1x base input |

Models supporting 1h TTL: Claude Opus 4.5+, Sonnet 4.5, Haiku 4.5
Models NOT supporting 1h: Claude Opus 4/4.1, Sonnet 4

## Cache Activation Thresholds

| Model | Min Cacheable Tokens |
|---|---|
| Claude Opus / Haiku 4.5 | 4,096 |
| Claude Sonnet | 1,024 |

In test scenarios with ~2,500 token system prompt, cache activates at R3 (when total > 4,096).
In production with ~14K system prompt, cache activates from R1.

