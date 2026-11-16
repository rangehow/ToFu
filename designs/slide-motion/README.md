# Slide motion: LibreOffice evidence

This is evidence for [`docs/SLIDE_MOTION.md`](../../docs/SLIDE_MOTION.md). It is not a
prototype. The sample is a synthetic three-page deck:

1. A title and a three-bullet text box with `motion: {reveal: [points]}`.
2. A `metric` component (`kpi`) and a shape, with `motion: {reveal: [chartbox], emphasize: kpi}`.
3. The same title and shape, smaller and moved, with `motion: {continues: true}`.

Tofu exported it with `export_pptx`: 2 fade transitions, 1 Morph, 5 build
steps, package QA passed. LibreOffice 26.8.2.2 then converted it headless
(`soffice --headless --convert-to odp|pdf`), and both conversions succeeded.

`libreoffice-pages.png` shows LibreOffice's PDF rendering of the three pages,
which is each page's final build state.

How LibreOffice imported the motion, from the ODP's `content.xml`:

| Page | Imported timing | Transition |
|---|---|---|
| 1 | three `on-click` nodes, each `ooo-entrance-fade-in`, one per paragraph | `fade` / `crossfade`, 0.5 s |
| 2 | `on-click` `ooo-entrance-fade-in` (shape), then `on-click` `ooo-emphasis-flash-bulb` (preset 26, the value text) | `fade`, 0.5 s |
| 3 | none | `fade`, 1 s: the Morph `mc:Fallback` (LibreOffice has no Morph) |

Live slideshow playback frames were not captured: headless LibreOffice
has no slideshow capture. `tests/slides/test_slides_motion.py::test_libreoffice_imports_motion`
repeats this import check when `soffice` is on `PATH`.
