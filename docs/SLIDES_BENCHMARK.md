# Slides benchmark: Tofu vs open-source deck generators

Owner: `lib/slides` (production recipe). Harness: `evaluations/slides_bench/`.
Decisions taken from these results live in
[`modules/production.md`](modules/production.md) ("Content grounding and delivery").

## What is measured

- **Corpus** (`evaluations/slides_bench/corpus.json`): six documents of the
  owner's kind, each pinned by sha256: two public arXiv papers (SWE-bench,
  2310.06770; Lost in the Middle, 2307.03172) and four private Chinese
  technical write-ups. Private entries are listed only in
  `<data root>/corpus.private.json`; their titles, paths, text, decks and
  screenshots never enter git. The data root is `SLIDES_BENCH_DATA`
  (default `<shared checkout>/data/ppt-bench`, gitignored).
- **Arms**, identical input text (Tofu received only the first 20,000
  characters until the source-coverage change below) and the same model
  (`gpt-5.5`) through the
  running server's model routing (`llm_bridge.py`, `openai_proxy.py`; never
  the canvas platform): Tofu's recipe (`run_tofu.py`, web research and image
  generation disabled for parity), PPTAgent (icip-cas) and SlideDeck AI
  (`competitors/`). Presenton was not run: its supported install is a Docker
  image and this host has no container runtime.
- **One renderer**: every PPTX goes through the same LibreOffice headless
  build to per-slide PNG (`render.py`).
- **Blind judge** (`score.py`): a vision model sees every slide image of one
  anonymous deck (label is a seeded hash, arm order is seeded-shuffled per
  document) plus the source, and scores 1–10 on the written rubric:
  specificity, faithfulness, takeaways, coverage, no_filler, narrative
  (content) and visual_design, layout_readability (visual).
- **Text audit** (`score.py`): slide text is split into statements and
  classified specific / generic / unsupported / filler against the source;
  also the share of assertion titles and visible numbers absent from the
  source.
- **Delivery**: a Tofu deck refused by a quality gate is undelivered. Its
  authored pages are force-exported (`force_export.py`) and judged so content
  can still be compared, but they never enter delivered averages.

Reproduce: `sh evaluations/slides_bench/run_arm_all.sh <run> <arm> [docs]`
per arm, then `python -m evaluations.slides_bench.evaluate <run>`.

## Results (2026-11-16, one deck per document and arm)

Judge means over all six documents (Tofu: forced exports, see Delivery).

| | Tofu before | Tofu after | PPTAgent | SlideDeck AI |
|---|---|---|---|---|
| delivered | 0/6 | 0/6 | 6/6 | 6/6 |
| specificity | 8.17 | 8.83 | 7.33 | 7.17 |
| faithfulness | 8.50 | 8.50 | 8.17 | 7.33 |
| takeaways | 8.00 | 8.33 | 6.33 | 6.83 |
| coverage | 7.17 | 7.83 | 6.50 | 6.00 |
| no_filler | 7.67 | 9.00 | 6.67 | 5.33 |
| narrative | 8.17 | 8.17 | 7.00 | 7.00 |
| visual_design | 7.83 | 7.50 | 3.00 | 4.00 |
| layout_readability | 6.67 | 6.67 | 3.83 | 2.83 |
| specific statements / slide | 8.71 | 12.06 | 3.93 | 4.24 |
| specific share | 0.72 | 0.90 | 0.85 | 0.64 |
| unsupported / slide | 0.57 | 0.25 | 0.15 | 0.33 |
| filler / slide | 0.74 | 0.07 | 0.01 | 0.76 |
| assertion-title share | 0.78 | 0.95 | 0.15 | 0.64 |
| numbers not in source / deck | 13.7 | 10.8 | 0.17 | 1.5 |
| layout findings left at the gate | 7–60 | 1–5 | — | — |

Limits: one deck per cell and one judge pass; the competitor decks were
judged once and the cached verdict is reused in the "after" run. The "before"
Tofu decks were built over the day from the then-current tree, not one pinned
revision. "Numbers not in source" counts axis ticks and labels as numbers.

## Results after layout repair to a fixed point (re-judged in one pass)

