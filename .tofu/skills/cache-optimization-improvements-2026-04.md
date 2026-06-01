---
name: cache-optimization-improvements-2026-04
description: Cache optimization round 2+3: 7 features implemented, all A/B tested — Phase D assistant compaction HARMS cache (+57% cost), must be opt-in only for emergency compaction
enabled: true
tags: [cache, anthropic, optimization, cost, prompt-caching, a/b-test]
created: 2026-04-06T07:50:57Z
updated: 2026-04-06T09:27:55Z
---

# Cache Optimization Improvements (2026-04-06)

## Live A/B Test Results Summary

### Round 2 Tests (aws.claude-opus-4.6, 6 rounds each)

#### TTL Latch: NO measurable benefit ❌
| Arm | Cost | Cache Savings % |
|-----|------|----------------|
| NO_LATCH | $0.3392 | 54.3% |
| WITH_LATCH | $0.3594 | 49.5% |

- Cache key = prompt prefix bytes only, NOT headers or TTL metadata
- **Verdict**: Keep code, but don't expect savings

#### Tool Result Ordering: NO effect on Claude ❌
- Identical cache_read/cache_write between arms
- **Verdict**: Keep for OpenAI/Qwen, neutral on Claude

### Round 3 Test: Phase D Assistant Compaction (aws.claude-opus-4.6, 10 rounds)

#### Phase D during normal rounds: HARMFUL ❌❌❌
| Arm | Cost | Cache Writes |
|-----|------|-------------|
| BASELINE (no Phase D) | $0.72 | 18,603 |
| PHASE_D (enabled) | $1.13 | 43,472 |

- **+57% cost increase** — Phase D is DESTRUCTIVE during normal rounds
- Root cause: Compacting assistant messages changes prefix bytes →
  full cache invalidation → expensive re-cache at 1.25x per round
- Pattern: R7-R10 showed `cache_read=0` (complete miss) every other round
  while baseline maintained continuous cache hits

#### Phase D disabled by default: CONFIRMED CORRECT ✅
Second run with Phase D disabled showed identical metrics between arms,
confirming the fix. Phase D now only fires during `reactive_compact`
(emergency, cache already being rebuilt).

## Critical Finding: ANY Message Mutation Kills Cache

**The #1 rule of prompt cache optimization:**
> Never mutate any message that is already in the cached prefix.

`get_cache_prefix_count()` returns `message_count - 2`, but Anthropic
actually caches everything up to BP4 (the conversation tail). This means
virtually ALL messages are in the cached prefix. Any byte change to
ANY historical message causes a full cache miss.

**Safe mutations** (don't change prefix bytes):
- Appending new messages at the end ✅
- Changing tool definitions ✅ (separate BP3)
- Changing system prompt ✅ (separate BP1-BP2)

**Unsafe mutations** (kill cache):
- Replacing cold tool result content ❌ (micro_compact is safe only
  because it skips cache prefix messages)
- Compacting assistant response text ❌ (Phase D!)
- Reordering messages ❌
- Any in-place content modification ❌

## Implementation Status (All Kept)

| # | Feature | Status | A/B Result | Notes |
|---|---------|--------|------------|-------|
| 1 | TTL detection bug fix | ✅ Active | Diagnostic | High diagnostic value |
| 2 | Concurrent conv tracking | ✅ Active | Diagnostic | Detects contention |
| 3 | Per-round cache stats INFO | ✅ Active | Diagnostic | Production visibility |
| 4 | TTL latch | ✅ Active | Neutral | Low value, no harm |
| 5 | Tool result ordering | ✅ Active | Neutral on Claude | For OpenAI/Qwen |
| 6 | Memory cleanup | ✅ Active | Required | Prevents leaks |
| 7 | Phase D assistant compact | ⚠️ Opt-in only | **HARMFUL** if always-on | Only in reactive_compact |

## Architecture

- Phase D flag: `micro_compact(messages, enable_assistant_compact=True)`
- Only enabled in: `reactive_compact()` (emergency compaction)
- NEVER enabled in: `run_compaction_pipeline()` (normal rounds)
- 9 unit tests in `TestAssistantContentCompaction`
- A/B test: `debug/test_phase_d_ab.py`

