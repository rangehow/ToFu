# PDF translation rendering (artifact layer)

Current-state authority for `lib/pdf_parser/render.py` (painting a solved
`PagePlan`) and `lib/pdf_parser/layout.py` (the extraction→layout bridge).
It consumes the geometry contract in [`PDF_RELAYOUT.md`](PDF_RELAYOUT.md).

## Problem and decision

**Problem.** The re-layout solver decides where translated lines go, but the
pipeline still has no artifact. A renderer that repaints a page from scratch
loses vector rules, figure bitmaps and table rulings; one that only paints over
source text leaves the old wording in the text layer, so extraction, search and
the fidelity gate see both copies.

**Decision.** `render_page_plan(source_pdf, plan, output_path)` copies the
source page's own content (images, vector figures, rules, table rulings), then
removes only the source glyphs a placed block replaces, and paints each
`PlacedLine` with PyMuPDF's `TextWriter` at the plan's exact baseline and
`TextRun` font/size. Page size and column assignment come from the plan; a run
or line outside its column raises `RenderInvariantError` instead of painting.

**Alternatives considered and rejected.**

* Repainting figures/tables from coordinates: loses vector detail and any
  content the extractor did not model.
* Painting opaque rectangles over source text: keeps the original wording in
  the PDF text layer and hides drawings beneath the box.
* Reconstructing the page with a layout engine: changes the page/column layout
  the owner requires to stay fixed.

## Pipeline

```
extract_pdf_structure(bytes)                    # lib.pdf_parser.structure
  → page_layout_from_structure(page)            # lib.pdf_parser.layout
  → solve_page(layout, translations)            # lib.pdf_relayout.solver
  → render_page_plan(bytes, plan, path)         # lib/pdf_parser/render.py
  → run_gate(source, rendered, mode='translation')  # pdf_fidelity.py
```

`page_layout_from_structure` is pure mapping: it builds `Rect`/`Column`/
`FontSpec`/`Block` values. It preserves blocks the solver model cannot own —
running heads/folios and full-width flowables on a multi-column page — and
reports them as `AdaptedPage.preserved` with a reason. The renderer keeps
their source text unchanged.

Block mapping: `paragraph→text`, `list→list`, `caption→caption`,
`heading`/`title→heading`, `figure`/`table` fixed. Column bands use the
extracted x-range; their vertical extent reaches from the running head to the
folio (page bounds when neither exists). A column is solved as contiguous runs
between fixed obstacles, so a long translation moves later flowable blocks down
the same run instead of being capped at the next block's original top.
`BlockPlacement.source_bbox` records where the source text was, which is what
the renderer redacts even when the translated block moved.

## Rendering contract

1. **Non-text content.** The page is copied with `Document.insert_pdf`; images
   and vector drawings are never redrawn. `apply_redactions` runs with
   `PDF_REDACT_IMAGE_NONE` and `PDF_REDACT_LINE_ART_NONE`, so only glyphs are
   removed.
2. **Owned text.** A source text block is removed only when a flowable
   placement's `source_bbox` starts at the same top edge and overlaps
   horizontally. The placement's painted `bbox` may be lower after a
   length-compensating reflow, but the source glyphs are still removed at
   `source_bbox`. Blocks inside a fixed figure/table, refused blocks, running
   heads and preserved full-width blocks keep their source text.
3. **Painting.** One `TextWriter` per page appends every run at
   `(run.x0, line.baseline_y)` with the run's embedded face and
   `run.font_size_pt`. Leading is implied by the plan's baselines.
4. **Columns.** Every line and run is checked against its `Column.bbox` before
   painting; a violation is `RenderInvariantError`. The page size must match
   the plan within 0.5 pt (`RenderInputError` otherwise).
5. **Fonts.** Only the local files named by `TextRun.font_path` are embedded.
   A missing file, an unreadable face, or a face whose family does not match
   the planned family raises `RenderFontError`; no family is invented.
6. **Refusals.** `RenderReport.refusals` carries every `LayoutRefusal`.
   `RenderReport.accepted` is false when any exists. Refused source text stays
   in place and is listed in `preserved_source_text`; it is never silently
   erased.
7. **Output.** `output_path` is written atomically (temp file + `os.replace`).
   The bytes are also returned so the caller can feed the gate directly.

### Baseline relation

The extraction layer reports line boxes, not baselines. The solver derives a
baseline as `line_top + size * ascent_ratio`; to invert PyMuPDF's line box
(`baseline - hhea.ascent`), `FontLibrary` must use the face's `hhea.ascent`
(not OS/2 `sTypoAscender`). With that ratio, an identity render reproduces the
source line boxes and text exactly.

### Column-band ink allowance

