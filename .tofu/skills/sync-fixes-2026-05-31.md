---
name: sync-fixes-2026-05-31
description: Backend+frontend sync hardening: cold-replay state synth, INSERT OR IGNORE collision detection, FTS CAS guard, content regression preserve-longer, push.js jitter
enabled: true
tags: [sync, sse, websocket, event-log, cas, fix]
created: 2026-05-31T03:05:23Z
updated: 2026-05-31T03:05:23Z
---

# Sync mechanism hardening — 2026-05-31

Followup to `sync-foundation-2026-05-09`. After a 3-agent review (backend +
frontend + protocol), we shipped fixes for the **localized correctness
bugs** while leaving the architectural ones (id-based regen/branch from
Phase 3, multi-tab CAS, SSE+push fanout dedup) for a separate PR.

## Backend

### `lib/tasks_pkg/event_log.py`
- **Refuse `event_id=None`** with a WARNING — the legacy fallback in
  `manager.append_event` could pass `None`, and `int(None)` either crashed
  or inserted NULL. Cold replay would have a hole that looked (to the
  user) like data loss.
- **Detect `INSERT OR IGNORE` collisions**: now check `cur.rowcount` and
  WARN. Distinguishing real-retry from real-duplicate is impossible at
  the row level, so the WARNING is the canary for the latter.
- **Promote persist failures from DEBUG → WARNING**: a silent persist
  failure means cold replay returns nothing for that window — the user
  perceives it as data loss.
- **Docstring honesty**: rewrote header to say there is no coalescing
  (was lying about 250 ms delta merge that was removed in 2026-05).
  `flush_pending` is kept as no-op for API compat.

### `lib/tasks_pkg/manager.py`
- **`append_event` legacy fallback**: now mints a real `seq` from
  `len(task['events'])` BEFORE falling through to `append_persistent_event`
  so cold-replay rows aren't dropped. The seq is set on `event['seq']`
  inside `events_lock` already.
- **`_sync_result_to_conversation` CAS**: replaced the re-SELECT-based CAS
  check (TOCTOU window where a third writer falsely reported CAS-miss)
  with `cur.rowcount` directly. Switched from `db_execute_with_retry` to
  raw `db.execute() + db.commit()` so rowcount survives.
- **FTS guard on CAS**: terminal-sync FTS update now gates on
  `_cas_succeeded`. Without this, a losing CAS write still rewrote the
  FTS row, leaving search hits pointing at content `messages` never
  accepted.
- **Same FTS guard added to `_sync_partial_to_conversation`** (was
  missing — the FTS race was the deferred half of the original CAS fix).
- **Drop `hasattr(row, 'get')` dead branch**: `sqlite3.Row` has no
  `.get()`, so the conditional always took the `row[1]` path. Use named
  column access `row['updated_at']`.

### `routes/chat.py`
- **`_extract_task_meta` field list**: added `provider_id`, `apiRounds`,
  `modifiedFiles`, `modifiedFileList`. Previously the in-memory done
  event dropped these fields while the DB-poll path included them →
  inconsistent UI for the same task depending on which path won the
  race. Docstring now lists the four sites that MUST stay in sync.
- **Cold-replay synth-done loop**: same field list extension.
- **Cold-replay state emission**: when `task_events` row is missing (TTL
  prune or task evicted), now also emit a synthetic `state` event from
  `task_results` BEFORE the synth-done. Without this, a Last-Event-ID
  reconnect after server restart got only metadata and lost ALL text —
  pure data loss. Mirrors the warm-fallback shape.
- **Warm-fallback DB-path field loop**: extended to match.
- **`chat_poll` in-memory loop**: added `provider_id` for symmetry with
  the DB path (was silently dropped on round-trip even though
  `persist_task_result` writes it into `meta_json`).
- **`chat_poll` DB-path loop**: added `provider_id`.
- **`_persist_conv_messages`**: now backfills `_msgId` via
  `_assign_message_ids(messages)` before writing. This is the central
  writer for chat send/regen/edit/continue, and it was the missing site
  from the 2026-05-09 Phase-2 work (only 5 sites had it; this is #6).

## Frontend

### `static/js/push.js`
- **Full jitter**: `Math.random() * baseDelay` instead of `baseDelay`.
  Prevents herd reconnect on server bounce when many tabs are open.
- **Min-uptime gate**: `_reconnectAttempt` reset only when connection
  held ≥ `MIN_UPTIME_MS` (5 s). Previously `onopen` reset the counter
  unconditionally, so an open-then-immediate-close flap kept resetting
  to 0 and reconnect-spammed.
- **Permanent close codes**: stop reconnecting on 1008 (policy) / 1011
  (internal error during open). Server saying "no" → respect it.

### `static/js/ui/sse_pipeline.js`
- **Content-regression detector now actually preserves**: was logging
  `console.error` then unconditionally overwriting with the shorter
  content (the warning was a lie). Now keeps whichever side is longer.
  Same fix for thinking. The race is real — server's `task['content']`
  mutation under `content_lock` can lag delta-applied client state — so
  trusting the longer side is correct.
- **`_continueApiRounds` cleanup in poll-done branch**: was orphaned;
  only SSE done path cleared it. Now both paths end in a consistent
  carrier state.

## Skipped (out of scope for this PR)

- M1: `_sync_result_to_conversation` lacks CAS retry. Rejected: the
  existing benign-skip log is correct semantics — task_results carries
  canonical data and the winner already had the SSE done event merged.
- D2/D3/D4: Phase-3/4 frontend cutover (id-based regen/branch, remove
  full-array PUT). Coordinated FE+BE work.
- D11: SSE+push dual-fanout consolidation. Architectural redesign.

## Verification

- `python -m py_compile` on all touched .py files: OK.
- `ruff check`: clean.
- JS syntax-checked via `new Function(src)`: clean.
- Full test run blocked by pre-existing flask/jinja2 env breakage in
  the conda env — NOT a regression from these changes.

## Files

- `lib/tasks_pkg/event_log.py`
- `lib/tasks_pkg/manager.py`
- `routes/chat.py`
- `static/js/push.js`
- `static/js/ui/sse_pipeline.js`

