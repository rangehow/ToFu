---
name: docling-structured-pdf-mode
description: Opt-in Docling backend for PDF parsing: mode='structured' → better tables/math, heavy dep (~2 GB)
enabled: true
tags: [pdf, docling, performance, install]
created: 2026-05-06T11:24:06Z
updated: 2026-05-06T11:24:06Z
---

# PDF structured mode via Docling (2026-05-06)

## What it is
Third text-extraction strategy added to `lib/pdf_parser`:
- **`mode='rich'`** — pymupdf4llm (default, shipped)
- **`mode='structured'`** — IBM Docling (TableFormer + equation model;
  best on academic PDFs). OPT-IN heavy dep (~2 GB with torch).
- **`mode='fast'`** — raw `get_text` (web_search / BM25 ranking).

## Files
- `lib/pdf_parser/docling.py` — new module. Lazy-loads `DocumentConverter`
  once per process, caches it. Graceful fallback when `docling` is missing
  (logs a one-time info message and returns None).
- `lib/pdf_parser/_common.py` — adds `HAS_DOCLING` flag (silent at import
  so we don't nag users who never asked for it).
- `lib/pdf_parser/text.py` — adds Strategy 0 for `mode='structured'`:
  tries docling, falls back to pymupdf4llm on None/failure.
- `lib/pdf_parser/core.py` — `parse_pdf(text_mode=...)` threads the choice
  through; `method` field in response reports `'docling'` when docling is
  available + requested.
- `lib/pdf_parser/__init__.py` — `_import('docling', ...)` registers the
  optional submodule.
- `routes/upload.py /api/pdf/parse` — reads `textMode` form field, falls
  back to env var `PDF_TEXT_MODE`.

## How users enable it
```bash
# 1. Install
./install.sh --with-docling               # adds ~2 GB via pip
python install.py --with-docling          # same, cross-platform
# Or manually: pip install docling --extra-index-url https://download.pytorch.org/whl/cpu

# 2. Turn on
export PDF_TEXT_MODE=structured            # per-process default
# OR per-request: POST to /api/pdf/parse with form field textMode=structured
```

## Progress callback caveat
Docling's high-level API does NOT expose mid-conversion per-page progress.
We only emit `(0, N)` at start and `(N, N)` at end. If the UI needs honest
per-page progress, use `mode='rich'` instead. The paper-reader SSE bridge
tolerates the coarse progress (only shows the bar, no "N/M" text).

## Install docs
- `install.sh` gained `--with-docling` flag (lines ~1082-1109, after the
  Playwright step).
- `install.py` gained matching flag + `install_docling()` helper.
- `.env.example` documents `PDF_TEXT_MODE`, `PDF_VLM_BATCH_PAGES`,
  `PDF_VLM_MAX_WORKERS`, `PDF_VLM_MAX_TOKENS`.
- Both README.md and README_CN.md have a dedicated section under Paper
  Reader explaining the trade-off and how to install.

## Design rationale
- Opt-in (NOT default) because docling pulls 2 GB of torch + model weights
  on first run. pymupdf4llm covers 95% of users.
- Import is silent when missing — we don't want to warn on every server
  start. First call to `extract_pdf_text_docling()` logs a one-time hint.
- Falls back to pymupdf4llm on ANY error (import, init, convert) so
  uploads never break when the user toggles `structured` without
  actually installing docling.

