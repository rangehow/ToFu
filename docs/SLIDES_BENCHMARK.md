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
- **Arms**, identical input text and the same model (`gpt-5.5`) through the
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

## Where Tofu lost, by stage

1. **Delivery (export gate), decisive.** All six Tofu decks were refused at
   `layout_qa`, so the arm shipped nothing while its refused pages out-scored
   both competitors. Most findings were 2–4 px "overflows" and "collisions"
   that are the font's half-leading deficit (content area taller than the
   authored line height): invisible and unfixable by moving boxes. The
   height-growth repair then split the PowerPoint reserve when centring a
   box. Fixed (findings left at the gate: 7–60 per deck before, 1–5 after); the
   remaining ones are real overflows that the
   LLM repair does not resolve in two attempts. That repair loop is the next
   loss to remove.
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
