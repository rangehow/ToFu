---
name: pg-tsvector-search-optimization
description: Conversation search PG: Phase-1 tsvector tsquery (Bitmap, ~15-35ms) + Phase-2 fallback now uses idx_conv_search_head_trgm expression GIN index on lower(left(search_text,10000)) — 1218ms→101ms common, <1ms rare.
enabled: true
tags: [postgresql, performance, search]
created: 2026-04-03T03:44:50Z
updated: 2026-06-15T14:54:15Z
---

# PostgreSQL Conversation Search Optimization

## Problem
`search_text ILIKE '%query%'` (and `lower(search_text) LIKE`) on conversations is a full Seq Scan (~790ms on 2.9k rows, avg 45KB/max 11.7MB search_text → TOAST decompress). The trgm index `idx_conv_search_trgm` is built on the RAW column, so the `lower(...)` wrapper defeats it.

## Solution: Two-Phase Search (routes/conversations_search.py::search_convs)
1. **Phase 1 (index-backed)** — branches by `_BACKEND`:
   - PG: `search_tsv @@ to_tsquery('simple', 'w1:* & w2:*')` → `Bitmap Index Scan on idx_conv_search_tsv`, ~15-35ms.
   - SQLite: `conversations_fts MATCH 'w1* w2*'` FTS5.
2. **Phase 2 (fallback)** — `lower(left(search_text, 10000)) LIKE '%query%'`, only if Phase-1 < 50 results. `left(...)` cap avoids decompressing megabyte TOAST values. Catches mid-word substrings tsvector misses.

## Phase-2 fallback was STILL slow (1218ms) — fixed 2026-06-15
EXPLAIN ANALYZE showed Phase-2 = full Seq Scan detoasting every row: common term `report` 1218ms, rare term `kubernetes`/`xqzk` ~750ms. Root cause: the `lower(left(search_text,10000))` wrappers don't match `idx_conv_search_trgm` (built on the RAW column), and raw-column `ILIKE` is ALSO a trap (lossy trgm recheck detoasts 1330 blobs → 815ms; 2-char terms can't use trgm at all).
**Fix**: expression trgm index matching the predicate EXACTLY, in `lib/database/_schema_pg.py`:
```sql
CREATE INDEX IF NOT EXISTS idx_conv_search_head_trgm ON conversations
  USING gin (lower(left(search_text, 10000)) gin_trgm_ops)
```
Result: `report` 1218ms→101ms, `kubernetes` 744ms→0.88ms, `xqzk` 761ms→0.02ms. Plan = `Bitmap Index Scan on idx_conv_search_head_trgm`. NO query change needed — the index slots under the existing SQL. CRITICAL: the `10000` cap in the index expression MUST stay in sync with the `left(search_text, 10000)` in conversations_search.py or the planner won't use it.

## History / gotcha
The `search_tsv` column + `idx_conv_search_tsv` GIN + write-path maintenance ALREADY existed, but the READ path had `if _fts_query and _BACKEND != 'pg'` — PG had NO Phase-1 and fell straight to Seq Scan. Fix (2026-06-13) added the PG tsquery branch. Fix (2026-06-15) added the head-trgm index for Phase-2.

## Observability
`_log_search_timing(query, n, elapsed)` logs **WARNING** when `elapsed >= _SLOW_SEARCH_THRESHOLD_S` (0.3s), else DEBUG. Grep token `[search_convs] SLOW`.

## search_tsv column (TSVECTOR, 'simple' config)
Maintained on every write path via `to_tsvector('simple', left(search_text, 50000))`:
routes/conversations.py save_conv; lib/tasks_pkg/manager.py; lib/tasks_pkg/endpoint.py; lib/scheduler/proactive.py; lib/scheduler/timer.py; lib/feishu/conversation.py.

## Tests
tests/test_conversation_search.py (34 pass on live PG) — `test_search_pg_phase1_uses_tsvector_index` (Bitmap Index Scan via EXPLAIN with `SET LOCAL enable_seqscan=off`; DictRow → use `r[0]`), `test_search_pg_fallback_uses_head_trgm_index` (NEW — pins idx_conv_search_head_trgm), `test_slow_search_threshold_log`.

