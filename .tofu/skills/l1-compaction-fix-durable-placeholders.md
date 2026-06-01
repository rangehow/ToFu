---
name: l1-compaction-fix-durable-placeholders
description: L1 micro_compact fixed (2026-05-12): now loads conv from DB, builds full _round_index, mutates toolContent for durability, persists via CAS UPDATE
enabled: true
tags: [compaction, L1, fix, verified]
created: 2026-05-12T07:39:22Z
updated: 2026-05-12T07:39:22Z
---

# L1 micro_compact — durability fix (verified 2026-05-12)

## What was broken
`task['toolRounds']` only holds CURRENT-turn rounds (initialized `[]` per
task in `manager.py:42` and reset per endpoint turn at
`orchestrator.py:1485`). The original `_round_index` was built solely
from `task['toolRounds']`, so cold-round tool_call_ids never matched and
`_stamp_l1` silently no-op'd on every L1 invocation.

Compounding bug: even when `messages[idx]['content']` was rewritten to a
placeholder, the next round's `build_api_messages_from_db` rebuilt the
api list from `r['toolContent']` (the original 33k-char source-of-truth
in the DB conv-form). The placeholder was thrown away every turn.

Live evidence: on `mp0sggcln5pruo`, logs reported `cold=106 compacted=39
~65324 tokens saved` but DB had `compactionLayer=null` on all 275
toolRounds and `build_api_messages_from_db` produced 549,442 chars of
un-compacted tool results.

## What's fixed (`lib/tasks_pkg/compaction.py`)
1. **`_round_index` now spans both** `task['toolRounds']` (current-turn,
   priority) AND `conv.messages[i].toolRounds` (cold rounds from prior
   assistant messages). The conv messages are loaded from the DB once
   per `micro_compact` call; the load is cheap (single SELECT).
2. **`_stamp_l1` mutates `round_entry['toolContent']`** to the
   placeholder string so the next `_reconstruct_tool_call_messages`
   rebuild produces compacted bytes. The api-form `messages[idx]['content']`
   mutation that was already there is now reinforced by the
   source-of-truth mutation.
3. **Persistence at the end of `micro_compact`**: when `_conv_dirty`
   (set only when a conv-owned round was mutated, via
   `_conv_owned_ids`), we write the full `messages[]` array back to
   the DB with a CAS guard on `updated_at`. CAS-skip is logged but
   not retried — the next round's `micro_compact` will redo the work.
4. New log line `[L1-persist] conv=X wrote durable placeholders to N
   toolRounds` on success, `[L1-persist] CAS skipped` on conflict.

## Verification (real conv mp0sggcln5pruo)
```
BEFORE: 275 toolRounds, 0 with compactionLayer, 549,442 chars
AFTER : 275 toolRounds, 61 stamped L1, 205,981 chars (62% reduction)
        80 big rounds → 19 (the 19 are in the hot tail, correctly skipped)
```

## What we didn't change
- Cache-prefix-skip logic (`idx < _cache_prefix_count: skipped_already`)
  stays — opportunistic compaction on cache cold is correct policy.
- Hot-tail (60) and threshold (2000) constants unchanged.
- L0 (tool_dispatch budget) unchanged.
- Frontend `tool_compacted` SSE handler / `_syncToolRoundsDOM` slot
  rebuild branch already in place from earlier fixes — they now actually
  receive events because `_stamp_l1` no longer no-ops.

## Test harness
`debug/test_l1_compact_cache_tradeoff.py` — clones a real conv into a
test row, replays N rounds via `dispatch_stream` so `add_cache_breakpoints`
fires (note: `dispatch_chat`/`chat()` in `lib/llm_client.py` does NOT
call `add_cache_breakpoints` — only `stream_chat` does).

## Cache cost trade-off (the secondary question)
The aggressive-vs-status-quo A/B with naive cache_tracking state setup
is dominated by R1-R2 cache cold-start luck. To get a clean answer
needs proper per-arm cache_tracking simulation. Per
`cache-tofu-vs-cc-comparison-results.md`, our 4-BP mixed-TTL strategy
already beats CC's 1-BP by 10-27% in controlled tests; the cache-prefix
skip is a small additional optimization on top.

## Files touched
- `lib/tasks_pkg/compaction.py:1335-1416` — new `_round_index`, conv
  load, `_conv_dirty` tracking, `_stamp_l1` toolContent mutation
- `lib/tasks_pkg/compaction.py:1894-1934` — new persistence block at
  function end with CAS guard
- `debug/test_l1_compact_cache_tradeoff.py` — A/B harness (not
  load-bearing, kept for future investigation)