The layout stage now repairs its own overflows (re-layout, measured copy
tightening; [`modules/production.md`](modules/production.md), "Layout repair
to a fixed point"); the gate is unchanged. Four fresh Tofu runs over the six
documents at successive revisions:

| run | passed `layout_qa` (unchanged gate) | delivered, visual QA skipped (as in the runs above) | delivered, production visual QA enforced |
|---|---|---|---|
| fit | 4/4 reaching it (2 refused earlier by the author gate) | — | 3/6 |
| fit2 | 6/6 | 6/6 | 4/6 |
| fit3 | 4/6 (both residuals fixed afterwards) | — | 2/6 |
| fit4 (final) | 5/6 | 5/6 (exported, package QA ok) | 2/6 |

The Tofu arm previously never ran visual QA (the benchmark process has no
dispatcher slot); it now runs production visual QA through the same bridge
(`run_tofu.py`). Its refusals vary run to run and are mostly not layout
(chart scales across two charts, a one-character title orphan, title
qualifiers, a page count read from the source); it also cannot parse 5–10
of its own replies per deck, because the 1500-token output budget is spent
on gpt-5.5 reasoning (`reasoning_tokens == max_tokens`, empty content), so
pages go unreviewed and visual-repair re-checks fail. Fixed in the next
section.

Blind judge, seed `slides-bench-v2`: every arm's decks re-judged in this one
pass, shuffled and anonymised; Tofu-prev is the previous run's refused
"after" decks. Means over all six documents (Tofu: delivered and refused
decks alike, as above):

| | Tofu fit4 | Tofu-prev | PPTAgent | SlideDeck AI |
|---|---|---|---|---|
| specificity | 8.83 | 8.83 | 7.50 | 7.33 |
| faithfulness | 8.83 | 8.67 | 7.67 | 7.50 |
| takeaways | 8.50 | 8.67 | 6.00 | 6.50 |
| coverage | 7.67 | 7.67 | 6.83 | 6.17 |
| no_filler | 8.83 | 8.67 | 6.50 | 5.50 |
| narrative | 8.17 | 8.33 | 7.00 | 6.83 |
| visual_design | 8.00 | 8.00 | 3.00 | 4.00 |
| layout_readability | 7.00 | 6.83 | 3.50 | 2.83 |
| content mean | 8.47 | 8.47 | 6.92 | 6.64 |
| visual mean | 7.50 | 7.42 | 3.25 | 3.42 |
| specific statements / slide | 10.06 | 11.33 | 3.99 | 4.68 |
| unsupported / slide | 0.17 | 0.28 | 0.21 | 0.44 |
| filler / slide | 0.07 | 0.17 | 0.03 | 0.70 |
| assertion-title share | 0.93 | 0.97 | 0.17 | 0.67 |
| numbers not in source / deck | 9.5 | 10.8 | 0.17 | 1.5 |

Limits: one deck per cell, one judge pass, n=6 per arm; differences of
0.2 are within one judge point on one deck. Tightened copy costs some
density (specific statements per slide 11.3 → 10.1) but not judged
specificity or coverage. One private document in fit4 kept one finding on a
component-expanded element, which the re-layout does not edit.

## Results with the visual review enforced (reasoning budget and review rules fixed)

The visual review now asks for the route's output limit and no longer
runs out of budget ([`modules/production.md`](modules/production.md),
"Visual review on reasoning models"). With every page actually reviewed, its
refusals were investigated and its rule bugs fixed: legibility judged on
contact-sheet thumbnails, deck pagination read as a source claim, missing
evidence for a page's own numbers, decimals misread from downscaled pixels,
and a deck rollback that discarded verified page fixes. Real defects got
repairs, never a looser check: visual repair runs to a fixed point;
widows and split words are re-broken by measurement; text running out of
its card is a layout finding; `muted` meets WCAG AA in every theme. Full
Tofu runs over the six documents (same model, renderer, gates):

| run | change under test | visual-review replies empty or cut off | delivered |
|---|---|---|---|
| fit4 (before) | — | 41 | 2/6 |
| fit5 | reply budget = route output limit | 0 | 0/6 |
| fit6 | contact sheet scoped to deck-level checks | 0 | 2/6 |
| fit7 | widow repair | 0 | 2/6 |
| fit8 | number evidence; deck rollback weighs pages | 0 | 1/6 |
| fit9 | visual repair to a fixed point | 0 | 2/6 |
| fit10 | card containment; WCAG muted | 0 | 2/6 |
| fit11 | split-word repair | 0 | 3/6 |
| fit12 (final, `b528831ec9`) | review reads authored page text | 0 | 3/6 |

fit5 refused all six decks: actually reviewed, every deck drew
contact-sheet `typography` majors (thumbnail legibility). The target of
5/6 delivered is **not met**. Replaying a refused deck's visual-QA stage
from its checkpoint shipped two of four decks in fit7 and one of three in
fit6 with no code change, so a deck's delivery still turns on single review
samples. The final residuals are real authored-geometry and content defects:
a line or ellipse stroke drawn through text, markers off a chart's
vertices, leader lines ending in empty space, and a percentage attributed to
the wrong bar segment. Their repair is the next loss to remove.

Blind judge, seed `slides-bench-v2`, one pass over 24 decks (fit12 Tofu,
the fit4 Tofu decks as Tofu-prev, PPTAgent, SlideDeck AI), shuffled and
anonymised. Means over all six documents, delivered and refused decks alike:

| | Tofu fit12 | Tofu-prev (fit4) | PPTAgent | SlideDeck AI |
|---|---|---|---|---|
| delivered | 3/6 | 2/6 | 6/6 | 6/6 |
| specificity | 8.67 | 8.83 | 7.33 | 7.17 |
| faithfulness | 8.33 | 9.00 | 7.67 | 7.83 |
| takeaways | 8.00 | 8.50 | 5.83 | 6.67 |
| coverage | 7.17 | 7.67 | 6.83 | 5.67 |
| no_filler | 8.17 | 8.67 | 6.67 | 5.17 |
| narrative | 8.33 | 8.33 | 7.00 | 6.50 |
| visual_design | 7.67 | 7.50 | 2.83 | 3.83 |
| layout_readability | 6.67 | 6.83 | 3.67 | 2.83 |
| content mean | 8.11 | 8.50 | 6.89 | 6.50 |
| visual mean | 7.17 | 7.17 | 3.25 | 3.33 |
| specific statements / slide (delivered) | 12.34 | 11.05 | 3.79 | 4.22 |
| unsupported / slide (delivered) | 0.44 | 0.15 | 0.24 | 0.34 |

Visual scores are equal; **content scores are lower** (8.11 vs 8.50),
mostly one deck: one private document scored 7.6 against 8.6, with 0.88
unsupported statements per slide. The changes in this round touch review and repair,
not outline or authoring, and with one deck per cell this run cannot
separate generation variance from repair edits. The fixed-point visual
repair rewrites pages more often than the single attempt did, and whether
those rewrites shed content is unmeasured. Limits: one deck per cell, one
judge pass, n=6 per arm; a 0.2 difference is one point on one deck.

## Drawn geometry and reproducible refusals (2026-11-17)

The changes are in [`modules/production.md`](modules/production.md), "Drawn
geometry, fact-keeping repairs and reproducible refusals". Every Tofu run
now also keeps its authored deck from before the layout and visual repair
stages (`tofu-authored/<doc>`, `run_tofu.py`). Each generation is
therefore its own repair-off control (`repair_effect.py`). All runs use
the same model, renderer and gates, with the visual review enforced.

**Do the repair edits cost content?** No. The judge scored each
generation's deck before and after repair, under two seeds
(`slides-bench-v2`, `-v3`).

| | value |
|---|---|
| generations (6 documents x 5 runs: rep-a, rep-b, fix18-fix20) | 30 |
| blind content, after repair minus before, mean (95% bootstrap CI over generations) | +0.02 (-0.04, +0.08) |
| blind visual, after minus before | -0.02 (-0.16, +0.13) |
| visible numbers the repair removed, per generation | 0.10 |

One repair removed numbers: in rep-b, built before the number-keeping
rule, a visual repair rewrote a title and replaced a cited counter-example
(three numbers). A repair candidate that drops a
number its feedback did not dispute is now rejected. fix18-fix20 lost no
numbers.

**Delivery** (decks shipped with every gate enforced):

| run | change under test (cumulative) | delivered | refusals (one finding each) |
|---|---|---|---|
| rep-a, rep-b | fit12 code + authored snapshot | 5/6, 3/6 | table line under a caption, title broken between a number and its unit, leader arrow to empty space |
| fix13 | strokes and chart overlays measured, rules moved off text, refusal must reproduce | 4/6 | pale text, marker off a hand-drawn curve |
| fix14, fix15 | repairs keep numbers, sketched curves, WCAG box text | 4/6, 4/6 | caps label refused though the evidence names it, arrows to nothing, cross-page number conflict |
| fix16, fix17 | arrows must point at an element (short ones extended) | 3/6, 2/6 | an automatic arrow extension ran into the wrong item; a literal tag-like word dropped by the renderer |
| fix18, fix19 | arrow extension removed (author ends arrows) | 4/6, 4/6 | conflicting source excerpts, serif title font, table header contrast |
| fix20 | caps labels the evidence names, unknown rich-text tags, conflicting excerpts | 5/6 | two similar dark series colours |
| fix21 (final, the landed code) | table cell contrast | **5/6** | chart legend text clipped |

The target of at least 5/6 is met by the final full run and the run
before it. Single runs still vary by one or two decks. Each remaining
refusal is a different real defect: legend clipping, series colour
contrast, font mixing. None of them repeated across runs.

The repeated review for a refusal (no new knob) cleared one refusal in
each of fix15, fix16, fix17 and fix19, out of 18 refused review results in
those runs. Each cleared one had not reproduced on identical input.

**Blind scores.** Each pass re-judged the fit4 decks (Tofu-prev, the
previous accepted run) next to the new decks under the same two seeds.
Means are over all six documents:

| run | seed | Tofu content | Tofu visual | fit4 content | fit4 visual | PPTAgent content / visual (v2, v3 where they differ) | SlideDeck AI content / visual |
|---|---|---|---|---|---|---|---|
| fix18 | v2 / v3 | 8.37 / 8.40 | 7.42 / 7.25 | 8.53 / 8.40 | 7.17 / 7.50 | 6.87 / 3.25 | 6.50 / 3.33 |
| fix19 | v2 / v3 | 8.20 / 8.30 | 7.00 / 7.33 | 8.53 / 8.37 | 7.17 / 7.08 | 6.87 / 3.25 | 6.50 / 3.33 |
| fix20 | v2 / v3 | 8.43 / 8.40 | 7.42 / 7.25 | 8.53 / 8.50 | 7.17 / 7.42 | 6.87 / 3.25 | 6.50 / 3.33 |
| fix21 | v2 / v3 | 8.23 / 8.17 | 7.17 / 7.17 | 8.53 / 8.30 | 7.17 / 7.25 | 6.87 / 3.25, 7.03 / 3.08 | 6.50 / 3.33, 6.37 / 3.33 |

Paired per document against the fit4 deck in the same pass (24 pairs,
fix18-fix21, seeds averaged):

- **Visual:** tied, +0.01 (-0.11, +0.16).
- **Content:** lower, -0.15 (-0.26, -0.05).

The content gap is not the repairs: the repair-off decks score the same.
It comes from generation and concentrates in pub-lostmiddle (fix21:
-1.1). The judge says the deck states Figure 5 qualitatively and omits
the key-value retrieval numbers. That is an outline and source-coverage
loss, which this round did not change; it is the next loss to remove. The
checks against the previous run (content 8.47, visual 7.50) are **not
met**:

- fix21 content is 8.20 against 8.42 for the fit4 decks in the same pass.
- fix21 visual is 7.17 against 7.21 in the same pass. That is within
  judge noise, but below the 7.50 recorded for fit4 when it was judged
  alone.

Both competitors stay far behind.

Limits:

- Runs were built from this change's pre-landing commits; fix21 ran
  the landed code.
- One deck per document per run, and two judge seeds.
- The competitor and fit4 verdicts for seed v2 are cached from fit12.
- The network volume dropped some verdict writes with remote I/O errors;
  those were re-judged, never copied.

## Source coverage (2026-11-17)

The change is in [`modules/production.md`](modules/production.md), "Source
coverage at the outline and authoring stage". The paper deck that lost
content in fix21 (Figure 5 described in words, key-value retrieval
numbers missing) had been given only the first 20,000 of the paper's
65,433 characters: the material contract cut it. The key-value results
start at character 24,457, and the appendix tables holding Figure 5's
values at 63,593. The SWE-bench paper (152,932 characters) and one
private write-up (26,270) were also cut; the judge and both competitors
always read the whole text. Material is now taken whole, the outline is
checked against an inventory of the source's headline results and
captioned figures, and every page that carries one must show its numbers.

**Delivery** (all gates enforced, same model and renderer):

| run | change under test (cumulative) | delivered | refusals |
|---|---|---|---|
| cov1 | whole material, inventory, figure index, outline fill, author coverage gate | 2/6 | 3 author-gate fallbacks: a page sharing a small integer with ten results was assigned all of them; a 7-series legend clipped |
| cov2 | carried = cites the result's own units; preview legend wraps, line categories labelled | 4/6 | a savings bar drawn like a cost; bar labels far from their bars; table rows over a footnote |
| cov3 | inventory ids kept off slides; bar categories on the left; `table_overflow` | 4/6 | caption over a table's first row; series in identical colours; a flow-chart semantics finding |
| cov4 | text over a table, series sharing a colour | stopped | superseded by the next change before it finished |
| cov5 (final, the landed code) | inventory holds at most one result per content page | **6/6** | none |

cov2 was judged too: content +0.17 (95% CI -0.10, +0.40) but visual
-0.29 (-0.50, -0.08) against the fit4 decks. The uncapped inventory had
listed 25-47 results for 9-12 page private decks, and carrying them all
crammed several results onto every page ("tables and footnotes too
dense" in every judge summary). cov5 bounds the inventory by the deck's
content pages.

**Blind scores, cov5.** One pass over 24 decks per seed (cov5 Tofu, the fit4
decks as Tofu-prev, PPTAgent, SlideDeck AI), seeds `slides-bench-v2` and
`-v3`, every verdict computed fresh in this pass (no cache copied).
Means over six documents and both seeds:

| | Tofu cov5 | Tofu-prev (fit4) | PPTAgent | SlideDeck AI |
|---|---|---|---|---|
| delivered | 6/6 | 2/6 | 6/6 | 6/6 |
| specificity | 9.00 | 8.75 | 7.58 | 7.42 |
| faithfulness | 8.83 | 8.83 | 8.00 | 7.50 |
| takeaways | 8.58 | 8.58 | 6.17 | 6.92 |
| coverage | 8.00 | 7.50 | 6.83 | 5.75 |
| no_filler | 8.58 | 8.83 | 6.92 | 5.75 |
| narrative | 8.25 | 8.33 | 7.17 | 6.67 |
| visual_design | 7.83 | 7.67 | 3.00 | 3.75 |
| layout_readability | 6.83 | 6.75 | 3.58 | 3.08 |
| content mean (first five, as the paired gap) | 8.60 | 8.50 | 7.10 | 6.67 |
| visual mean | 7.33 | 7.21 | 3.29 | 3.42 |

Paired per document against the fit4 deck in the same pass (6 pairs,
seeds averaged, percentile bootstrap; `repair_effect.py cov5 --arms
tofu-prev,tofu`):

- **Content:** +0.10 (95% CI -0.10, +0.30). With narrative included,
  +0.07 (-0.11, +0.25). The paper deck that lost 1.1 in fix21 gains
  +0.4; coverage, the dimension this change targets, rises 7.50 to 8.00.
- **Visual:** +0.12 (-0.25, +0.50).
- **Repairs:** after minus before the layout and visual repair stages,
  content -0.07 (-0.15, +0.02), visual +0.12 (-0.08, +0.33); no visible
  number lost.

Limits:

- One run (6 pairs), so the CIs are wide; fix18-fix21 had 24 pairs.
- One deck per document, two judge seeds.
- cov1-cov3 ran intermediate commits of this change, cov5 the landed
  code.
- One seed-v3 audit reply was cut off mid-JSON; rerunning the evaluation
  recomputed only that cell.

## Where Tofu lost, by stage

1. **Delivery (export gate), decisive.** All six Tofu decks were refused at
   `layout_qa`, so the arm shipped nothing while its refused pages out-scored
   both competitors. Most findings were 2–4 px "overflows" and "collisions"
   that are the font's half-leading deficit (content area taller than the
   authored line height): invisible and unfixable by moving boxes. The
   height-growth repair then split the PowerPoint reserve when centring a
   box. Fixed (findings left at the gate: 7–60 per deck before, 1–5 after); the
   remaining ones were real overflows that the
   LLM repair did not resolve; the layout stage now repairs them itself
   (results above).
2. **Source reading.** Supplied material was one raw blob next to topic web
   research; pages drifted to generic headings and derived numbers. Now
   verbatim `[M#]` evidence units, cited per page.
3. **Outline.** A forced layout-variety rule produced decorative charts with
   no data (judge: "schematic replacements for real plots"). Removed; the
   outline plans an assertion per page around the strongest results.
4. **Page writing.** Filler and meta labels ("读法", "要点"), invented
   numbers, text below projection size. Now rejected by the author gate.
5. **Export.** Negative bar values rendered as positive in LibreOffice
   (missing `c:invertIfNegative`). Fixed.

PPTAgent's strength is faithfulness from copying source sentences (high
specific share, few unsupported claims) at the cost of topic titles and
text-only template slides; SlideDeck AI is generic and overflows its layouts.
Neither builds charts from the source numbers; Tofu does, which is most of
its visual lead.
