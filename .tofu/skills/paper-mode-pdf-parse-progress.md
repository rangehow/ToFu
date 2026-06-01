---
name: paper-mode-pdf-parse-progress
description: Paper mode: per-page PDF parse progress via pymupdf4llm pages=[i] loop + SSE parse_progress
enabled: true
tags: [paper-mode, pdf, sse, progress, pymupdf4llm]
created: 2026-04-17T10:56:38Z
updated: 2026-04-17T10:56:38Z
---

# Paper Mode — Per-page PDF parse progress

## Problem
`pymupdf4llm.to_markdown(doc, page_chunks=True, ...)` is a **single blocking call**. On a 30-page arXiv paper with `table_strategy="lines"` it can take 30–60s with zero feedback. The paper-reader UI showed a stuck "Extracting PDF text…" spinner with no detail.

## Fix
1. `lib/pdf_parser/text.py::extract_pdf_text(..., progress_callback=None)` now iterates **page by page** calling `pymupdf4llm.to_markdown(doc, pages=[pi], ...)` per page, invoking `progress_callback(done, total)` after each page. Adds ~5-10% overhead but enables honest progress.
2. `lib/pdf_parser/core.py::parse_pdf(..., progress_callback=None)` threads a `(stage, done, total)` callback through (stages: `'text'`, `'images'`).
3. `routes/paper.py::fetch_arxiv_stream`: runs `parse_pdf` in a **worker thread** and bridges its callback to SSE via a `queue.Queue`. Emits `parse_progress {parse_stage, page, total_pages}` events throttled to ~10/sec, with 1s heartbeats (`:hb\n\n`) to keep proxies from buffering.
4. `static/js/paper-reader.js::_renderArxivFetchProgress` handles `parse_progress`, shows `"page N / M"` with a determinate bar.

## Key gotcha
Do NOT emit SSE events from inside the callback directly — the callback runs in the worker thread, not the Flask generator. Use a thread-safe queue and let the generator drain it.

## Model picker: no more "Default (auto)"
Removed the "Default (auto)" dropdown item in `_populatePaperReportModelDropdown` — it was ambiguous (meant "let server pick whatever chat is using"). Now auto-selects the first visible chat model on first populate. `_paperReportModel` is seeded on `enterPaperMode()` and before `_generatePaperReport()` as a safety net. Label never shows "Default" — falls back to "Select model" if no models available.

