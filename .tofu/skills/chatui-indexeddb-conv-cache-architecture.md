---
name: chatui-indexeddb-conv-cache-architecture
description: IndexedDB cache: split-store v2 (conv_meta + messages keyed by [convId,_msgId]), diff writes via FNV-1a content hash, paginated read API, compat get() wrapper, eviction + quota monitoring
enabled: true
tags: [javascript, indexeddb, cache, architecture, performance, conversations]
created: 2026-03-30T05:31:20Z
updated: 2026-05-29T00:59:10Z
---

# IndexedDB Conversation Cache Architecture

## Design Principles
- **Server (PostgreSQL) is always the single source of truth**
- IndexedDB is a **read-through local cache** — never authoritative
- On click: render from cache instantly, background-verify freshness
- On mutation: write-through to cache after server sync succeeds
- On failure: graceful fallback to server fetch (cache is optional)
- No sync conflicts possible — worst case is stale cache, auto-corrects
- **NEVER write to cache during streaming** — server checkpoints to PG every 5s, always fresher

## Critical: Why No Cache Writes During Streaming

```python
# lib/tasks_pkg/manager.py
_STREAM_CHECKPOINT_INTERVAL = 5  # seconds
```
Writing to IndexedDB during streaming would create entries that are ALWAYS staler than the server.
On page refresh, `initActiveTasks` reconnects via SSE and the server serves the latest checkpoint.

## Schema v2 — split stores + diff writes (2026-05-29)

DB: `tofu_conv_cache`, version 2.  Two object stores:

- **`conv_meta`** — keyed by `id`.  Holds:
  `{ id, title, updatedAt, cachedAt, settings, msgOrder, msgCount }`.
  `msgOrder` is `[{id, h}, ...]` — ordered list of message keys plus
  FNV-1a content hash for diff detection.  Indexed by `cachedAt` for
  LRU eviction.
- **`messages`** — composite key `[convId, msgId]`.  One row per message:
  `{ convId, msgId, data }`.  Cascade-deleted via
  `IDBKeyRange.bound([convId, ''], [convId, '\uffff'])`.

### Migration
v1 → v2 in `onupgradeneeded`: drops the old monolithic `conversations`
store.  Server re-fills on next click — same one-shot strategy used for
the legacy `chatui_conv_cache` DB cleanup.  No data loss because PG is
authoritative.

### Diff write algorithm (`put(conv)`)
1. Build `newOrder` = `[{id: _msgKey(m,i), h: hash32(stripped)}, ...]`.
   `_msgKey` prefers `msg._msgId` (server uuid or client `tmp_<uuid>`),
   falls back to `_idx_<i>` defensively.
2. Read existing meta row to get `oldOrder`.
3. Single rw tx across both stores:
   - `delete([convId, oldId])` for ids missing from new.
   - `put({convId, msgId, data})` for new ids OR ids whose hash changed.
   - Always rewrite the meta row (cheap, bumps cachedAt for LRU).
4. Log shows `(+added ~changed -deleted)` for diagnostic visibility.

### Why `_msgId` makes this correct
Per `bilateral-msgid-and-data-msg-id` memory + Phase 2 of sync-foundation:
- Server backfills UUID via `_assign_message_ids` on every JSONB write.
- Client stamps `tmp_<uuid>` via `_ensureMsgId` before any push.
- IDs survive splice / pop / regenerate / branch-truncate — index shifts
  no longer corrupt the diff.

## API Surface

| Method | Returns | Use |
|---|---|---|
| `get(id)` | full `{...meta, messages: [...]}` (compat) | Existing 32 callers, unchanged |
| `getMeta(id)` | metadata only | Cheap freshness check |
| `getMessages(id, {beforeIdx, afterIdx, limit})` | windowed message array | Future paginated UI |
| `put(conv)` | void | Diff write |
| `remove(id)` | void | Cascade delete (meta + range delete messages) |
| `evict()` | count | LRU drop with cascade delete |
| `clear()` | void | Wipe both stores |
| `stats()` | `{count, messageCount, available}` | Settings UI |
| `isAvailable()` | bool | Feature gate |

## Eviction & Quota
- `EVICT_EVERY_N_PUTS = 20` — every 20th successful `put()` triggers
  background `evict()` + `_maybeCheckQuota()`.
- `_requestPersistentStorage()` at startup — `navigator.storage.persisted()`
  → `persist()`.  Logged at info, never thrown.
- `_maybeCheckQuota()` — throttled to 5 min via `QUOTA_CHECK_INTERVAL_MS`.
  If `estimate().remaining < 50 MB`, fires extra `evict()`.
- Legacy DB `chatui_conv_cache` deleted ONCE, guarded by
  `localStorage['tofu_conv_cache_legacy_cleanup_v1']`.

## Cache Write Points (ALL from server-confirmed data)

| File | Trigger | Purpose |
|---|---|---|
| `core/conversations.js` syncConversationToServer | After successful PUT | Write-through |
| `core/conversations.js` loadConversationsFromServer | Prefetched active conv | Cache server data on boot |
| `core/conversations.js` Phase 2 | After fresh server fetch | Update with server checkpoint |
| `ui/streaming_render.js` finishStream | Stream completed | Final post-stream data |

## Cache Read/Remove Points

| File | Operation | Purpose |
|---|---|---|
| `core/conversations.js` Phase 1 | `get()` | Instant render |
| `core/cross_tab_sync.js` `conv_deleted` | `remove()` | Cross-tab invalidation |
| `core/conversations.js` 404 | `remove()` | Clean ghost conv |
| `main/main_conv_lifecycle.js` deleteConversation | `remove()` | Remove deleted conv |

## Phase 2 Freshness Check (Critical)

```javascript
} else if (!hasLocalData || cacheHit || (!isStreaming && !conv.activeTaskId)) {
```
**Without `cacheHit`**, when conv.activeTaskId is set and data came from cache,
Phase 2 silently skipped server data → user stuck with stale pre-stream cache.

## Fetch Timeout
- With cache hit: 10s (background check)
- Without cache hit: 15s (user is waiting)

## Bug Fixes Applied
1. **Fetch timeout** — server freeze no longer causes infinite hang
2. **Cache data re-upload guard** — `!cacheHit` prevents stale cache from overwriting server
3. **Math.max stack overflow** — `.reduce()` instead of `Math.max(...spread)`
4. **Phase 2 always wins for cache data** — `cacheHit` added to freshness condition
5. **No streaming cache writes** — removed all periodic/emergency cache writes during SSE/poll
6. **Legacy DB delete spam** (2026-05-28) — guarded by localStorage flag, runs once
7. **Schema v2 split-store** (2026-05-29) — diff writes via `_msgId`, cascade-delete via composite key range, paginated read API

## Console Utilities
- `clearConvCache()` — clear all cached conversations
- `convCacheStats()` — show cache count and availability

## Settings UI
- Settings > Advanced > Local Cache section
- Shows count of cached conversations
- "Clear Cache" button with toast feedback + `_needsLoad` reset

