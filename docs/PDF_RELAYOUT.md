# PDF translation re-layout (geometry layer)

Current-state authority for `lib/pdf_relayout/`. This layer sits between PDF
extraction (a separate owner) and a later renderer. It is pure CPU: no model,
network, PDF parsing, or pixel output.

## Problem and decision

**Problem.** Translation changes text length. A renderer that reuses the source
line breaks either overflows the block, spills into the neighbouring column, or
paints over a figure. Reflowing by changing the page layout would invalidate the
owner's requirement that a single-column page stays single-column and a
two-column page stays two-column.

**Decision.** `solve_page(layout, translations)` keeps every block's column
assignment, horizontal margins, and all fixed geometry, then reflows a column
as one contiguous run between fixed obstacles. Each run is first laid out at
the readable floor; the remaining height is then offered to blocks in reading
order, so a longer translation pushes later flowable blocks down instead of
being refused at the next block's original top. Leading and font size still
shrink only within documented readable floors, and a run that the column
cannot hold is refused explicitly; the solver never emits an overlapping plan.

**Alternatives considered and rejected.**

* Reflowing into the full column width: changes indented/centered blocks and
  can collide with a figure inset in the same column.
* Capping each block at the next flowable block's original top: makes a
  translation that is longer than the source gap an automatic refusal even when
  the column has enough room, so length compensation does not hold.
* Moving fixed figures/tables: changes what the document shows. They remain
  hard region boundaries, and flowable blocks never cross them.
* Silently dropping or clipping overflow text: loses translated content without
  a diagnosable outcome.
* Shrinking below 7 pt or leading below 1.08em: produces illegible or colliding
  type; refusal is the honest result.

## Coordinate space and input

Coordinates are PDF points with the origin at the page's top-left; `y` grows
downward. A bbox is the content band, not a padded table cell.

| Input | Meaning |
|---|---|
| `PageLayout.page` | page media box |
| `PageLayout.columns` | ordered `Column(column_id, bbox)`; ids are stable |
| `Block.block_id` | stable id, unique on the page |
| `Block.kind` | `text`, `heading`, `caption`, `list`, `formula` (flowable) or `figure`, `table` (fixed) |
| `Block.bbox` | extracted content band |
| `Block.column` | extracted column id; never changed by the solver |
| `Block.font` | requested family, size, baseline leading, weight |
| `Block.attached_to` | caption → fixed block id; the attachment is preserved |
| `translations` | `block_id → translated text`; fixed blocks need no entry |

Input faults fail fast with `LayoutInputError`: duplicate ids, a flowable block
outside its column or the page, overlapping source blocks, overlapping columns,
an unknown column, or a caption attached to a missing/non-fixed/positionally
detached element. A missing translation is a refusal, not a source-text
fallback.

## Solving rules

1. Fixed blocks are copied verbatim into the plan (`preserved_geometry=True`).
2. Each column is split into runs between fixed obstacles. Flowable blocks are
   solved top-to-bottom in each run. A block keeps its original top when that
   is below the running cursor, and is pushed down by the preceding translation
   otherwise. Original left/right margins stay clipped to the column, so a
   block can never leave its column.
3. The downward limit is the nearest of: the column bottom, or a figure/table
   whose horizontal span overlaps the column and whose top is at or below the
   run's top. A 4 pt clearance is kept. Later flowable blocks are not limits;
   they move within the run.
4. A caption attached to a figure/table preserves its original gap to that
   element (minimum 2 pt). A caption above its figure therefore cannot grow into
   it; if it cannot fit, it is refused.
5. Text is tokenized with Latin word breaks, break-after `-`/`/`/dashes, and
   per-CJK-character breaks. CJK opening/closing punctuation is kept with its
   neighbour (kinsoku). Overlong tokens are split at character boundaries so
   reflowed lines stay within the block width.
