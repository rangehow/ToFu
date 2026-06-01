---
name: paper-reader-text-recovery
description: Paper mode: library entries created before server-side parsing have empty parsedText — use /api/paper/reparse to recover
enabled: true
tags: [paper, pdf, bug-pattern]
created: 2026-04-17T09:39:43Z
updated: 2026-04-17T09:39:43Z
---

# Paper Reader — parsedText recovery

## Bug pattern

Each paper library entry stores `parsedText` in localStorage. If an entry was saved:
- before server-side parsing was added, or
- with a failed PDF parse (scanned/image PDFs), or
- via a codepath that doesn't parse (early arXiv fetches),

then `_paperParsedText` stays `''` when the entry is re-opened, and the Report tab shows:
> "No paper text available. Load a PDF first."

even though the PDF file is still on disk under `PAPER_DIR`.

## Fix (already applied)

- Backend endpoint `POST /api/paper/reparse` in `routes/paper.py` — takes a `filename`
  (basename already under `PAPER_DIR`), re-runs `lib.pdf_parser.parse_pdf`, returns
  `{ok, text, total_pages, text_length}`.
- Client helper `_ensurePaperText()` in `static/js/paper-reader.js` — called lazily
  by the Report tab (`_generatePaperReport`) and Q&A (`_sendPaperQuestion`) when
  `_paperParsedText` is empty. Persists recovered text via `_saveActivePaperState()`.

## Key files

- `routes/paper.py` — `reparse_paper()` handler (near `upload_paper`)
- `static/js/paper-reader.js` — `_ensurePaperText()` helper + call sites in Report + Q&A

## Do NOT forget

After editing `static/js/*.js`, the bundler (`lib/js_bundler.py`) regenerates on
content-hash change at server startup. Old `bundle-<hash>.js` is cleaned up
automatically, but removing it manually forces a clean rebuild next restart.

