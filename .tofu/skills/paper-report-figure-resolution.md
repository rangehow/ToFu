---
name: paper-report-figure-resolution
description: Paper report figure quality: manifest v7, multi-page tables, caption-below tables, image-block stop
enabled: true
tags: [paper-reader, pdf, images]
created: 2026-05-20T06:39:30Z
updated: 2026-05-24T08:10:16Z
---

# Paper Report — Figure Image Quality

## Resolution
Three knobs control extracted-figure sharpness (`lib/pdf_parser/images.py` + `routes/paper.py`):
1. `_extract_paper_figures(max_image_width=…)` — pixel-cap of the rendered crop
   (currently **1800**). Was 900 — too low for retina, made figures blurry.
2. `detect_and_clip_figures(zoom = min(max_image_width / clip.width, 5.0))`
   — zoom ceiling. Was 3.0 → small figures couldn't reach max_image_width.
3. `resize_image_bytes(quality=90)` JPEG quality. Was 82.

## Auto-crop whitespace + tighter horizontal clipping
`_auto_crop_whitespace()` trims near-white borders. When detected image
rects exist above a figure caption, horizontal bounds are tightened to the
union of those rects + caption width. Same logic for tables when
`page.find_tables()` returns a matching bbox.

## Section-heading aware clipping
`_SECTION_HEAD_RE` matches "3.2 Foo" / "4 Experiments". Both figure and
table clip walks stop at numbered section headings, in addition to body
text and other captions. Prevents tables from spilling into the next
section when `find_tables()` misses the boundary.

## Caption-below table convention (v6/v7)
When a `table_cap` is detected and:
  • the downward walk produces a tiny clip (< 100 px), OR
  • the downward walk runs into a sizable image block (height > 50 px
    AND width > 30% of page) — that's the NEXT figure, never part of a
    text-typeset table → caption-below convention is in play
… switch to walking UPWARDS from the caption. The upward walk stops at
the previous caption / section heading / sizable image block (whose TOP
becomes the upper bound — that image is likely the table rendered as
graphics above its caption).

This catches papers like YOCO-U where Table 2's caption sits ABOVE the
next figure (the bar chart) and the actual table data is rendered as
prose+formulas ABOVE the caption. Without this fix the clip captured
the bar chart of the unrelated figure that follows.

## Multi-page table continuation (v5+)
`detect_and_clip_figures(page, ..., doc=doc)` accepts the parent
pymupdf.Document. When a table_cap clip extends to within 30 px of the
page bottom, `_try_stitch_next_page_table()` renders a continuation slice
from the next page (top → first body text / section heading / caption,
capped at page height) and PIL-stitches the two PNGs vertically into one
composite. Bounded to ONE next page; longer tables would need recursion.
The composite reports `source='table_clip_multi'` and includes a `pages`
array `[p, p+1]` in the manifest entry.

## Manifest cache invalidation
`uploads/papers/images/<phash>/manifest.json` is versioned:
`{"version": _FIG_EXTRACT_VERSION, "images": [...]}`. Current version = **7**.
Bump `_FIG_EXTRACT_VERSION` in `routes/paper.py` whenever extraction params
change so existing papers regenerate on next access.

## Click-to-enlarge
`.paper-report-body img` / `.paper-report-content img` have CSS
`cursor:zoom-in` (`static/styles.css:667`) but no built-in click handler.
A delegated listener at the bottom of `static/js/paper-reader.js` calls
`_openImageFullscreen(img.src)` (defined in `static/js/image-gen.js`).

For tall images (height > 1.3× width — common for stitched multi-page
tables), the fullscreen overlay switches to `overflow-y:auto;
align-items:flex-start; max-height:none` so the image is scrollable.

