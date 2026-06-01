---
name: vlm-pdf-refresh-persistence
description: Fix for PDF VLM parsing progress lost on page refresh: sessionStorage persistence + server task reconnection by filename
enabled: true
tags: [javascript, python, pdf, vlm, refresh, sessionStorage, persistence]
created: 2026-04-06T15:17:54Z
updated: 2026-04-06T15:17:54Z
---

# VLM PDF Parse Refresh Persistence

## Problem
When a PDF is uploaded and VLM parsing is in progress, refreshing the page loses:
1. The `pendingPdfTexts` array (text parse result + VLM task_id)
2. The VLM polling loop (`_vlmParseEntry`) dies
3. Server-side VLM task continues but is orphaned (nobody polls it)

## Solution

### Frontend (upload.js)
- **`_vlmSaveState()`**: Persists `pendingPdfTexts` array to `sessionStorage` (key: `chatui_vlm_pending`)
  - Called after: text parse completes, VLM task starts, VLM progress updates, entry removal, doc upload
- **`_vlmClearState()`**: Clears sessionStorage (called when `pendingPdfTexts = []` on message send)
- **`_vlmRestoreState()`**: Called on page load — rebuilds `pendingPdfTexts` from sessionStorage
  - If VLM was in-progress AND taskId is saved → resumes polling via `_vlmPollTask()`
  - If VLM was in-progress BUT no taskId → calls `_vlmReconnectByFilename()` to find task on server
- **`_vlmPollTask(entry, taskId, isAlive, onUpdate)`**: Extracted shared polling loop used by both fresh parse and resume

### Backend (lib/pdf_parser/vlm.py + routes/upload.py)
- **`find_vlm_tasks_by_filename(filename)`**: Looks up active VLM tasks by filename for reconnection
- **`GET /api/pdf/vlm-tasks?filename=xxx`**: API endpoint exposing the lookup

### Key Design Decisions
- sessionStorage (not localStorage) — scoped to tab, auto-cleared on browser close
- Text content IS saved (can be 10-500KB) — the whole point is to preserve the text parse result
- Graceful degradation: if sessionStorage is full, `try/catch` silently fails → same behavior as before fix
- `_vlmSaveState()` is called from `onUpdate` callback during resume, keeping state fresh

## Files Modified
- `static/js/upload.js` — sessionStorage save/restore/reconnect logic
- `static/js/main.js` — `_vlmClearState()` on send, `_vlmRestoreState()` on init
- `static/js/ui.js` — `_vlmSaveState()` on edit restore
- `lib/pdf_parser/vlm.py` — `find_vlm_tasks_by_filename()`
- `routes/upload.py` — `GET /api/pdf/vlm-tasks` endpoint

