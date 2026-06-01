---
name: docx-to-pdf-with-latex-via-katex-playwright
description: Convert DOCX containing LaTeX $...$/$$...$$ math to PDF using KaTeX + Playwright Chromium
enabled: true
tags: [docx, pdf, latex, katex, playwright, conversion]
created: 2026-05-05T11:19:43Z
updated: 2026-05-05T11:19:43Z
---

# DOCX → PDF with LaTeX math rendered (KaTeX + Playwright)

When a .docx contains LaTeX written as plain text with `$...$` / `$$...$$` delimiters
(NOT OMML equations), and the environment has NO pandoc / libreoffice / xelatex /
wkhtmltopdf but DOES have Playwright + Chromium, this recipe produces a clean PDF:

1. Extract paragraphs (+ inline images) with `python-docx`:
   - `d.paragraphs` for text
   - Find image rIds per paragraph via `re.findall(r'r:embed="([^"]+)"', etree.tostring(p._element))`
   - Read `word/_rels/document.xml.rels` to map rId → media file
2. Build a single self-contained HTML:
   - Inline the project's `static/vendor/katex/katex.min.{css,js}`
   - Rewrite `url(fonts/KaTeX_*.woff2)` in katex.min.css to base64 data URIs
     (so PDF export has all glyphs without filesystem access)
   - Inline the images as base64 data URIs
   - Escape text with `html.escape` — dollar signs survive since they're not
     in the HTML-special set
   - Inject a tiny `auto-render` script: walk text nodes, match
     `/\$\$([\s\S]+?)\$\$|\$([^\$\n]+?)\$/g`, call
     `katex.render(tex, span, {displayMode, throwOnError:false, strict:'ignore'})`
   - After walking, set `document.body.dataset.katexDone='1'`
3. Render to PDF with Playwright:
   - `page.goto('file://...', wait_until='networkidle')`
   - `page.wait_for_function("document.body.getAttribute('data-katex-done')==='1'")`
   - `page.pdf(format='A4', print_background=True, margin=...)`

CSS tips:
- Use `@page { size: A4; margin: 18mm 16mm; }`
- CJK font stack: `"Noto Serif CJK SC","Source Han Serif SC","Songti SC","SimSun", serif`
- `figure.pic { page-break-inside: avoid; }` to keep problem images intact
- `hr.sep { page-break-before: always; }` for section separators

Working script lives at /tmp/jieda_build/build_pdf.py (used on
/mnt/.../docs/解答260505.docx on 2026-05-05).

Classifier for 解答*.docx style files:
- `题目分析`, `核心知识点小结` → h2
- `^\(\d+\)\s` → h3  (e.g. "(1) 求证：…")
- lines ending `证明思路：` / `解题步骤：` → h4
- lines wrapped in `$$...$$` only → `<p class="display-math">`

