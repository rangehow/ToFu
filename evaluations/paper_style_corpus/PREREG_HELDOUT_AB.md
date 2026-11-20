# Pre-registration: held-out A/B of the shipped paper-style checklist

Registered 2026-11-18 by the commit that adds this file. That commit
precedes every writer or judge call on the units below. Results go to
`validation_heldout_ab.json`. The interpretation goes to
`docs/PAPER_STYLE_EVIDENCE.md`. Nothing in this file changes after the
registering commit.

## Question

Does revising a paper's title/abstract/introduction with the 5 shipped
checklist items produce a better text than revising it without them, on
drafts that played no part in selecting those items? The 5 items are
`paper.style.abstract-number`, `paper.style.intro-quantified`,
`paper.style.caption-takeaway`, `paper.style.limitations-section` and
`paper.style.intro-findings-first-person`.

## Units (50, frozen)

`prereg_heldout_ab.json` lists every unit with the SHA-256 of its exact text.
Snapshot copies are under `data/paper-style-corpus/heldout_ab/units/`, and
`validate.py heldout` refuses to run on a unit whose hash differs.

- **8 own drafts.** None of these papers is among the 26 selection units. Each
  paper appears once, and older copies of the same paper are excluded:
  - `knowledge/paper/main.tex` (recovery)
  - `knowledge/paper/allocation`
  - `knowledge/paper/repair`
  - `card/CARD_iclr2027`
  - `parallel/paper` (parallelism)
  - `parallel/papers/main_c4l`
  - `parallel/papers/paperA`
  - `parallel/papers/paperB`

  The unit is title + abstract + the first body section, with `\input` inlined
  (`draft_unit`).
- **42 held-out accepted-ordinary papers.** These are positions 40–81 of
  `held_out_units` (test split, hash order). Positions 0–15 were the per-rule
  A/B units and 16–39 the 24 ablation/selection units; all 42 excluded ids
  are listed in the JSON, and their overlap with the study units is asserted
  to be empty. The unit is abstract + introduction in plain text.
- **Freshness caveat, stated in advance.** The test split entered the corpus
  analysis only in aggregate:
  - as matched controls in the held-out same-sign check behind "supported"
    findings;
  - in the separation AUC.

  No study unit was revised, judged in a draft A/B, or used to pick, drop or
  word an item.

## Arms

The writer is `gpt-5.6-terra` at temperature 0. The prompt is
`registry_revise_messages`: plain text for accepted papers, LaTeX for own
drafts. No outline is used, and none ships.

- **A:** the registry's base paper rules (`rules_for('paper')`, lint +
  checklist, no `paper.style.*`).
- **B:** A plus the 5 items, exactly as the writer reads them from
  `style_rules.json`.

## Judging

The pipeline's rule-free judge is used: `pipeline._judge_pair` wrapped in
`critic.blind_pairwise`.

- Each unit is judged in **both presentation orders**, and rubric totals
  decide each order.
- B is preferred only if it wins both orders, and A likewise. Otherwise the
  unit is a split.
- The brief carries only the judge model, as in the selection runs (no task
  line).
- Primary judge: `gpt-5.5`. Second judge: `deepseek-v4-pro`. Each is cached
  under its own judge name and model.

## Tests and success criterion

1. **Primary:** an exact two-sided sign test over units, with B-preferred vs
   A-preferred counts and splits dropped (`gpt-5.5`). It passes if
   **B > A and p < 0.05**.
2. **Second judge:** `deepseek-v4-pro` must point the same direction
   (B-preferred > A-preferred). No significance is required.
3. **Provenance gate:** **zero unsourced numbers in arm B** over all 50
   units.
   - The registered gate is `unsourced()`: an unsourced number is
     `numbers_in(strip_latex_arguments(revision))` minus `numbers_in(source
     unit)`. Every digit run is counted, so range ends, negative values and
     comma groups are included. A rounded or re-expressed value counts as
     unsourced, which makes this check conservative.
   - Citation keys (`\cite*`) not in the source are reported. They are not
     part of the criterion, because held-out units are plain text.
   - This is an equivalent of assignment 0c8f48f1's gate, which had not
     landed at registration. If its `lib.longform.documents.provenance.check_texts`
     lands before reporting, its result is reported beside this one, for
     information only.

**Pass = 1 and 2 and 3.**

- **If it passes,** the 5 items are marked proven in `style_rules.json` and
  the docs.
- **If it fails,** they stay reviewer prompts with no quality claim, and the
  docs say so.

## Reporting

Report all of the following:

- W/T/L, sign-test p, mean outcome (+1/0/−1) and rubric margin
  (B − A totals over both orders), each with a 2000-draw seeded bootstrap
  95% CI, for both judges.
- A per-unit table: verdict, position bias and totals per judge.
- The provenance gate per arm.
- Own-draft and accepted-paper subgroups, as description only.

## Fixed handling

- A writer call that fails, or a judge call that still fails after the
  harness's 3 tries, aborts the run. A rerun replays the cache and repeats only the missing calls.
- No unit is dropped, replaced or added.
- No other judge, item set, unit or test is substituted after this commit.
