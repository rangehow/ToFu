---
name: paper-report-stream-persistence-fix
description: Paper report is server-owned background task; frontend polls by task_id keyed on (paper_hash,lang); rehydrates via event replay; chat-compat tool rounds rendered via renderToolRoundsHTML
enabled: true
tags: [paper-mode, report, streaming, state-persistence, bug-fix, sse]
created: 2026-04-18T06:48:42Z
updated: 2026-05-22T03:05:46Z
---

# Paper Report — Server-Owned Background Task (2026-04-18 rewrite)

## Problem history
1. **v1 (SSE streaming, async-local state)** — tool/thinking/delta state
   lived in the `_generatePaperReport` async function. Switching tabs
   restarted generation and wiped the DOM.
2. **v2 (module-scoped stream object, client-owned)** — state survived tab
   switches but chat-mode round-trip still broke it because client state
   was detached from the backend stream; cache-miss during in-flight stream
   restarted from the beginning.

## v3 architecture — SERVER OWNS THE TASK
Per user directive 2026-04-18: generation happens **exactly once**; backend
persists the full report; frontend only shows progress.

### Backend (`routes/paper.py`)
- Task store `_report_tasks` keyed by `(paper_hash, lang)` — dedup across
  all clients/tabs/reloads.
- `_new_report_task()` initialises `{events, tool_rounds, full_text,
  enriched_text, status, abort_event, events_lock}`.
- `_run_report_task()` background worker:
  - Calls `dispatch_stream` (raw-messages path, so Opus 4.7+ works).
  - Pushes chat-compatible events to `task['events']`:
    `tool_start` / `tool_done` / `thinking` / `delta` / `enriched` /
    `done` / `error`. Every event has a monotonic `seq`.
  - On completion: INSERT OR REPLACE into `paper_reports`, emits `done`.
- `_execute_report_tool()` returns `(tool_content_str, display_results, search_diag)`:
  - `display_results`: same per-URL structured list as the chat handler
    (`_format_search_display_for_results` equivalent) — title, url, source,
    snippet, fetched, fetchedChars.
  - `search_diag`: propagated when 0 results (network_error / no_matches).
  - The `tool_done` event includes `results` and optionally `searchDiag`.
- Endpoints:
  - `POST /api/paper/report/start` — dedup; returns `{cached, report}` or
    `{task_id, paper_hash, running, existed}`.
  - `GET /api/paper/report/poll?task_id=X&cursor=N` — returns new events
    since cursor, `next_cursor`, `status`, and (when done) the final report.
  - `POST /api/paper/report/abort` — abort a specific task_id.
  - `POST /api/paper/report/lookup` — find running task by
    `(paper_hash, lang)` — used on tab re-entry.
- TTL: finished tasks purged after 1 h via `_cleanup_stale_report_tasks()`.

### Frontend (`static/js/paper-reader.js`)
- Single module-scoped `_paperReportStream = {paperId, lang, taskId,
  cursor, status, toolRounds[], fullText, thinkingText, contentStarted,
  pollTimer}`.
- `_pollReportTask()` ticks every 1.2 s; applies events via
  `_applyReportEvent()`; repaints via `_paintReportFromState()` only when
  `paperId === _activePaperId`.
- `_applyReportEvent` → `tool_done`: uses `ev.results` (structured per-URL
  display data from backend). Falls back to synthesizing a minimal results
  entry from `ev.toolContent` when `ev.results` is absent (backward compat
  with in-flight tasks started before the backend fix).
- `_paintReportFromState()` calls **`renderToolRoundsHTML(s.toolRounds,
  running)`** from `ui.js` — identical look to chat tool rendering.
- `_loadOrGenerateReport()` priority: (1) existing local poll state →
  paint+resume; (2) `/api/paper/report/lookup` by paper_hash → attach
  and poll; (3) `/api/paper/report/cache` DB hit → render instantly; (4)
  start new task via `/start`.
- `exitPaperMode` / paper switch only clears `pollTimer`; the server task
  keeps running and is re-attached on re-entry.
- `_regeneratePaperReport` aborts current task via `/abort` then calls
  `_generatePaperReport(true)` (force=true bypasses DB cache).

## Key invariants
- Server events are append-only with monotonic `seq`; frontend never
  mutates stream state outside `_applyReportEvent`.
- Tool-round schema: `{roundNum, toolName, query, toolCallId, toolArgs,
  status: 'searching'|'done', toolContent, _elapsed, results}` — same as
  `lib/tasks_pkg/tool_display._build_tool_round_entry` produces for chat.
- `results` contains per-URL dicts: `{title, url, source, snippet, fetched, fetchedChars}`.
- Frontend re-paint only when `_paperReportStream.paperId ===
  _activePaperId` — prevents cross-paper DOM leakage.
- DB row is the ground truth; running in-memory task is a transient cache
  for progress events until completion.

## Files touched
- `routes/paper.py` — `_execute_report_tool` returns structured tuple,
  `tool_done` event includes `results` + `searchDiag`.
- `static/js/paper-reader.js` — `_applyReportEvent` tool_done populates
  `r.results` from event, with synthesis fallback.
- `static/styles.css` — added `.paper-report-tools` wrapper padding.

