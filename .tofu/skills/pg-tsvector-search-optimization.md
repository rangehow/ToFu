---
name: pg-tsvector-search-optimization
description: Conversation search optimization: tsvector GIN index (0-5ms) + ILIKE fallback on left(search_text, 10000) for substring matches
enabled: true
tags: [postgresql, performance, search]
created: 2026-04-03T03:44:50Z
updated: 2026-04-03T03:44:50Z
---

# PostgreSQL Conversation Search Optimization

## Problem
`search_text ILIKE '%query%'` on conversations table is slow (250ms+) because:
- `search_text` column values are huge (avg 48KB, max 827KB) → stored in TOAST
- ILIKE recheck after GIN trigram index scan decompresses every TOAST value
- Even the trigram index can't help — TOAST decompression dominates

## Solution: Two-Phase Search
1. **Phase 1: `search_tsv @@ to_tsquery('simple', query || ':*')`** — stored tsvector column with GIN index, ~0-5ms
2. **Phase 2 (fallback): `left(search_text, 10000) ILIKE '%query%'`** — only runs if Phase 1 returns <50 results, ~120ms with truncation avoiding full TOAST decompress

## Key Architecture
- `search_text TEXT` — full plaintext of all messages (for snippet extraction & ILIKE fallback)
- `search_tsv TSVECTOR` — stored tsvector using `'simple'` config (works for English + Chinese)
- Both maintained on every write path via `to_tsvector('simple', left(search_text, 50000))`
- Schema version bump triggers `_backfill_search_tsv()` migration

## Performance Results
| Query type | Old | New | Speedup |
|---|---|---|---|
| Common (50+ hits) | 250ms | 1-7ms | 40-250x |
| Rare (few hits) | 250ms | ~125ms | 2x |

## Write Paths (all must update `search_tsv`)
- `routes/conversations.py` — save_conv
- `lib/tasks_pkg/manager.py` — _sync_result_to_conversation, _sync_partial_to_conversation
- `lib/tasks_pkg/endpoint.py` — endpoint sync
- `lib/scheduler/proactive.py` — proactive agent
- `lib/scheduler/timer.py` — timer watcher
- `lib/feishu/conversation.py` — Feishu sync

## SQL Pattern for Updates
```sql
UPDATE conversations SET ..., search_text=?,
    search_tsv=to_tsvector('simple', left(?, 50000))
WHERE id=?
```

