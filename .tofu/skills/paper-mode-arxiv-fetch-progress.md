---
name: paper-mode-arxiv-fetch-progress
description: Paper mode arXiv fetch: server-side streaming endpoint with progress + parse, fixing the silent text-extraction failure that caused "PDF not loaded" on Report tab
enabled: true
tags: [paper-mode, arxiv, sse, progress, bug-fix]
created: 2026-04-17T08:50:24Z
updated: 2026-04-17T08:50:24Z
---

# Paper Mode — arXiv Fetch Progress + Parse Fix

## Problem
Fetching from arXiv in Paper Mode had two issues:
1. UI only showed a static "Fetching from arXiv…" spinner — no download progress.
2. After downloading, the frontend did two extra round-trips (re-download blob → re-upload to `/api/pdf/parse`). If either failed it was silently swallowed by `console.warn`, leaving `_paperParsedText = ''`. When the user opened the Report tab, `_generatePaperReport()` showed "No paper text available. Load a PDF first." — perceived as "PDF not loaded".

## Fix

### Backend — new SSE endpoint
`POST /api/paper/fetch-arxiv-stream` in `routes/paper.py` streams events:
- `{stage: 'resolve', arxiv_id, pdf_url}`
- `{stage: 'download', downloaded, total}` (throttled ~10/s)
- `{stage: 'download_done', file_size, elapsed, cached}`
- `{stage: 'parse_start'}`
- `{stage: 'parse_done', total_pages, text_length, elapsed}`
- `{stage: 'done', ok, pdf_url, arxiv_id, parsed_text, total_pages, text_length, cached, parse_error?}`
- `{stage: 'error', error}`

Downloads via `requests.iter_content`, then parses via `lib.pdf_parser.parse_pdf(max_text_chars=0, max_images=0)` — text is returned inline in the final `done` event, so no client round-trip needed. The old `/api/paper/fetch-arxiv` is kept for backward compat.

### Frontend
`_fetchArxivPaper()` in `static/js/paper-reader.js`:
- Consumes the SSE stream, calls `_renderArxivFetchProgress()` on every event.
- Progress bar uses real `downloaded/total` bytes; falls back to indeterminate animation if `Content-Length` missing.
- Surfaces `parse_error` as a warning `debugLog`, and emits a clear warning if parsed text is empty (so the user knows Report/Q&A won't work).
- Fix location: no more silent `console.warn` on text extraction failure.

### CSS
Added `.paper-fetch-progress`, `.paper-fetch-bar`, `.paper-fetch-detail` in `static/styles.css`.

## Bundle
JS bundle auto-rebuilds from file mtimes via `lib/js_bundler.py` on server restart — no manual rebuild needed.

