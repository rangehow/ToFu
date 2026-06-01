---
name: paper-report-image-injection
description: Paper report: deterministic post-processing injects cropped figures/tables inline (LLMs ignore manifest instruction)
enabled: true
tags: [paper-mode, report, images, post-processing, bug-fix]
created: 2026-04-17T10:20:58Z
updated: 2026-05-19T16:18:25Z
---

# Paper Report — Deterministic Image Injection

## Problem
Extracted figures/tables (pymupdf clip → `/api/paper/images/<hash>/fig_NN_pM.jpg`)
were listed in a manifest appended to the LLM prompt with instructions to
embed `![caption](url)`. **LLMs (glm-5.1, gpt-4.1-mini, claude) universally
ignored the instruction** — 0 image tags in any stored report.

## Fix — `_inject_images_into_report()` in `routes/paper.py`
Deterministic post-processing:
1. Parse each manifest caption for kind (Figure/Fig/图, Table/Tab/表) + number.
2. Split report into paragraphs, search for mention patterns `Figure 3` /
   `图 3` / `Table 1` / `表 1`.
3. Insert `![caption](url)` right after the first paragraph that mentions it.
4. Skip code fences and table rows.
5. Append unmatched images as "📎 Figures & Tables (Appendix)" gallery.
6. No-op if model already embedded any `/api/paper/images/` URL.

## Manifest is server-owned (refactor 2026-05-19)
The image manifest is the authoritative source of truth on disk at
`uploads/papers/images/<phash>/manifest.json`. The **client never forwards
images** in any request — `_load_image_manifest(phash)` and
`_extract_paper_figures(filepath, phash)` in `routes/paper.py` are the
canonical helpers.

## Wiring
- **Live stream**: after `_stream_report_with_tools()` completes, enrich
  full_text, persist enriched version to `paper_reports`, emit `enriched`
  SSE event.
- **`/api/paper/report/start`**: loads manifest by hash; falls back to
  `_ensure_paper_images(filename, phash)` if no manifest exists yet.
- **`/api/paper/report/cache`**: loads manifest by hash and re-enriches old
  cached reports on the fly.
- **`/api/paper/upload` & `/api/paper/fetch-arxiv-stream`**: extract figures
  synchronously as part of ingestion — no race with a background JS call.
- **`/api/paper/extract-images`**: legacy / forced-re-extract endpoint;
  no longer in the hot path.

## Frontend (`static/js/paper-reader.js`)
- Receives `paper_hash` + `images` directly from upload / arxiv responses.
  No more `_extractPaperImages()` race condition.
- Cache lookup body shrunk to `{paper_hash, lang}`.
- `_persistPaperEntry(entry, _first)` only ships parsed_text / images /
  paperHash on the first save; subsequent saves are small mutable updates.

## Export (server-rendered, refactor 2026-05-19)
`GET /api/paper/report/export?paper_hash=…&lang=…&format=md|html` returns
the file as a download via Content-Disposition. The HTML variant uses
Python's `markdown` package (3.5+) with `tables`, `fenced_code`,
`attr_list`, `sane_lists` extensions. Root-anchored URLs are absolutized
via `request.host_url`. Frontend `_exportPaperReport(format)` is now a
thin shim: `<a href=…>` for md/html, `window.open + .print()` over the
HTML for pdf.
