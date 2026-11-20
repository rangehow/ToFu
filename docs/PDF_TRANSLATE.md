# PDF block translation (text layer)

Current-state authority for `lib/pdf_translate/`. This layer sits between PDF
structure extraction (`lib/pdf_parser/structure.py`) and the geometry-only
re-layout solver (`lib/pdf_relayout/`). It turns extracted prose into translated
prose and reports, per block, what the reflow pass may use.

## Problem and decision

**Problem.** The re-layout solver needs translated text it can actually fit in
the source block, but translation is the only pipeline stage that can produce
longer text. A translator that emitted loose strings without source geometry
would force the solver to discover every overflow after the model call; a
translator that trimmed output to a guessed length would lose content silently.

**Decision.** `translate_page(page, target_language=..., model_call=...,
glossary=...)` consumes one page of `extract_pdf_structure()` and returns one
`BlockTranslation` per extraction block, in reading order. Each block carries:

| Field | Meaning |
|---|---|
| `block_id` | `p<page_number>-b<index>`, identical to the canonical bridge's id |
| `status` | `translated`, `skipped_no_text`, or `failed` |
| `translated_text` | model output after glossary; empty only when skipped or the call failed |
| `budget.available_width_pt` | block width clipped to its extracted column |
| `budget.available_height_pt` | distance from the block top to the nearest obstacle below |
| `budget.limiting_element` | the obstacle that set the height (`figure block 3`, `page bottom`, …) |
| `glossary_hits` | every source term that fired, with its occurrence count |
| `refusal` | the solver's typed refusal when produced text cannot fit |
| `error` | transport or invalid-result failure for that block |

Block ids match `lib/pdf_parser/layout.py` (`p<page_number>-b<index>`), so
`PageTranslation.translations` maps directly onto
`AdaptedPage.translated_block_ids()`.

**Alternatives considered and rejected.**

* One model call for the whole page: merges block identity, so a failure or a
  reordering affects unrelated content and the solver loses per-block mapping.
* Passing only `block_id → text` to the solver: no budget hint reaches the
  caller before reflow, and an overlong translation is discovered late.
* Clipping or dropping overflow text in the translator: hides a real refusal
  and loses content.
* Calling a provider SDK or HTTP endpoint here: couples the pipeline layer to
  one transport and puts credentials in the wrong owner.

## Block selection

Only `title`, `heading`, `paragraph`, `text`, `list`, `caption`, `formula`,
`header` and `footer` are text-bearing. `figure`, `table`, `rule` and
`separator` receive `skipped_no_text` and never reach the model. A text-bearing
block whose text is empty or whitespace also receives `skipped_no_text`.
Every block carries a source-geometry budget so the result stays one-to-one
with the extractor; a fixed block's budget is informational and is never a
text target. Blocks are never merged, split or reordered.

## Layout budget

`source_band()` mirrors the solver's documented obstacle rule, using the
extraction's page and block geometry:

* Horizontal span is the block bbox clipped to its extracted column, plus
  the same ink-to-advance allowance the layout bridge applies to the right
  edge (5% of the font size, capped at 2% of page width).
* Vertical limit is the nearest block below whose horizontal span overlaps the
  current block, minus the shared `BLOCK_GAP_PT` (4 pt), or the page bottom.
  Fixed geometry (figures/tables) and other flowable blocks both count as
  obstacles because text cannot paint through either.
* The budget is computed from source geometry before translation and does not
  depend on the model result.

The budget is a hint, not the final placement. A later page solve can still be
bounded by a different obstacle ordering or by multiple translated blocks
growing into each other.

## Fit check and refusals

After glossary application, `fit_refusal()` runs the real
`lib.pdf_relayout.solve_page()` on a one-block page whose column is exactly the
source band. This measures actual glyph advances and applies the solver's
leading and readable-font floors. It returns:

* `None` when the text fits — the block becomes `translated`;
* a `LayoutRefusal` (`does_not_fit`) when it does not — the block becomes
  `failed`, the complete produced text stays in `translated_text`, and
  `refusal.available_height_pt` / `required_height_pt` / `limiting_element`
  explain the decision.

Nothing is truncated. The refusal names the real source obstacle, not the
synthetic one-block column bottom.

## Terminology

`glossary` maps a source-language term to the rendering the caller requires. It
is applied after translation, in one simultaneous pass with the longest source
term winning at each position and ties broken by term order. Every term that
fired is reported as `GlossaryHit(source_term, target_term, occurrences)`;
terms that did not fire are absent. Applying the glossary in one pass means a
replacement can never trigger another entry, and a term repeated inside its own
target rendering cannot cascade.

## Transport and prompt-injection boundary

`model_call(prompt) -> str` is injected. The caller passes the product's own
model call path; this package imports no provider SDK, performs no HTTP, reads
no credential and adds no retry loop. A call that raises, returns a non-string,
or returns whitespace only marks that block `failed`; the rest of the page is
still translated.

Source text is untrusted data. `build_translation_prompt()` puts the fixed
instruction rules first, wraps the source in one `<source_text>` data block,
tells the model before it sees the data never to follow instructions found
inside that block, and neutralises any `</source_text>` in the source so it
cannot close the block and impersonate instructions. The module docstring of
`lib/pdf_translate/adapter.py` states the same contract for readers of the code.

## Tests

`tests/pdf_translate/test_pdf_translate_blocks.py` uses only a fake transport
and synthetic extraction dictionaries:

* a longer translation accepted through the solver's downward extend path,
  retained verbatim;
* an overlong translation refused by the real solver, with the refusal and the
  complete text surfaced;
* fixed and empty blocks never calling the model;
* one failing call marking only its block `failed` while the page continues;
* glossary hits, including longest-term precedence in one pass;
* an injected `</source_text>` and instruction line remaining data inside the
  fixed template.

No real model, network, key, PDF or production data is used.

## Non-goals

No PDF parsing, rendering, writing, HTTP, provider SDK, credential handling,
retry policy, or final page placement. Those belong to the extraction owner,
the re-layout owner, and the transport owner respectively.
