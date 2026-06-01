---
name: sync-foundation-2026-05-09
description: Persisted SSE events (no coalescing) + stable _msgId + by-id PATCH + comprehensive checkpoint — Phase 0/1/2 sync foundation
enabled: true
tags: [sync, sse, schema, messages, translate]
created: 2026-05-09T06:20:11Z
updated: 2026-05-22T03:33:38Z
---

# Sync Foundation (Phases 0/1/2) — 2026-05-09

User asked for the "longest-term, most robust iteration option" against
recurring user-data loss after SSE drops. We landed the foundation in one
PR; Phases 3-5 must follow as separate PRs (require coordinated frontend
cutover + JSONB→rows backfill that can't be done blindly in one shot).

## What shipped

### Phase 0 — Persisted SSE event log
- New table `task_events(task_id, event_id, ts_ms, type, payload)`,
  PK `(task_id, event_id)`, on **both** PG and SQLite (schema version 17).
- New module `lib/tasks_pkg/event_log.py`:
  - `append_persistent_event(task_id, event_id, event)` — mirrors every
    SSE event into the table.
  - **Delta coalescing**: consecutive content/thinking deltas within a
    250 ms window merge into one row, stored at the **last** event_id
    (so reconnects mid-coalesce don't lose content — they may
    re-receive a chunk they already saw, never the opposite).
  - `flush_pending(task_id)` — forced flush on `done` events.
  - `read_events(task_id, since_event_id=None)` — Last-Event-ID replay.
  - `has_terminal_event(task_id)`, opportunistic TTL prune (6h).
- `manager.append_event` now also calls `append_persistent_event`.
- `routes/chat.py::chat_stream` cold-path: when task is gone AND the
  client provides Last-Event-ID, replay from `task_events` table
  instead of synthesising a `state`+`done` snapshot. Survives
  `cleanup_old_tasks` and server restart.

### Phase 1 — Comprehensive checkpoint
- `_sync_partial_to_conversation` (manager.py) rewritten:
  - Bounded CAS retry (3 attempts) instead of single-shot guarded write.
  - Writes the full structural payload: toolRounds, model, provider_id,
    preset, modifiedFiles, modifiedFileList, apiRounds, _emitContent,
    _emitToolName, _memoryPrefetch, _gitSha — backend-authoritative
    (only fills if frontend hasn't).
  - Skips terminal-only fields (finishReason / usage / toolSummary).
- Result of: page reload mid-stream now reconstructs the same UI the
  user saw before the disconnect, even without poll fallback.

### Phase 2 — Stable per-message IDs
- `_assign_message_ids(messages)` and `find_message_by_id(messages, id)`
  in `lib/tasks_pkg/manager.py`. Idempotent UUID backfill.
- Called from every JSONB write site: `routes/conversations.save_conv`,
  `routes/conversations.patch_message`, `_sync_partial_to_conversation`,
  `_sync_result_to_conversation`.
- New endpoint: `PATCH /api/conversations/<cid>/messages/by-id/<msg_id>`.
  Same whitelist + persistence flow as the index endpoint, but
  index-free. Returns 404 when the id isn't present, with `msgCount`
  so callers know how to recover.
- `routes/translate.py`:
  - `/api/translate/start` accepts `msgId` (preferred) alongside `msgIdx`.
  - `_commit_translation_inner` resolution order: msgId → msgIdx →
    content match (against `originalText`). When content match resolves
    a message that lacks `_msgId`, the caller-supplied id is backfilled.
  - Success log includes `via=msgId|msgIdx|content` for tracing.
- Frontend (minimal change, server-side compat preserved):
  - `_patchMessageOnServer` (ui.js): looks up `_msgId` from
    `conversations[].messages[idx]`, hits the by-id endpoint when
    present; falls back to the index path on 404.
  - `_startTranslateTask` (translation.js) accepts and forwards `msgId`.

### Tightened metadata merge in result sync
- `_sync_result_to_conversation` now treats backend as authoritative for
  terminal metadata (finishReason / usage / model / provider_id) once
  CAS succeeds — earlier values written by the frontend (e.g. an
  intermediate `interrupted` set during a partial sync) no longer block
  the final canonical write.
- `_assign_message_ids()` is also called here.

### SQL translation
- `_PK_MAP` extended with `'task_events': ['task_id', 'event_id']` so
  `INSERT OR IGNORE` translates to PG `ON CONFLICT (task_id, event_id) DO NOTHING`.

## Tests
- `debug/test_event_log_and_msgid.py` — 5 tests, all green:
  1. `test_event_log_replay` — coalesce semantics + since_event_id windows.
  2. `test_assign_message_ids_idempotent` — uuid + idempotency + lookup.
  3. `test_patch_message_by_id_survives_insert` — by-id PATCH works
     even after a concurrent index-shift.
  4. `test_translate_commit_resolves_by_id` — msgId wins over a stale
     msgIdx (the "msg_idx N out of range" warning fix).
  5. `test_translate_commit_falls_back_to_content` — content match still
     works as the last-resort fallback; backfills the supplied id.
- `debug/test_chatinner_endpoints.py` — 5 existing tests still pass.

## What did NOT ship (deferred — needs coordinated work)
- Phase 3: migrate edit/regenerate/branch frontend call sites to id-based.
  Currently they keep using msgIdx (still works through the legacy path).
- Phase 4: stop the frontend from PUT'ing the full messages array via
  `syncConversationToServer`. Requires a `POST /messages` endpoint for
  user-message creation and removing the cross-talk autodedup heuristic.
- Phase 5: split JSONB array → per-message rows table; collapse
  `task_results` into `messages.meta`. Requires a one-shot backfill
  migrator (must produce the same `build_search_text` output).

## Files
- `lib/tasks_pkg/event_log.py` (new)
- `lib/tasks_pkg/manager.py` (`_assign_message_ids`, `find_message_by_id`,
  `append_event` persistence wiring, `_sync_partial_to_conversation`
  rewrite, metadata merge tightening)
- `lib/database/_schema_pg.py` + `_schema_sqlite.py` (task_events DDL,
  schema version 16 → 17)
- `lib/database/_sql_translate.py` (`_PK_MAP` for task_events)
- `routes/chat.py` (cold-path SSE replay from task_events)
- `routes/conversations.py` (PATCH by-id endpoint, _msgId backfill)
- `routes/translate.py` (msgId resolution chain)
- `static/js/ui.js` (`_patchMessageOnServer` prefers by-id)
- `static/js/translation.js` (`_startTranslateTask` forwards `msgId`)
- `debug/test_event_log_and_msgid.py` (new — 5 smoke tests)
- `docs/ARCHITECTURE.md` (§3.2, §4 step 5, new §6 roadmap, scan date)

## Hot-reload caveat
Existing conversations have no `_msgId` until they're next written
(any save_conv / patch / partial sync / result sync). All read paths
tolerate missing ids. No backfill thread runs at startup — it's
opportunistic on touch.

## Forbidden anti-patterns (don't reintroduce)
- Don't address messages by index across an async boundary. Always pass
  `msgId` and resolve server-side. The translate warning the user pasted
  ("msg_idx 7 out of range (len=5)") was the canonical example.
- Don't write to `task['events']` without going through `append_event`
  — bypassing it skips the persistent log.
- Don't expose `_msgId` in `_PATCH_MSG_WHITELIST`. The id is server-set
  and immutable from the client side.
