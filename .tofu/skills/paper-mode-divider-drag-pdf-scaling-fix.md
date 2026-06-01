---
name: paper-mode-divider-drag-pdf-scaling-fix
description: Fix paper mode: divider drag blank gap + PDF pages not scaling proportionally when panel shrinks
enabled: true
tags: [paper-mode, bug-fix, css, resize, pdf, scaling, divider]
created: 2026-04-16T02:40:16Z
updated: 2026-04-18T03:11:49Z
---

---
name: paper-mode-divider-drag-pdf-scaling-fix
description: Paper mode zoom + divider-drag behavior — horizontal scroll on overflow, auto fit-to-width on load and on panel shrink
enabled: true
tags: [paper-mode, bug-fix, css, resize, pdf, scaling, divider, zoom]
---

# Paper Mode Divider + Zoom Behavior

## Zoom model (as of 2026-04-18)

Matches Chrome/Acrobat/Preview:
- Zoom slider (25–400%) is meaningful across its full range.
- When zoomed-in content exceeds panel width, `.paper-pdf-container` scrolls horizontally (`overflow:auto` + `align-items: safe center`).
- **No** `max-width:100%` clamp on `.paper-page-wrapper` / `.paper-pdf-canvas` — they render at their true CSS pixel size.

### Initial load (paper-reader.js `loadPaperPdf`)
- After `getDocument`, auto fit-to-width: compute `containerW / baseViewport.width` and set `_paperScale` before first render.

### Divider drag end (paper-reader.js `_onMouseUp` / `_onTouchEnd`)
- `_autoRefitIfOverflowing()`: if page wrapper width > viewer.clientWidth - 32, call `paperFitWidth()`.
- **Widening** the left panel preserves current zoom (user gets whitespace around the page).
- **Narrowing** below current page size snaps to fit-width automatically (no manual re-fit needed).

### Manual fit (unchanged)
- ⤢ toolbar button and `0` hotkey both call `paperFitWidth()`.

## Divider drag mechanics (unchanged)
- Only left panel gets `flex:none` + explicit width during drag.
- Right panel stays `flex:1` to fill remaining space (prevents blank gap from subpixel rounding).
- `available = bodyW - dividerW` constrains `newLeftW` between 250 and `available - 250`.
- Double-click resets to 50/50.

## ResizeObserver text-layer scaling (legacy, now rarely triggers)
With max-width cap removed, wrapper rendered width always equals `cssW`, so `actualW/origW ≈ 1`.
Observer is kept as a safety net in case a CSS constraint ever clamps a wrapper again.

## Key files
- `static/js/paper-reader.js` — `loadPaperPdf` (init fit-width), `_renderAllPages`, `paperFitWidth`, `_autoRefitIfOverflowing`, divider drag handlers.
- `static/styles.css` — `.paper-pdf-container` (`align-items: safe center`), `.paper-page-wrapper` (no max-width), `.paper-pdf-canvas` (no max-width).

