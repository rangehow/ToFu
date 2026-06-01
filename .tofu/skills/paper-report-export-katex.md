---
name: paper-report-export-katex
description: Paper report HTML/PDF export: KaTeX + math protection + base64-embedded images for offline portability
enabled: true
tags: [paper-mode, export, katex, markdown]
created: 2026-05-24T01:29:55Z
updated: 2026-05-24T07:24:17Z
---

# Paper report — Standalone HTML / PDF export

## Three things the export must get right

### 1. KaTeX math rendering
Python's `markdown` package mangles `$a_i$` → `$a<em>i</em>$` because
`_i_` is emphasis. Two-step fix in `routes/paper.py::export_report`:
- **Stash math BEFORE markdown**: regex `$$...$$`, `\[...\]`, `$...$`,
  `\(...\)` into `\x02MATH<i>\x03` placeholders, run
  `markdown.markdown(...)`, restore originals.
- **Inject KaTeX CDN**: `<link>` for `katex.min.css` + two `<script
  defer>` tags. The auto-render `onload` calls
  `renderMathInElement(document.body, {…})` over the four delimiter
  pairs and sets `window.__katexReady = true`.

### 2. Embedded images (base64 data: URIs)
For paper-image URLs (`/api/paper/images/<phash>/<fname>`), the export
endpoint reads the file from disk and inlines it as
`data:image/png;base64,...` so the standalone HTML works offline (no
server reachability required). Previous behavior — rewriting to absolute
http(s) URLs — broke when users opened the file from their downloads
folder while the server was unreachable.

```python
def _embed_paper_image(match):
    attr, url = match.group(1), match.group(2)
    m = re.match(r'^/api/paper/images/([a-f0-9]{8,64})/([\w\-.]+)$', url)
    if not m: return attr + origin + url   # non-paper URLs fall back
    ...
    return f'{attr}data:{mime};base64,{b64}'
```

Other root-anchored URLs still get rewritten to `<origin>/...` — only
paper-image links are inlined (data URIs balloon HTML size if applied
to every static asset).

### 3. PDF flow = inline HTML + auto-print
`format=pdf` returns the same HTML body **inline** (no
`Content-Disposition: attachment`) plus an embedded auto-print script.
The script polls `window.__katexReady` AND waits for image loads before
calling `window.print()` — otherwise printing fires before formulas are
typeset and the PDF shows raw `$…$` source. Frontend opens
`?format=pdf` in a new tab; no client-side print logic.

