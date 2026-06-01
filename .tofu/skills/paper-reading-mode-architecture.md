---
name: paper-reading-mode-architecture
description: Paper Mode v3: text selection fix (--scale-factor), alphaxiv-style selection toolbar, background report pre-generation, DB persistence
enabled: true
tags: [feature, paper-mode, architecture, streaming, report, persistence, i18n, db-cache]
created: 2026-04-09T22:15:15Z
updated: 2026-04-17T11:02:22Z
---

---
name: paper-reading-mode-architecture
description: Paper Mode v3: text selection fix (--scale-factor + transform-origin), alphaxiv-style selection toolbar, background report pre-generation, DB persistence
enabled: true
tags: [feature, paper-mode, architecture, streaming, report, persistence, i18n, db-cache]
created: 2026-04-09T22:15:15Z
updated: 2026-04-17T00:00:00Z
---

# Paper Reading Mode Architecture

## Text Selection Fix (pdf.js v3.x)
- **Root cause #1**: pdf.js v3.11 requires `--scale-factor` CSS variable on the text layer container
- The text spans use `calc(var(--scale-factor) * Xpx)` for positioning
- **Fix #1**: `textDiv.style.setProperty('--scale-factor', _paperScale.toString())` in `_renderAllPages()`
- Error if missing: "The `--scale-factor` CSS-variable must be set, to the same value as `viewport.scale`"

## Ghost-Text Offset Fix on Selection (2026-04-17)
- **Symptom**: selecting PDF text shows doubled/offset glyphs — each selected word appears twice, progressively drifting rightward
- **Root cause #2**: pdf.js applies per-span `transform: scaleX(N)` to make fallback-font glyph widths match canvas glyph widths. Without `transform-origin: 0 0` on the span, scaleX pivots from center, shifting every span horizontally. Amplifies toward the right of each line.
- **Root cause #3**: `color: transparent` hides text normally, but `::selection` repaints glyphs in default color, exposing the misalignment.
- **Fix (static/styles.css)**:
  ```css
  .paper-text-layer span{...;transform-origin:0 0}
  .paper-text-layer ::selection{background:rgba(110,86,207,0.35);color:transparent}
  .paper-text-layer span::selection{background:rgba(110,86,207,0.35);color:transparent}
  ```

## Text Selection + Quote (AlphaXiv-style)
- Floating selection toolbar appears when user selects text in PDF viewer
- Two buttons: **Ask** (quotes + auto-sends "Explain this part") and **Quote** (inserts into QA input)
- Toolbar positioned relative to `.paper-left` container (absolute positioning)
- `_handlePaperTextSelection()` called on `mouseup`, positions the `#paperQuoteBtn` div
- `_askAboutPaperSelection()` — quotes selection and auto-sends question
- `_quotePaperSelection()` — inserts as blockquote in QA input

## Report Pre-Generation (Background)
- `_preGenerateReport()` fires immediately after PDF parsing completes (in `_paperUploadFile` and `_fetchArxivPaper`)
- First checks server DB cache via `/api/paper/report/cache`
- If no cache, silently streams from `/api/paper/report` to populate DB
- When user clicks Report tab: `_paperReportCache` already populated → instant render
- `_pregenAbort` AbortController used to cancel on paper switch or manual regenerate
- Result also stored in-memory and saved to library state

## Report Generation
- **Single model call** — one comprehensive prompt generates the full report in a streaming fashion
- Two prompt templates: `_REPORT_PROMPT_EN` (English) and `_REPORT_PROMPT_ZH` (Chinese)
- Backend: `POST /api/paper/report` → streams SSE `{delta}` chunks, then `{done: true, paper_hash: "..."}`
- If DB cache hit, returns `{cached: true, report: "...", paper_hash: "..."}`

## Report Persistence — DB Only, NOT localStorage
- Reports stored in `paper_reports` table (SQLite)
- Keyed by `(paper_hash, lang)` — hash is SHA-256 of paper text, truncated to 32 chars
- `paper_hash` stored in localStorage library entry for stable cache lookups
- parsedText truncated to 200K in localStorage; hash computed from full text on server

## localStorage Library Entry
```js
{
  id, title, pdfUrl, arxivId,
  parsedText: (truncated to 200K),
  qaHistory: (last 20),
  hasReport: true/false,
  paperHash: "abc123...",
  babelCache, createdAt, pageCount
}
```

## State Variables
- `_paperReportCache` — in-memory report text (cleared on paper switch/mode exit)
- `_paperHash` — server-side hash for DB cache lookups
- `_pregenAbort` — AbortController for background report pre-generation

