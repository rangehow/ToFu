---
name: anthropic-cache-key-mechanics
description: VERIFIED: Anthropic cache key = prompt prefix bytes, no contention between conversations (A/B tested), alternating HIT/MISS explained
enabled: true
tags: [cache, anthropic, architecture, verified, a/b-test]
created: 2026-04-06T08:22:51Z
updated: 2026-04-10T08:31:19Z
---

# Anthropic Cache Key Mechanics (Verified 2026-04-06, updated 2026-04-10)

## Key Finding

**The Anthropic prompt cache key is based solely on the prompt prefix bytes.**

### What does NOT affect the cache key:
1. `anthropic-beta` request header
2. `cache_control.ttl` value (`1h` vs `5m` hit same cache)
3. Tool result ordering
4. Beta feature flags
5. **Other concurrent conversations** — cache is NOT shared/contended between conversations

### What DOES break the cache:
1. **Any change to message content** in the cached prefix
2. Different system prompt text
3. Different tool definitions
4. **In-place content modification** of ANY historical message

## ★ CRITICAL: Cache Contention Does NOT Exist (A/B Tested 2026-04-10)

Ran controlled experiment: Conv A solo for 6 rounds vs Conv A + Conv B interleaved.
**Per-round cache_read is identical** (±0.0%) between solo and interleaved modes.

- R3: Solo=4,290 vs Intlv=4,292 (+0.0%)
- R5: Solo=4,458 vs Intlv=4,461 (+0.1%)

Interleaving can actually HELP: Conv B kept the shared system+tools prefix warm,
giving Conv A a cache hit in R6 where solo had a miss.

**Root cause**: Cache key = exact prefix bytes. Different conversations = different
prefixes (after system+tools) = different cache keys = no interference possible.

## ★ The "Cache Prefix" Is Bigger Than You Think

`get_cache_prefix_count()` returns `message_count - 2`, suggesting only
old messages are cached. But Anthropic caches everything up to **BP4**
(the last message with content), which is virtually ALL messages.

This means **any mutation to any historical message** causes a full
cache miss. A/B tested: compacting cold assistant messages (Phase D)
caused +57% cost increase due to repeated full re-caches.

## ★ Overlapping Breakpoints Do NOT Cause Redundant Writes (Verified 2026-04-08)

When two breakpoints cache overlapping prefixes, the API reports
`cache_creation_input_tokens` as the **total unique tokens** written,
NOT per-breakpoint. No redundancy from multiple breakpoints.

## ★ Alternating HIT/MISS Pattern (Small Prompts)

When system+tools < 4096 tokens (Opus minimum), only BP4 provides cacheable
segments. BP4 moves each round:
- Round N: BP4 at position X → cache WRITE at X
- Round N+1: BP4 at position X+2 → no cache entry at X+2 → WRITE (miss)
- Round N+2: BP4 at position X+4 → no cache entry at X+4 → WRITE (miss)

But round N+1 DOES get a hit from X's cache for the prefix portion.
In production with ~14K system+tools (>> 4096), BP1-BP3 provide stable
baseline hits even when BP4 moves.

## Safe vs Unsafe Operations

| Operation | Cache Impact |
|-----------|-------------|
| Append new messages | ✅ Safe (extends prefix) |
| Modify system prompt | ✅ Safe (separate BP1-BP2) |
| Modify tool definitions | ✅ Safe (separate BP3) |
| micro_compact (skip cached) | ✅ Safe (respects prefix count) |
| Multiple BPs on sub-prefixes | ✅ Safe (no redundant writes) |
| Multiple concurrent conversations | ✅ Safe (no contention) |
| Compact assistant content | ❌ KILLS CACHE (+57%) |
| Reorder messages | ❌ KILLS CACHE |
| Any in-place edit | ❌ KILLS CACHE |

## A/B Testing Rules
- Arms must use unique system prompt suffixes (arm seed) — **at the FRONT of content**
- Opus needs ≥4096 token minimum cacheable segment
- Claude tokenizer ≈ 1.44x tiktoken cl100k_base

