---
name: compaction-viewer-architecture
description: Compaction Viewer: inline chips + right-side drawer that surface transcript_archive snapshots so users can inspect pre-compaction context
enabled: true
tags: [ui, compaction, drawer, transcript_archive]
created: 2026-05-06T06:01:19Z
updated: 2026-05-06T06:01:19Z
---

# Compaction Viewer (2026-05-06)

Surfaces the pre-compaction context so users can see exactly what hit the LLM
right before `force_compact_if_needed` / `reactive_compact` rewrote the
message list.

## Architecture

### DB
`transcript_archive` now carries metadata columns:
`trigger` (force | reactive | manual), `task_id`, `round_num`, `model`,
`tokens_before`, `tokens_after`, `msgs_before`, `msgs_after`, `reason`.
Migrations added to BOTH `_schema_sqlite.py` and `_schema_pg.py` (§10.3),
bumped to SCHEMA_VERSION 16.

### Backend
- `lib/tasks_pkg/compaction.py::_archive_transcript()` — now accepts
  trigger/task/round/tokens/msgs/reason and emits a `compaction` SSE event
  on `task['events']` via `manager.append_event`. Returns the inserted
  row id for later UPDATE.
- `reactive_compact` takes an EARLY snapshot before Phase 0 image-strip,
  passes `_compaction_skip_archive=True` into `force_compact_if_needed`
  so we don't double-archive the same 413.
- `execute_compact_tool` UPDATEs the row with `tokens_after` + summary
  AFTER the LLM summary runs, then emits a `compaction_done` event.

### Routes (routes/conversations.py)
- `GET /api/conversations/<id>/compactions` — lazy metadata only
  (payload_size reported, messages_json never serialized here).
- `GET /api/conversations/<id>/compactions/<archive_id>` — full payload.
- Both gracefully fall back to legacy column set if migrations haven't run.

### Frontend (static/js/compaction-viewer.js)
- Public API: `window.openCompactionViewer(convId, archiveId?)`
  `window.closeCompactionViewer()`, `window.attachCompactionMarkersToConversation(convId, messages)`
- Drawer (right-side, NOT modal — main chat stays readable).
- Three tabs: Messages · Summary · History.
- Messages rendered raw (no markdown) so the user sees exact
  whitespace/tool args/tool output. `image_url` blocks stay COLLAPSED
  behind a "reveal" button (base64 can be multi-MB).
- Cache per-session by `(convId, archiveId)` — cleared on conv switch.

### UI integration
- `ui.js` SSE handler branch: `ev.type === 'compaction'` appends a marker
  to `assistantMsg._compactions[]`; `compaction_done` upgrades the matching
  marker with final numbers.
- Inline rendering: `_compactions[]` → `.compaction-marker-row` chips with
  CTA button, progress pulse during `in_progress` state.
- `core.js::loadConversationMessages` calls `attachCompactionMarkersToConversation`
  from BOTH cache-load and server-load paths → re-hydrates markers by
  matching `archive.task_id → message._taskId` (fallback: last assistant).

## Design decisions (do NOT regress)

1. **Drawer not modal** — main chat readability preserved.
2. **Two SSE events, not one** — `compaction` when archive row is written
   (frontend can show in-progress spinner immediately); `compaction_done`
   after LLM summary completes. Payload size is optional so we don't
   block the marker on the expensive summarization.
3. **Lazy payload fetch** — list endpoint never includes `messages_json`;
   only on-demand via `/compactions/<id>`.
4. **Deduplicated reactive archive** — reactive_compact archives BEFORE
   Phase 0 (captures raw pre-failure state) and force_compact called
   nested inside skips its own archive via `_compaction_skip_archive=True`.
5. **Caveat in drawer header** — users are told this is the
   post-L1/L2 state, not raw user input. The conversation DB is the
   source of truth for raw user text.