A source line's bbox is its ink extent; a pen advance includes the final
glyph's right side bearing. `layout.py` widens a band and block right edge by
`0.05 em` (bounded by the page) so an exactly full source line does not reflow
at the same font. Without it, an identity translation could never preserve the
source line breaks.

## End-to-end entry point

`lib/paper/builds/pdf_translation.py` composes the whole path and is the only
caller that owns the sequence:

```python
translate_pdf(source_pdf, target_language,
              translate=callable, declared_fonts=..., font_dirs=...)
```

`translate` has the adapter's `model_call(prompt) -> str` shape and is passed
straight to `translate_page`; this module performs no model, network or key
handling. Per page it translates, solves, renders and finally runs
`run_gate(..., mode='translation')` on the merged document. The result carries
the output bytes, the gate's per-page reports, the built manifest, every block
outcome and a translated/refused/skipped/failed/preserved summary.

The manifest covers every source block exactly once: figures/tables are
`fixed`, layout-preserved blocks are `preserved`, accepted translations are
`translated`, and blocks whose translation failed or was refused are `refused`
(the renderer keeps their source text). A manifest gap therefore fails the
gate's `translation_manifest` check instead of passing vacuously. A failed
block stays rendered: only its translation is missing, so its source text is
preserved and the page's other blocks are still placed.

The CLI uses only deterministic fake translators (`identity`, `upper`,
`overlong`, `raise-first`), so it needs no model or network:

```sh
python -m lib.paper.builds.pdf_translation \
  --source in.pdf --output out.pdf --target-language zh --translator upper
```

It exits 0 when every page passes the gate, 1 when any page fails (the report
names the page and check), and 2 for unusable input.

## HTTP entry point

`routes/paper_pkg/_pdf_translation.py` is the only HTTP caller of
`translate_pdf`. The browser entry is `frontend/src/features/paper/fulltext.ts`.

* `GET /api/v4/paper/translate/pdf/status` answers
  `{configured, reason}` from `lib/paper/builds/pdf_translation_transport.py`.
  `configured` is the durable existence of a text route (`has_capable_slots`),
  not transient cooldown. No provider is called.
* `POST /api/v4/paper/translate/pdf` accepts `{paper_id, paper_hash?,
  target_language}`. `paper_id` resolves the owner-scoped `paper_library` row;
  a supplied hash must match it, and the row's `pdf_filename` (basename under
  `PAPER_DIR`) is the source. Success is `200 application/pdf` with
  `X-Tofu-Translation-Pages`, `-Translated`, `-Refused` and `-Gate: pass`
  headers. The browser turns those bytes into one object URL and releases it
  on teardown.
* Failures are explicit and never a fake artifact: 400 invalid input, 404 no
  owner-scoped stored PDF, 409 no configured translation transport, 422 an
  unusable source or a document that failed the translation-mode gate (the
  gate and solver are unchanged).

`pdf_translation_transport.py` resolves the injected callable from the shared
model dispatch (`smart_chat`, one prompt per block) and returns `None` when no
route exists. There is no second provider transport; the route adds no model
call of its own. When the transport is absent, the panel states that the
server cannot translate and the action stays disabled.

## Verification and the fidelity gate

`tests/pdf_parser/test_pdf_render_pipeline.py` builds synthetic pages with
PyMuPDF (no real PDFs, model, network or OCR) and runs the whole pipeline:

* Identity translation, one column, two columns and a ruled table: the full
  `run_gate(source, rendered)` reproduction report is green, every plan line's
  baseline and embedded font is checked, and the report is written to
  `tmp_path/pdf-fidelity-report.json`.
* A 30 % longer translation: the solver accepts the block and rendering keeps
  every line inside its column. `run_gate(..., mode='translation')` is now
  fully green — page/column geometry, exact figure/table boxes, the translated
  block's original column band, target presence and the declared font stack —
  while the same bytes correctly fail the default reproduction mode's
  `text_presence`. That contrast is the executable specification for the
  translation contract; the longer translation is not expected to pass the
  reproduction gate.
* A block that cannot fit is refused by the solver; `RenderReport.refusals`
  carries `does_not_fit`, and the source sentence remains in the output.
* A CJK translation embeds DejaVu Sans for Latin and Noto Sans SC for Han text
  through the documented fallback stack.
* A full-width flowable on a two-column page is preserved and reported rather
  than silently dropped.
* `tests/paper/test_pdf_translation_length_stress.py` measures five synthetic
  layouts at 0.6x/1.0x/1.5x/2.2x (and an explicit-refusal 6x case) from the
  rendered PDF: page/column overflow, body-block overlap, dropped text and page
  fill are all reported per fixture and stretch factor.