6. A run is measured at the readable floor first. Any spare run height is then
   available to blocks in reading order: a block keeps the source font size and
   reduces leading in 0.02 steps down to `MIN_LEADING_RATIO`, then reduces the
   font size in 0.25 pt steps down to `MIN_READABLE_FONT_SIZE_PT`. Type is never
   enlarged, and growth consumed by one block reduces the spare height offered
   to later blocks. If even the floor layout does not fit, the longest prefix
   that stays above the first refused block's original rectangle is placed and
   the rest are refused, so kept source text cannot be overprinted.
7. The accepted plan is re-validated before return: fixed geometry unchanged,
   column assignment unchanged, every flowable box inside its column, and no
   flowable box intersecting any fixed element or another flowable box. A
   violation is a solver bug (`LayoutInvariantError`), not a user-visible
   refusal.

## Documented limits

| Constant | Value | Reason |
|---|---|---|
| `MIN_READABLE_FONT_SIZE_PT` | 7.0 | smallest legible print size at 100% zoom |
| `MIN_LEADING_RATIO` | 1.08 | below this ascenders/descenders collide |
| `BLOCK_GAP_PT` | 4.0 | minimum separation from the next obstacle |
| `CAPTION_MIN_GAP_PT` | 2.0 | caption stays visibly attached |

`minimum_readable_size_for(font)` is `min(source size, 7 pt)`: a block that is
already smaller than 7 pt is not shrunk further.

## Refusals

`LayoutRefusal` is explicit and machine-readable:

* `does_not_fit` — even at the minimum size/leading the text is taller than the
  available band. The message names the required and available heights, the
  minimum font/leading, and the limiting element (figure, table, block, or
  column bottom).
* `missing_translation` — no translated text was supplied.

Refused blocks have no placement. Other blocks remain placed and validated.

## Font stack

Faces are measured with fontTools advances from the actual installed files.
Each character is resolved independently, so mixed Latin/CJK text can switch
faces mid-line. Order is fixed:

| Script / style | Fallback order |
|---|---|
| Latin sans | DejaVu Sans → Lato → Noto Sans SC |
| Latin serif | DejaVu Serif → Lato → Noto Sans SC |
| Latin mono | DejaVu Sans Mono → Noto Sans SC |
| CJK (Han, kana, Hangul, CJK punctuation/fullwidth) | Noto Sans SC → DejaVu Sans |

Source family names are mapped to a style (`serif`/`mono`/`sans`); an
unavailable embedded family never becomes a private font loader. Faces are
discovered under `/usr/share/fonts`, `/usr/local/share/fonts`, `~/.fonts`, and
`~/.local/share/fonts`; `font_dirs` exists for hosts that mount fonts elsewhere.
Only regular and bold faces are admitted (italic, light, thin, condensed faces
are skipped). `missing_stack_families()` reports any family absent from the
host, and `PagePlan.missing_glyphs` lists characters no curated face covered.
Neither is silently ignored.

## Tests

The renderer consumes this plan; see
[`PDF_TRANSLATION_RENDER.md`](PDF_TRANSLATION_RENDER.md).

`tests/pdf_relayout/test_pdf_relayout_solver.py` covers: 30% single-column
growth extending within the column at the original font size; two-column text
not bleeding across the gutter; refusal rather than overlap of a figure;
refusal of a caption that would detach from its table; and a mixed CJK/Latin
case that exercises the Noto Sans SC → DejaVu Sans fallback. Accepted and
refused cases both assert unchanged column assignment and no flowable/figure
intersection.

`tests/paper/test_pdf_translation_length_stress.py` drives the full
pipeline on five synthetic born-digital layouts at 0.6x/1.0x/1.5x/2.2x with
deterministic fake translators and measures page/column overflow, body-block
overlap, dropped text and page fill on the rendered PDF. A 6x run asserts the
explicit-refusal path. Nothing in that suite uses a model, network or OCR.

## Non-goals

No PDF reading, glyph rasterization, PDF writing, hyphenation dictionaries,
model calls, or network access. Rendering lives in `lib/pdf_parser/render.py`
and consumes the plan's `PlacedLine`/`TextRun` data.