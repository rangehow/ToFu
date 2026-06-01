---
name: pdf-vlm-batching-and-parallel-uploads
description: PDF uploads parallelized (no mutex) + VLM batches K pages per call (env PDF_VLM_BATCH_PAGES default 4)
enabled: true
tags: [pdf, vlm, performance, frontend, concurrency]
created: 2026-05-06T10:58:55Z
updated: 2026-05-06T10:58:55Z
---

# PDF: parallel uploads + VLM page batching (2026-05-06)

## Frontend (Option C — kill the single-flight mutex)

`static/js/upload.js`:
- `pdfProcessing` is no longer a boolean mutex; it's an integer COUNTER
  ("# of in-flight text-extract calls"). Initialized lazily in
  `handlePDFUpload` so the legacy `core.js` `pdfProcessing = false`
  declaration still works (false coerces to 0 / NaN handling via `| 0`).
- `handlePDFUpload` no longer rejects when another parse is in flight —
  it pushes a placeholder `pdfObj` (with `method: 'parsing'`) into
  `pendingPdfTexts` IMMEDIATELY so the second file's card appears
  optimistically while the first is still being parsed.
- `handleFileUpload` now `Promise.allSettled`s the per-file tasks
  instead of `for…of await`. Werkzeug already runs `threaded=True`,
  so concurrent `/api/pdf/parse` requests run on different threads.
- `_isAnyPdfTextParsing()` / `_refreshPdfProgressBar()` give a unified
  multi-file progress message.

`static/js/main.js` `sendMessage()`:
- Old `if (pdfProcessing) return;` silently dropped sends — replaced
  with a non-blocking note and an extended wait in
  `_waitForVlmParsing` that ALSO awaits text-parse completion
  (`p.method === 'parsing'`) before the VLM-wait phase. 5-min cap.

## Backend — VLM batching

`lib/pdf_parser/vlm.py`:
- `vlm_parse_pdf(..., batch_pages=None, max_workers=None, ...)` now
  groups N pages per VLM call (default 4, env `PDF_VLM_BATCH_PAGES`,
  bounds 1–16). 64-page paper: 64 calls → 16 calls. Same image bytes,
  far less HTTP / 429 storm.
- `max_workers` cap (env `PDF_VLM_MAX_WORKERS`, default = #batches =
  unlimited). Lower it on shared keys.
- `max_tokens` scales with batch (4096/page default, env
  `PDF_VLM_MAX_TOKENS`, bounds 2048–131072).
- `_vlm_call_pages(..., max_tokens)`: timeout now scales with batch
  size (60s + 30s/page, capped 480s); `max_retries=5` (was 3 default
  in smart_chat) to better tolerate 429-cycles since 429 doesn't burn
  the retry budget anyway.
- Progress callback is now thread-safe (lock around `done_pages`).
- Label format: `p.X-Y` for batches, `p.X` for single-page.

## Why batching is safe for paper-style PDFs
The system prompt already instructs "continue naturally without page
markers" across page boundaries, so the model concatenates output for
a multi-page batch fine. Quality gate in `_vlmPollTask` still runs
client-side.

## Tuning knobs at a glance
- `PDF_VLM_BATCH_PAGES=1` — restore legacy one-page-per-call.
- `PDF_VLM_BATCH_PAGES=4` — default.
- `PDF_VLM_BATCH_PAGES=8` + `PDF_VLM_MAX_WORKERS=4` — slowest 429 storm,
  good for shared keys.

## Related memory
See `vlm-wait-indicator-welcome-screen-bug` for the user-bubble render
ordering fix that goes hand-in-hand with this change.

