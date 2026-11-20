# Paper style evidence

Authority record for the evidence behind the paper-writing pipeline's style
rules: `evaluations/paper_style_corpus/` (corpus, extraction, annotation,
analysis, distillation, validation), the generated
`lib/longform/documents/style_rules.json` (loaded by
`lib/longform/documents/rules.py::rules_for`) and the shared measurements in
`lib/longform/documents/style_metrics.py`. The pipeline itself is owned by
[DOCUMENT_PIPELINE.md](DOCUMENT_PIPELINE.md); this document only justifies the
`paper.style.*` rules and the per-type skeletons.

## Answer (2026-11-18, powered re-validation)

**The style evidence is real; a measurable writing benefit from turning it
into rules is not.** Award and highly cited papers do differ from matched
ordinary papers (the supported findings below replicate on held-out pairs),
but neither validation shows that the distilled rules make a judge separate
better papers or make a revised draft better:

- *Separation.* With three judge samples per paper and arm and a bootstrap
  that resamples whole venue-years, the checklist changes the held-out AUC by
  +0.033 [−0.018, +0.080] (9 items) and +0.014 [−0.035, +0.060] (the 5
  shipped items); against accepted controls only, +0.022 [−0.026, +0.069]
  and −0.001 [−0.053, +0.048]. Within venue-year (the matched comparison)
  the difference is ≈ 0 on held-out papers and *negative* on the 797
  training papers the rules came from (−0.055 [−0.085, −0.027]). A second
  requested judge route (`deepseek-v4-pro`, shipped checklist) agrees: −0.011
  [−0.089, +0.060]. The earlier single-sample +0.044 was the upper end of
  judge noise.
- *Draft A/B with the pipeline's own judge.* Over 26 units (the bf16
  mechanism paper, parallel's Paper 3, and 24 held-out accepted-ordinary
  papers never used for rule selection), revising with all 9 style rules and
  the outlines vs without them: the production judge prefers the style
  revision 21–1 (4 split), but that judge is told the style rules, so this is
  circular; the same judge with the style rules left out of its instruction
  (*style-blind*) gives 13 vs 10 (3 split), sign-test p = 0.68. On both real
  drafts the style-blind judge prefers the revision **without** the style
  rules, in both orders.
- *Ablation.* Dropping each rule (and the outlines) from the full set and
  judging style-blind, removal was preferred on more units than presence for
  `paper.style.abstract-verbs`, `paper.style.contributions-prose`,
  `paper.style.teaser-figure`, `paper.style.related-work-late` and the
  per-type outlines; these were removed (criterion fixed before the run; none
  of the margins is significant on its own). The remaining 5 checklist items
  vs none: style-blind 14 vs 7 (5 split), p = 0.19; this configuration was
  selected on the same 26 units, so even that is optimistic.
- *Rule-free pipeline judge (re-run 2026-11-18).* The pipeline's A/B judge
  no longer reads any registry rule (DOCUMENT_PIPELINE.md, "The A/B judge is
  style-blind"); the "style-blind" judge above still read the base paper
  rules. Re-judging the shipped configuration (cached revisions, same 26
  units) with it: 12 vs 3 (11 split), p = 0.035, rubric margin +3.5
  [+1.0, +6.0]. Both real drafts (bf16, Paper 3) turn from a two-order win
  for B (rule-fed judge) and a two-order win for A (style-blind judge) into
  **no preference** with position bias, rubric totals slightly favouring the
  version without the style rules (89 vs 92, 80 vs 81). The 5 items were
  kept by an ablation on these units, so this p is optimistic.
  The judge saw no task line in this harness (units have no brief).
- *Pre-registered held-out A/B (2026-11-18).* The registration is
  `evaluations/paper_style_corpus/PREREG_HELDOUT_AB.md` at commit
  `579282395f`, made before any revision or judgment. It covers 50 frozen
  units outside the 16 + 26 unit-level A/B selection units: 8 own drafts
  and 42 accepted papers. They are not untouched development data: aggregate
  same-sign/AUC analyses used these papers, and the same-sign analyses affected
  supported findings and distillation. Arms: the 5 items vs none. The registered test **did not
  pass**.
  - Both cached judge routes preferred B (labels below are requested/local
    route labels, not verified upstream model identities):
    - `gpt-5.5`: 21 vs 4 (25 split), p = 0.0009, rubric margin +3.6
      [+1.8, +5.6].
    - `deepseek-v4-pro`: 21 vs 6 (23 split), p = 0.006, margin +4.7
      [+2.8, +6.7].
  - The failure is criterion 3, the registered provenance gate, which
    requires zero unsourced numbers in B. It found 1 (unit `C3449f48d`): the
    revision additionally cites "Section 5"; the source's subsection references
    5.1 and 5.2 remain in the revision.
    That is a section reference, not an invented result, but the gate was
    fixed in advance and the outcome is not revised after the fact.
  - The preference comes from the accepted papers: 20 vs 4 and 21 vs 5. On
    the 8 own drafts both judges find no preference: 1 vs 0 and 0 vs 1,
    margins +0.6 and −0.5.
  - Details are under "Held-out A/B (pre-registered)" below. Promoting the
    items needs a new registration on fresh units, with a gate that does not
    count section numbers.

The registry keeps the 5 items as reviewer checklist prompts with their
evidence, not as a claimed quality lever. Previously `_writer_context` also
listed them under "Rules (all mandatory)", so calling them "reviewer prompts
only" was inaccurate. The final runtime correction excludes checklist items
entirely from initial writer context using the common registry `kind`.
The earlier advisory writer section did not satisfy reviewer-only admission
and has been removed. Lint and gate rules remain mandatory; reviewer checklist
handling and findings-driven revision are unchanged. Thus the five items are
reviewer prompts only, **not proven** writing requirements, because
the pre-registered held-out A/B failed its registered criterion. It ships no
outlines: the
registry → outline/draft/revise wiring for per-type outlines exists
(`rules.py::outline_guidance`, tested), but its data is empty because the
ablation did not support it. The draft-level verdict on the two pilots (both
lose under the style-blind judge, n = 2) is the strongest reason not to add
more style rules without a new A/B.

## Held-out A/B (pre-registered)

Registered in `PREREG_HELDOUT_AB.md` (commit `579282395f`). The unit list
and hashes are in `prereg_heldout_ab.json`, and the run is `validate.py
heldout`. Results are in `validation_heldout_ab.json`.

**Setup**

- Requested writer route: `gpt-5.6-terra` at temperature 0. The text cache
  discarded usage; actual upstream writer identity is unknown.
- Requested/cached judge route labels: `gpt-5.5` and `deepseek-v4-pro`.
  Cached response model labels came from local configuration, not upstream
  attestation. Actual upstream judge identities and their independence or
  distinct model families cannot be verified from these artifacts. References
  to model names in this report denote route labels, not verified models.
- Held-out means excluded from **unit-level revision A/B selection only**.
  Aggregate same-sign checks and separation AUC used these accepted papers;
  the same-sign checks affected supported findings and their distillation.
  This is not untouched development data.
- A: the base paper rules. B: A plus the 5 `paper.style.*` checklist items.
- Judge: the pipeline's rule-free `_judge_pair` with `blind_pairwise`, both
  presentation orders. B or A counts only when it wins both orders.

**Results**

| judge | B / split / A | sign-test p | mean outcome [95% CI] | rubric margin B−A [95% CI] |
|---|---|---|---|---|
| `gpt-5.5` (primary) | 21 / 25 / 4 | 0.0009 | +0.34 [+0.16, +0.52] | +3.62 [+1.76, +5.58] |
| `deepseek-v4-pro` | 21 / 23 / 6 | 0.0059 | +0.30 [+0.12, +0.48] | +4.72 [+2.78, +6.74] |
| `gpt-5.5`, own drafts (8) | 1 / 7 / 0 | 1.0 | +0.13 [0, +0.38] | +0.63 [−1.13, +2.75] |
| `deepseek-v4-pro`, own drafts (8) | 0 / 7 / 1 | 1.0 | −0.13 [−0.38, 0] | −0.50 [−2.38, +1.25] |

**Provenance gate (registered `unsourced()`)**

- Arm A: 0 unsourced numbers and 0 unsourced citation keys.
- Arm B: **1 flagged distinct number**, value **`5`**, in unit `C3449f48d`
  (additional "Section 5" references); the source's 5.1/5.2 references remain
  in the cached revision. There are 0 unsourced citation keys. This is a
  reference-number mismatch under the registered lexical gate, not evidence
  of a fabricated scientific result.
- Assignment 0c8f48f1's `provenance.check_texts` (commit `a9138e4672`), run
  for information on the same revisions, reports 0 unsourced items in both
  arms. It classes the same `5` as a rounding of `5.1`.

**Registered outcome:** primary significant ✓, second judge same direction
✓, zero unsourced numbers in B ✗, so **fail**. These refer to the recorded
route-labelled comparisons, not independently attested model families.
The checklist is not promoted to proven writing requirements.

**Per-unit results.** Each judge cell gives the verdict with the rubric
totals over both orders as (B–A). "Pos. bias" means the judge picked the
same position in both orders: **all 25 primary-route splits and all 23
secondary-route splits chose position 2 twice**. This is directional
second-position bias, not merely unspecified position bias. `_judge_pair`
uses the higher rubric total when totals differ; **when the two totals are
equal it retains the response's explicit winner field**. Consequently equal
scores do not yield a neutral tie in each presentation. This tie-breaking
behavior is part of the frozen experiment, not corrected or re-scored here.
The cached primary route has one equal-total presentation; the secondary
route has none.
The recorded sign tests describe this particular judge behavior; the bias
and unknown upstream identities limit quality/generalization claims.

| unit | kind | gpt-5.5 (B–A totals) | deepseek-v4-pro (B–A totals) | unsourced numbers in B |
|---|---|---|---|---|
| knowledge_recovery | own | split (pos. bias) (77–76) | split (pos. bias) (86–84) | 0 |
| knowledge_allocation | own | split (pos. bias) (85–83) | split (pos. bias) (93–95) | 0 |
| knowledge_repair | own | split (pos. bias) (91–91) | split (pos. bias) (92–91) | 0 |
| card_iclr2027 | own | split (pos. bias) (87–87) | split (pos. bias) (90–90) | 0 |
| parallel_parallelism | own | B (79–72) | split (pos. bias) (90–87) | 0 |
| parallel_c4l | own | split (pos. bias) (78–80) | split (pos. bias) (96–95) | 0 |
| parallel_paperA | own | split (pos. bias) (85–88) | split (pos. bias) (91–96) | 0 |
| parallel_paperB | own | split (pos. bias) (83–83) | A (86–90) | 0 |
| C1588ea0e | accepted | split (pos. bias) (72–72) | B (93–86) | 0 |
| C64383a63 | accepted | split (pos. bias) (74–74) | split (pos. bias) (88–88) | 0 |
| C9c711adf | accepted | A (57–64) | A (74–81) | 0 |
| C005a6967 | accepted | B (78–73) | split (pos. bias) (89–91) | 0 |
| C04a488fb | accepted | A (74–78) | split (pos. bias) (92–87) | 0 |
| C1d57f23e | accepted | B (63–56) | split (pos. bias) (87–78) | 0 |
| Cc118d677 | accepted | split (pos. bias) (68–67) | split (pos. bias) (93–93) | 0 |
| Cc0943596 | accepted | B (75–56) | B (87–74) | 0 |
| C3845a18b | accepted | split (pos. bias) (68–70) | split (pos. bias) (89–88) | 0 |
| Ca2400ebe | accepted | A (71–81) | B (99–84) | 0 |
| C00e47fb5 | accepted | split (pos. bias) (76–73) | B (92–77) | 0 |
| C017b480c | accepted | A (46–53) | A (68–76) | 0 |
| C4020cb7a | accepted | B (70–55) | B (89–70) | 0 |
| C771ea9e3 | accepted | split (pos. bias) (63–62) | B (86–79) | 0 |
| Cf4ceadd6 | accepted | split (pos. bias) (73–71) | split (pos. bias) (85–80) | 0 |
| C004e7256 | accepted | split (pos. bias) (51–52) | B (65–58) | 0 |
| C9d7c112f | accepted | B (77–61) | B (81–74) | 0 |
| C75bfee07 | accepted | split (pos. bias) (82–82) | B (93–83) | 0 |
| C97b0b0f7 | accepted | B (72–60) | B (89–75) | 0 |
| Cfbf5fabc | accepted | B (70–58) | B (85–67) | 0 |
| C56cbfaab | accepted | split (pos. bias) (73–73) | split (pos. bias) (80–80) | 0 |
| Cd561185c | accepted | split (pos. bias) (64–64) | A (74–78) | 0 |
| C489b175f | accepted | split (pos. bias) (51–51) | split (pos. bias) (86–87) | 0 |
| C132bb1b6 | accepted | B (67–60) | split (pos. bias) (85–82) | 0 |
| Cb7aabba7 | accepted | split (pos. bias) (55–58) | split (pos. bias) (72–73) | 0 |
| C3449f48d | accepted | split (pos. bias) (58–58) | split (pos. bias) (82–78) | 1 (value: `5`) |
| Cdf634041 | accepted | B (71–62) | B (79–69) | 0 |
| C025a3028 | accepted | split (pos. bias) (63–65) | A (81–85) | 0 |
| C02879ae6 | accepted | split (pos. bias) (56–58) | B (70–66) | 0 |
| C341d931d | accepted | B (63–58) | B (87–78) | 0 |
| C9d644766 | accepted | B (51–47) | B (73–67) | 0 |
| C90a0555c | accepted | B (68–63) | B (86–71) | 0 |
| Cdcf31686 | accepted | B (76–66) | B (89–80) | 0 |
| C01c9070b | accepted | B (62–55) | B (78–66) | 0 |
| Cd5fb0bf6 | accepted | B (75–65) | B (75–64) | 0 |
| C65009c7c | accepted | B (67–59) | A (79–86) | 0 |
| C6e22ffef | accepted | split (pos. bias) (69–69) | split (pos. bias) (87–87) | 0 |
| Cf6e5cd1a | accepted | B (68–57) | B (83–63) | 0 |
| C6b2dbc69 | accepted | B (71–65) | split (pos. bias) (83–79) | 0 |
| C77add634 | accepted | B (70–42) | split (pos. bias) (78–76) | 0 |
| Cde8d914a | accepted | split (pos. bias) (88–89) | split (pos. bias) (91–90) | 0 |
| C4481dd2b | accepted | B (84–72) | B (95–83) | 0 |

## Problem

The owner asked (2026-11-18) to study recent best papers and very highly
cited papers and to turn what distinguishes them — wording, voice, structure —
into the writing pipeline's process instead of advice. Style lore ("always
add a contribution list", "never hedge", "put a teaser on page 1") is mostly
untested; rules built from it can make drafts worse. A rule earns its place
only if exceptional papers differ from *matched* ordinary ones, the difference
replicates on held-out papers, and applying the rule to a draft is preferred
by a blind reader.

## Decision

**Matched corpus, paired contrasts, held-out confirmation, then per-rule A/B.**

1. **Corpus** (`build_corpus.py`, manifest `manifest.csv`): award papers
   (best / outstanding / honorable mention / test of time) from official award
   pages of NeurIPS, ICML, ICLR, ACL, EMNLP, CVPR, ICCV, ECCV and COLM
   2020–2026 (`awards_sources.json`, source URL per row), plus the 100 most
   cited 2020–2024 AI/CV research papers on OpenAlex with an arXiv version
   (surveys, technical reports and model cards excluded; count, OpenAlex id and
   retrieval date per row). Each positive gets an accepted-ordinary control
   from the same venue-year (OpenReview poster/plain accepts, ACL Anthology
   main volumes, CVF/PMLR proceedings) and, where an OpenReview pool exists, a
   rejected control (the HF parquet mirror of OpenReview that
   `evaluations/paper_review_calibration/` already downloads). Full text only
   from arXiv or the ACL Anthology; licence per row (arXiv licence from
   DataCite). PDFs, extracted text and annotations stay under the gitignored
   `data/paper-style-corpus/`; only the manifest, statistics, candidates and
   rules are committed. Positives and their controls share a pair id, and the
   70/30 train/test split is by pair.
2. **Measurements**: deterministic features from the PDF text
   (`extract.py` + `style_metrics.py`: sentence length, hedges, boosters,
   first person, passives, nominalisations, numbers, section roles and
   shares, captions, page-1 figure) and a blind model annotation
   (`annotate.py`, `gpt-5.6-terra`; the annotator never sees title, venue,
   authors or group) of abstract moves, introduction paragraph roles,
   one-sentence insight, contribution form, claim strength and scoping,
   related-work style, limitations framing, problem framing and paper type.
3. **Statistics** (`analyze.py`): paired differences within matched pairs,
   seeded bootstrap 95% CIs, sign-flip permutation p, Benjamini–Hochberg q
   per contrast. A finding is *supported* when the train CI excludes 0,
   q < 0.10 and the held-out difference has the same sign.
4. **Distillation** (`distill.py`): only supported `top_vs_accepted`
   findings that also point the same way among award papers alone (the
   venue-year-matched contrast) become candidates. Metric lints use one
   bound convention: the top-group quartile on the unfavourable side, so a
   rule never fails more than a quarter of the papers that justify it; a
   binary property that a quarter of top papers lack becomes a checklist
   item, not a lint. Exemplars are short quotes from training-split top
   papers, with the source paper id.
5. **Per-rule blind A/B** (`validate.py rules`): each candidate visible in an
   abstract + introduction is applied alone to 16 held-out ordinary papers
   (baseline = the pipeline's own paper rules); a different-model judge
   compares both revisions in both orders. A candidate ships only if its
   revision wins both orders on more units than the baseline does.
   Structural candidates (captions, limitations section, related-work
   placement, teaser figure) cannot be shown in that unit and ship on corpus
   evidence alone, as reviewer checklist items.
6. **Separation** (`validate.py separation --split test|train --repeats
   N`): a judge with the checklist vs the same judge without it scores
   "exceptional vs ordinary" from an anonymised excerpt. Repeat 0 is the
   temperature-0 judgment, further repeats sample at temperature 1 and the
   paper's score is their mean. AUC with a venue-year cluster bootstrap
   (resampling whole venue-years keeps each positive with its matched
   controls), the within-venue-year AUC, and a paired permutation test.
   The training split is reported as in-sample (the rules were derived on it).
7. **Draft A/B and ablation** (`validate.py drafts [--ablate]`): each unit's
   abstract + introduction is revised by the writer model with the
   registry's base paper rules (A) and with base + `paper.style.*` +
   outlines (B), prompts built from `rules.py` exactly as the writer reads
   them. B vs A is judged by the pipeline's blind-A/B judge
   (`pipeline._judge_pair` + `lib.production.critic.blind_pairwise`: both
   orders, rubric totals decide each order), which reads no registry rule
   (`rule_free` in the results). `--ablate` adds, per rule and for the
   outlines, B-without-it vs B under the same judge. The selection run
   (`validation_drafts_selection.json`) predates that judge: its
   `production` judge read every registry rule and its `style_blind` judge
   every rule but `paper.style.*`. An item is removed when its removal is preferred on more units than
   its presence. Win/split/loss with an exact sign test and bootstrap CIs of
   the mean outcome and rubric margin.

**What the data overturned.** Three common beliefs are *not* supported, so
no rule encodes them: (a) "never hedge": award/high-citation papers hedge
slightly *more* than matched controls, which supports the owner's
2026-11-18 correction (claim less, phrase carefully, no fence sentences;
`paper.no-fences`, `paper.no-overclaim`, `paper.calibrated-claims` in
`rules.py`); (b) "always add a bulleted contribution list": top papers use
contribution lists markedly *less* often and list fewer items; (c) title
formulas (colon, "Name: description", claims, questions) show no reliable
difference. Accepted-vs-rejected differences do not survive FDR correction
on any measured feature: surface style does not separate accepted from
rejected papers in this corpus.

**Regenerating.** `build_corpus.py --download` → `analyze.py --extract` →
`annotate.py` → `analyze.py` → `distill.py` → `validate.py rules --write`
→ `validate.py drafts --ablate --draft NAME=main.tex ...` (writes
`validation_drafts_selection.json`; remove its `harmful` items from
`style_rules.json` with `write_style_file`) → `validate.py drafts` (the
shipped configuration, `validation_drafts.json`) and `validate.py
separation --split test --repeats 3` → `report.py` (rewrites the block
below). The pre-registered held-out A/B runs `validate.py heldout-freeze`
(units and hashes, before registration), then `validate.py heldout`. `tests/longform/test_style_rules_evidence.py` refuses a rule that
does not cite a supported finding, a testable rule that lost its A/B, a rule
or outline set whose ablation showed harm, or a ban on calibrated hedging.

## Alternatives considered

- **Hand-written style guide from famous papers.** Rejected: anecdotes
  without controls confuse what top papers share with what all papers share
  (every abstract states a method), and the resulting rules were partly
  wrong here (hedging, contribution lists).
- **Unmatched comparison (top papers vs a random sample).** Rejected: venue,
  year and template dominate surface features (page counts, section
  numbering); pairs from the same venue-year remove most of that. The
  high-citation contrast keeps a residual mismatch (its controls are
  same-year ICLR/NeurIPS papers), which is why rules also need the award-only
  direction.
- **Model-only judgement of "what makes a paper great".** Rejected as the
  evidence: a model's taste is what the separation check tests, not a source
  of rules; annotation is used only for moves a regex cannot see, blind to
  group.
- **Ship every supported finding.** Rejected: some supported differences are
  outcomes, not writing choices (longer papers, more pages), and a rule can
  still read worse when applied; the per-rule A/B removed one candidate
  (`paper.style.intro-verbs`, a 5–5 tie).
- **A hard lint for every candidate.** Rejected where a quarter or more of
  top papers would fail it (abstract numbers): those are checklist items.
- **Judge the draft A/B with a judge told the rules.** Rejected: the
  pipeline judge then listed every registry rule and rewarded the arm told
  the style rules (21–1 here) regardless of quality. That finding made the
  pipeline's own A/B judge rule-free, so it is now the test.
- **Remove an item only when its harm is significant.** Considered: with 26
  units no single ablation reaches significance either way, so that rule
  would keep everything, including items the pilots argue against. The
  point-estimate criterion is the same one that admitted rules (`validate.py
  rules`), so admission and removal are symmetric.
- **Ship the per-type outlines on corpus evidence alone.** Rejected: they
  were A/B-testable in the draft unit and their ablation went against them
  (removal preferred 11 vs 7).

## Limits

- Coverage gaps: NAACL, ACL 2020–2022 and 2026, EMNLP 2020–2022 and 2025,
  COLM 2024–2025 award lists were not collected (`awards_sources.json`
  `gaps`). CVPR/ICCV/ECCV positives use conference-site or TCPAMI lists.
- arXiv versions of rejected OpenReview submissions may be later revisions.
- PDF segmentation is heuristic; papers whose abstract or body did not
  segment (body < 1500 words, abstract < 40 or > 400 words) are excluded.
- The annotation and both judges are language models; ICLR/NeurIPS papers
  may be in their training data, so the separation check measures agreement
  with real outcomes, not unseen-paper generalization.
- The per-rule A/B uses 16 units per rule and the ablation 26 units: no
  single rule's margin is statistically significant on its own; both are
  do-no-harm filters on top of the corpus evidence. The shipped
  configuration's draft A/B reuses the ablation's units (post-selection).
- Draft units are abstract + introduction only; the structural items
  (captions, limitations section) can only change what a revision says about
  them, not the paper's figures or sections.
- A second requested judge route (`deepseek-v4-pro`) replicates the held-out
  separation once; its upstream identity/family is not attested here; `claude-sonnet-5-5`, `claude-opus-5` and the Gemini routes
  returned server errors or empty replies on this host and were not used.

## Evidence

<!-- evidence:begin -->
Generated 2026-11-18 from `evaluations/paper_style_corpus/` (annotation model `gpt-5.6-terra`). Papers analysed per group: {'award': 390, 'highcite': 85, 'control_accepted': 478, 'control_rejected': 214}.

Cells: mean of first group vs second group; paired mean difference Δ with seeded bootstrap 95% CI; standardized paired effect dz; Benjamini–Hochberg q; n training pairs; held-out Δ. **✓** = supported (train CI excludes 0, q < 0.10, same sign on held-out). Columns: award+high-citation vs accepted-ordinary (same venue-year), award only vs accepted-ordinary, accepted-ordinary vs rejected.

### Title patterns

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| title words | 9.08 vs 9.36; Δ -0.282 [-0.727, +0.169], dz -0.07, q 0.42, n=308; test Δ +0.143 (n=133) | 9.35 vs 9.47; Δ -0.124 [-0.636, +0.384], dz -0.03, q 0.80, n=258; test Δ +0.255 (n=102) | 8.59 vs 8.32; Δ +0.27 [-0.385, +0.91], dz +0.07, q 0.87, n=122; test Δ -0.161 (n=62) |
| title has a colon | 0.51 vs 0.51; Δ +0 [-0.0812, +0.0779], dz +0.00, q 1.00, n=308; test Δ +0 (n=133) | 0.512 vs 0.539; Δ -0.0271 [-0.12, +0.062], dz -0.04, q 0.76, n=258; test Δ -0.127 (n=102) | 0.41 vs 0.295; Δ +0.115 [+0.0082, +0.23], dz +0.19, q 0.63, n=122; test Δ +0 (n=62) |
| title is "Name: description" | 0.367 vs 0.393; Δ -0.026 [-0.104, +0.0519], dz -0.04, q 0.73, n=308; test Δ -0.0602 (n=133) | 0.353 vs 0.403; Δ -0.0504 [-0.136, +0.0271], dz -0.07, q 0.43, n=258; test Δ -0.196 (n=102) | 0.303 vs 0.23; Δ +0.0738 [-0.0328, +0.18], dz +0.12, q 0.66, n=122; test Δ +0.0645 (n=62) |
| title is a question | 0.0227 vs 0.0162; Δ +0.00649 [-0.0162, +0.0292], dz +0.03, q 0.90, n=308; test Δ +0.00752 (n=133) | 0.0271 vs 0.0116; Δ +0.0155 [-0.00775, +0.0388], dz +0.08, q 0.52, n=258; test Δ +0.0098 (n=102) | 0.0164 vs 0.0082; Δ +0.0082 [-0.0164, +0.041], dz +0.05, q 1.00, n=122; test Δ +0 (n=62) |
| title states a claim (verb) | 0.133 vs 0.0909; Δ +0.0422 [-0.00649, +0.0909], dz +0.10, q 0.22, n=308; test Δ +0.0526 (n=133) | 0.136 vs 0.0814; Δ +0.0543 [+0.00388, +0.109], dz +0.13, q 0.15, n=258; test Δ +0.049 (n=102) | 0.0984 vs 0.041; Δ +0.0574 [-0.0082, +0.123], dz +0.16, q 0.63, n=122; test Δ +0 (n=62) |

### Abstract move structure

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| abstract states a problem (annotated) | 0.541 vs 0.648; Δ -0.107 [-0.192, -0.0228], dz -0.14, q 0.05, n=307; test Δ -0.0909 (n=132) **✓** | 0.558 vs 0.651; Δ -0.093 [-0.182, -0.00388], dz -0.13, q 0.13, n=258; test Δ -0.098 (n=102) | 0.628 vs 0.669; Δ -0.0413 [-0.165, +0.0826], dz -0.06, q 0.93, n=121; test Δ -0.131 (n=61) |
| abstract states a gap (annotated) | 0.339 vs 0.391; Δ -0.0521 [-0.127, +0.0261], dz -0.08, q 0.38, n=307; test Δ +0.0227 (n=132) | 0.38 vs 0.38; Δ +0 [-0.0814, +0.0814], dz +0.00, q 1.00, n=258; test Δ +0.0588 (n=102) | 0.388 vs 0.339; Δ +0.0496 [-0.0744, +0.182], dz +0.07, q 0.88, n=121; test Δ -0.0656 (n=61) |
| abstract states an implication (annotated) | 0.45 vs 0.401; Δ +0.0489 [-0.0293, +0.134], dz +0.07, q 0.42, n=307; test Δ +0.114 (n=132) | 0.461 vs 0.399; Δ +0.062 [-0.0194, +0.143], dz +0.09, q 0.31, n=258; test Δ +0.176 (n=102) | 0.455 vs 0.554; Δ -0.0992 [-0.215, +0.0165], dz -0.16, q 0.63, n=121; test Δ +0.082 (n=61) |
| abstract moves in canonical order (annotated) | 0.917 vs 0.917; Δ -9.61e-07 [-0.0155, +0.0149], dz -0.00, q 1.00, n=306; test Δ -0.0179 (n=132) | 0.922 vs 0.92; Δ +0.00116 [-0.0152, +0.0189], dz +0.01, q 0.99, n=257; test Δ -0.0276 (n=102) | 0.908 vs 0.865; Δ +0.0434 [+0.014, +0.0731], dz +0.27, q 0.42, n=121; test Δ +0.0378 (n=61) |
| abstract states a number (share) | 0.763 vs 0.594; Δ +0.169 [+0.0909, +0.237], dz +0.26, q 0.00, n=308; test Δ +0.0301 (n=133) **✓** | 0.736 vs 0.585; Δ +0.151 [+0.0698, +0.229], dz +0.23, q 0.01, n=258; test Δ -0.0588 (n=102) | 0.541 vs 0.541; Δ +0 [-0.123, +0.115], dz +0.00, q 1.00, n=122; test Δ +0.161 (n=62) |
| numbers per 1k abstract words | 17.4 vs 8.57; Δ +8.88 [+6.27, +11.7], dz +0.36, q 0.00, n=308; test Δ +5.04 (n=133) **✓** | 15 vs 8.75; Δ +6.26 [+3.75, +8.76], dz +0.30, q 0.00, n=258; test Δ -0.325 (n=102) | 6.99 vs 7.33; Δ -0.333 [-3.71, +2.91], dz -0.02, q 0.98, n=122; test Δ +5.01 (n=62) |
| abstract words | 173 vs 174; Δ -1.35 [-7.23, +4.98], dz -0.02, q 0.82, n=308; test Δ +0.226 (n=133) | 173 vs 171; Δ +2.01 [-4.32, +8.41], dz +0.04, q 0.72, n=258; test Δ +1.42 (n=102) | 181 vs 182; Δ -1.18 [-11.9, +8.9], dz -0.02, q 0.98, n=122; test Δ +6.65 (n=62) |

### Introduction roles and contributions

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| intro states a one-sentence insight (annotated) | 0.984 vs 0.98; Δ +0.00326 [-0.0163, +0.0228], dz +0.02, q 1.00, n=307; test Δ +0.00758 (n=132) | 0.981 vs 0.977; Δ +0.00388 [-0.0194, +0.0271], dz +0.02, q 1.00, n=258; test Δ +0.0098 (n=102) | 0.992 vs 0.975; Δ +0.0165 [-0.0165, +0.0496], dz +0.09, q 0.93, n=121; test Δ +0.0328 (n=61) |
| intro quantifies the main result (annotated) | 0.599 vs 0.456; Δ +0.143 [+0.0717, +0.221], dz +0.21, q 0.01, n=307; test Δ +0.0758 (n=132) **✓** | 0.566 vs 0.465; Δ +0.101 [+0.0194, +0.182], dz +0.14, q 0.07, n=258; test Δ +0.0196 (n=102) **✓** | 0.413 vs 0.38; Δ +0.0331 [-0.0744, +0.14], dz +0.05, q 0.98, n=121; test Δ +0.18 (n=61) |
| intro opens broadly (annotated) | 0.948 vs 0.977; Δ -0.0293 [-0.0586, +0], dz -0.11, q 0.19, n=307; test Δ -0.0455 (n=132) | 0.938 vs 0.973; Δ -0.0349 [-0.0736, +0], dz -0.12, q 0.19, n=258; test Δ -0.0588 (n=102) | 0.975 vs 0.959; Δ +0.0165 [-0.0331, +0.0661], dz +0.06, q 0.98, n=121; test Δ +0.0164 (n=61) |
| "we show/find/..." per 1k intro words | 1.57 vs 1.12; Δ +0.456 [+0.191, +0.719], dz +0.19, q 0.01, n=308; test Δ +0.459 (n=133) **✓** | 1.64 vs 1.11; Δ +0.53 [+0.239, +0.826], dz +0.22, q 0.01, n=258; test Δ +0.503 (n=102) **✓** | 1.16 vs 1.51; Δ -0.347 [-0.745, +0.0407], dz -0.16, q 0.63, n=122; test Δ -0.00743 (n=62) |
| any contribution list (annotated) | 0.495 vs 0.694; Δ -0.199 [-0.274, -0.124], dz -0.29, q 0.00, n=307; test Δ -0.136 (n=132) **✓** | 0.508 vs 0.709; Δ -0.202 [-0.287, -0.12], dz -0.30, q 0.00, n=258; test Δ -0.147 (n=102) **✓** | 0.653 vs 0.562; Δ +0.0909 [-0.0413, +0.215], dz +0.13, q 0.66, n=121; test Δ -0.0164 (n=61) |
| bulleted contribution list (annotated) | 0.244 vs 0.43; Δ -0.186 [-0.257, -0.111], dz -0.28, q 0.00, n=307; test Δ -0.197 (n=132) **✓** | 0.256 vs 0.442; Δ -0.186 [-0.267, -0.105], dz -0.28, q 0.00, n=258; test Δ -0.235 (n=102) **✓** | 0.38 vs 0.306; Δ +0.0744 [-0.0413, +0.198], dz +0.12, q 0.69, n=121; test Δ +0.0328 (n=61) |
| number of listed contributions (annotated) | 1.89 vs 2.33; Δ -0.436 [-0.697, -0.169], dz -0.18, q 0.01, n=307; test Δ -0.348 (n=132) **✓** | 1.96 vs 2.35; Δ -0.388 [-0.667, -0.105], dz -0.17, q 0.04, n=258; test Δ -0.363 (n=102) **✓** | 2.23 vs 2.19; Δ +0.0413 [-0.372, +0.446], dz +0.02, q 0.98, n=121; test Δ +0.148 (n=61) |

### Claim strength and hedging

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| claim strength 1-5 (annotated) | 4.17 vs 4.16; Δ +0.00651 [-0.104, +0.117], dz +0.01, q 1.00, n=307; test Δ +0.0985 (n=132) | 4.14 vs 4.15; Δ -0.0116 [-0.132, +0.101], dz -0.01, q 0.99, n=258; test Δ +0.0588 (n=102) | 4.19 vs 4.16; Δ +0.0331 [-0.124, +0.207], dz +0.03, q 0.98, n=121; test Δ +0.0328 (n=61) |
| claims scoped to tested settings (annotated) | 0.681 vs 0.717; Δ -0.0358 [-0.101, +0.0293], dz -0.06, q 0.51, n=307; test Δ -0.00758 (n=132) | 0.647 vs 0.721; Δ -0.0736 [-0.143, -0.00388], dz -0.12, q 0.16, n=258; test Δ +0.0196 (n=102) | 0.694 vs 0.669; Δ +0.0248 [-0.0992, +0.14], dz +0.04, q 0.98, n=121; test Δ +0.0492 (n=61) |
| hedges per 1k abstract words | 2.42 vs 1.86; Δ +0.558 [-0.0458, +1.22], dz +0.10, q 0.16, n=308; test Δ +0.499 (n=133) | 2.61 vs 1.81; Δ +0.808 [+0.109, +1.53], dz +0.14, q 0.08, n=258; test Δ +0.759 (n=102) **✓** | 1.72 vs 2.14; Δ -0.428 [-1.37, +0.54], dz -0.08, q 0.85, n=122; test Δ -0.687 (n=62) |
| hedges per 1k intro words | 3.14 vs 2.59; Δ +0.551 [+0.103, +1], dz +0.14, q 0.05, n=308; test Δ +0.68 (n=133) **✓** | 3.22 vs 2.51; Δ +0.709 [+0.219, +1.22], dz +0.18, q 0.02, n=258; test Δ +1.02 (n=102) **✓** | 2.58 vs 3.17; Δ -0.592 [-1.29, +0.0848], dz -0.15, q 0.63, n=122; test Δ -0.953 (n=62) |
| hedges per 1k body words | 3.33 vs 2.64; Δ +0.687 [+0.412, +0.953], dz +0.28, q 0.00, n=308; test Δ +0.591 (n=133) **✓** | 3.4 vs 2.73; Δ +0.669 [+0.373, +0.978], dz +0.27, q 0.00, n=258; test Δ +0.415 (n=102) **✓** | 2.31 vs 2.7; Δ -0.392 [-0.819, +0.0333], dz -0.16, q 0.63, n=122; test Δ -0.47 (n=62) |
| promotional words per 1k body words | 2.07 vs 2.18; Δ -0.106 [-0.313, +0.101], dz -0.06, q 0.48, n=308; test Δ -0.135 (n=133) | 2.04 vs 2.32; Δ -0.283 [-0.479, -0.0612], dz -0.16, q 0.04, n=258; test Δ -0.429 (n=102) **✓** | 1.63 vs 1.72; Δ -0.0938 [-0.381, +0.195], dz -0.06, q 0.88, n=122; test Δ +0.269 (n=62) |

### Sentence length, voice, nominalisation

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| abstract words per sentence | 23.9 vs 23.8; Δ +0.11 [-0.645, +0.859], dz +0.02, q 0.90, n=308; test Δ +0.0836 (n=133) | 23.9 vs 23.7; Δ +0.246 [-0.56, +1.07], dz +0.04, q 0.75, n=258; test Δ +0.539 (n=102) | 23.8 vs 23.2; Δ +0.607 [-0.491, +1.58], dz +0.10, q 0.69, n=122; test Δ +0.878 (n=62) |
| body words per sentence | 25.1 vs 24.9; Δ +0.188 [-0.305, +0.664], dz +0.04, q 0.62, n=308; test Δ +0.024 (n=133) | 25.1 vs 24.7; Δ +0.434 [-0.0591, +0.953], dz +0.10, q 0.20, n=258; test Δ +0.37 (n=102) | 25.6 vs 25; Δ +0.647 [-0.0739, +1.41], dz +0.16, q 0.63, n=122; test Δ +0.423 (n=62) |
| first person per 1k abstract words | 26.2 vs 23.9; Δ +2.32 [+0.573, +4.03], dz +0.15, q 0.03, n=308; test Δ +1.13 (n=133) **✓** | 26 vs 23.8; Δ +2.21 [+0.356, +4.11], dz +0.14, q 0.07, n=258; test Δ +1.94 (n=102) **✓** | 24.7 vs 27.1; Δ -2.4 [-5.64, +0.725], dz -0.14, q 0.63, n=122; test Δ -0.503 (n=62) |
| first person per 1k intro words | 17.7 vs 16.2; Δ +1.46 [+0.287, +2.57], dz +0.15, q 0.04, n=308; test Δ +0.704 (n=133) **✓** | 17.8 vs 16.2; Δ +1.58 [+0.325, +2.75], dz +0.16, q 0.05, n=258; test Δ +0.243 (n=102) **✓** | 16.8 vs 16.1; Δ +0.716 [-0.854, +2.31], dz +0.08, q 0.85, n=122; test Δ +0.57 (n=62) |
| passives per body sentence | 0.228 vs 0.231; Δ -0.00225 [-0.0145, +0.00997], dz -0.02, q 0.86, n=308; test Δ -0.008 (n=133) | 0.23 vs 0.22; Δ +0.00939 [-0.00383, +0.023], dz +0.09, q 0.28, n=258; test Δ -0.00512 (n=102) | 0.247 vs 0.255; Δ -0.00834 [-0.0303, +0.0125], dz -0.07, q 0.88, n=122; test Δ -0.0135 (n=62) |
| nominalisations per 1k abstract words | 65.8 vs 71.9; Δ -6.11 [-10.4, -1.59], dz -0.15, q 0.03, n=308; test Δ -5.9 (n=133) **✓** | 66.4 vs 73.3; Δ -6.97 [-12.1, -2.05], dz -0.17, q 0.03, n=258; test Δ -7.53 (n=102) **✓** | 67.4 vs 68.2; Δ -0.835 [-6.81, +5.86], dz -0.02, q 0.98, n=122; test Δ -14.9 (n=62) |
| nominalisations per 1k intro words | 56.7 vs 62; Δ -5.22 [-8.06, -2.4], dz -0.20, q 0.01, n=308; test Δ -7.02 (n=133) **✓** | 56.9 vs 63; Δ -6.1 [-9.17, -2.96], dz -0.24, q 0.00, n=258; test Δ -9.24 (n=102) **✓** | 59 vs 61.4; Δ -2.42 [-6.54, +1.92], dz -0.10, q 0.69, n=122; test Δ -4.21 (n=62) |
| nominalisations per 1k body words | 52.8 vs 55.9; Δ -3.1 [-5.06, -1.01], dz -0.17, q 0.02, n=308; test Δ -3.6 (n=133) **✓** | 53.2 vs 57; Δ -3.84 [-6.01, -1.62], dz -0.21, q 0.01, n=258; test Δ -5.78 (n=102) **✓** | 51.6 vs 53.4; Δ -1.81 [-5.01, +1.24], dz -0.11, q 0.69, n=122; test Δ -3.45 (n=62) |

### Teaser figure and figure/table design

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| Figure 1 on page 1 | 0.422 vs 0.344; Δ +0.0779 [+0.00974, +0.149], dz +0.12, q 0.11, n=308; test Δ +0.015 (n=133) | 0.422 vs 0.411; Δ +0.0116 [-0.0659, +0.0891], dz +0.02, q 0.94, n=258; test Δ -0.0784 (n=102) | 0.082 vs 0.115; Δ -0.0328 [-0.107, +0.041], dz -0.08, q 0.88, n=122; test Δ +0.0806 (n=62) |
| any visual on page 1 | 0.425 vs 0.344; Δ +0.0812 [+0.013, +0.153], dz +0.13, q 0.08, n=308; test Δ +0.015 (n=133) **✓** | 0.422 vs 0.411; Δ +0.0116 [-0.0659, +0.0891], dz +0.02, q 0.94, n=258; test Δ -0.0784 (n=102) | 0.082 vs 0.115; Δ -0.0328 [-0.107, +0.041], dz -0.08, q 0.88, n=122; test Δ +0.0645 (n=62) |
| figures in body | 5.19 vs 4.53; Δ +0.659 [+0.227, +1.12], dz +0.17, q 0.02, n=308; test Δ +0.895 (n=133) **✓** | 5.08 vs 4.69; Δ +0.395 [-0.0891, +0.849], dz +0.10, q 0.20, n=258; test Δ +0.118 (n=102) | 3.94 vs 4.52; Δ -0.582 [-1.31, +0.107], dz -0.15, q 0.63, n=122; test Δ -0.677 (n=62) |
| tables in body | 3.53 vs 3.8; Δ -0.266 [-0.701, +0.24], dz -0.07, q 0.43, n=308; test Δ +0.429 (n=133) | 3.16 vs 3.96; Δ -0.806 [-1.25, -0.314], dz -0.21, q 0.01, n=258; test Δ -0.765 (n=102) **✓** | 2.78 vs 2.37; Δ +0.41 [-0.156, +0.934], dz +0.14, q 0.65, n=122; test Δ +0.581 (n=62) |
| caption words | 38.6 vs 34.7; Δ +3.97 [+1.44, +6.35], dz +0.18, q 0.01, n=299; test Δ +1.54 (n=126) **✓** | 39.1 vs 34.2; Δ +4.89 [+2.39, +7.5], dz +0.23, q 0.01, n=251; test Δ +1.21 (n=99) **✓** | 37.4 vs 38.2; Δ -0.79 [-5.84, +4.04], dz -0.03, q 0.98, n=115; test Δ -0.328 (n=60) |
| captions with >= 2 sentences (share) | 0.692 vs 0.633; Δ +0.0593 [+0.0191, +0.1], dz +0.16, q 0.03, n=299; test Δ +0.0218 (n=126) **✓** | 0.699 vs 0.628; Δ +0.071 [+0.023, +0.117], dz +0.19, q 0.02, n=251; test Δ +0.0128 (n=99) **✓** | 0.687 vs 0.649; Δ +0.0384 [-0.0369, +0.112], dz +0.09, q 0.77, n=115; test Δ -0.0279 (n=60) |

### Related-work positioning

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| related work placed after the method | 0.238 vs 0.119; Δ +0.119 [+0.0553, +0.187], dz +0.22, q 0.01, n=235; test Δ +0.0928 (n=97) **✓** | 0.243 vs 0.119; Δ +0.124 [+0.0495, +0.198], dz +0.23, q 0.01, n=202; test Δ +0.0946 (n=74) **✓** | 0.121 vs 0.143; Δ -0.022 [-0.132, +0.0879], dz -0.04, q 0.98, n=91; test Δ -0.0455 (n=44) |
| related-work position (0 = start of body) | 0.252 vs 0.171; Δ +0.0812 [+0.0354, +0.127], dz +0.23, q 0.01, n=235; test Δ +0.059 (n=97) **✓** | 0.253 vs 0.164; Δ +0.0891 [+0.0421, +0.138], dz +0.25, q 0.01, n=202; test Δ +0.0642 (n=74) **✓** | 0.207 vs 0.229; Δ -0.0217 [-0.0923, +0.0423], dz -0.07, q 0.88, n=91; test Δ -0.0128 (n=44) |
| related work thematic with contrast (annotated) | 0.726 vs 0.772; Δ -0.0456 [-0.111, +0.0228], dz -0.08, q 0.39, n=307; test Δ -0.167 (n=132) | 0.756 vs 0.779; Δ -0.0233 [-0.093, +0.0465], dz -0.04, q 0.76, n=258; test Δ -0.196 (n=102) | 0.769 vs 0.678; Δ +0.0909 [-0.0248, +0.198], dz +0.14, q 0.66, n=121; test Δ +0.0164 (n=61) |
| related-work share of body | 0.105 vs 0.114; Δ -0.00941 [-0.0209, +0.00205], dz -0.09, q 0.23, n=308; test Δ -0.0119 (n=133) | 0.106 vs 0.114; Δ -0.0081 [-0.0206, +0.00486], dz -0.08, q 0.35, n=258; test Δ +0.000714 (n=102) | 0.119 vs 0.134; Δ -0.0156 [-0.0373, +0.00428], dz -0.13, q 0.63, n=122; test Δ -0.0312 (n=62) |

### Section structure and length ratios

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| top-level sections | 8.49 vs 7.62; Δ +0.867 [+0.484, +1.29], dz +0.24, q 0.00, n=308; test Δ +1.05 (n=133) **✓** | 8.49 vs 7.87; Δ +0.616 [+0.225, +1.06], dz +0.18, q 0.02, n=258; test Δ +0.569 (n=102) **✓** | 6.89 vs 6.82; Δ +0.0656 [-0.418, +0.541], dz +0.02, q 0.98, n=122; test Δ +0.581 (n=62) |
| introduction share of body | 0.141 vs 0.155; Δ -0.0136 [-0.021, -0.00628], dz -0.20, q 0.01, n=308; test Δ -0.00614 (n=133) **✓** | 0.143 vs 0.153; Δ -0.00984 [-0.0174, -0.00151], dz -0.15, q 0.06, n=258; test Δ -0.00317 (n=102) **✓** | 0.161 vs 0.167; Δ -0.00666 [-0.0235, +0.00916], dz -0.07, q 0.87, n=122; test Δ +0.00621 (n=62) |
| method share of body | 0.356 vs 0.335; Δ +0.0206 [-0.00519, +0.0498], dz +0.09, q 0.24, n=308; test Δ +0.0101 (n=133) | 0.355 vs 0.327; Δ +0.0287 [+0.00108, +0.0577], dz +0.12, q 0.13, n=258; test Δ +0.0268 (n=102) | 0.376 vs 0.35; Δ +0.0256 [-0.0158, +0.0678], dz +0.11, q 0.66, n=122; test Δ +0.0272 (n=62) |
| experiments share of body | 0.251 vs 0.295; Δ -0.0439 [-0.0669, -0.0198], dz -0.20, q 0.01, n=308; test Δ -0.0223 (n=133) **✓** | 0.247 vs 0.299; Δ -0.052 [-0.0787, -0.0246], dz -0.24, q 0.01, n=258; test Δ -0.042 (n=102) **✓** | 0.276 vs 0.264; Δ +0.012 [-0.0184, +0.0431], dz +0.07, q 0.87, n=122; test Δ +0.0127 (n=62) |
| body words | 5701 vs 4885; Δ +816 [+383, +1.35e+03], dz +0.19, q 0.00, n=308; test Δ +694 (n=133) **✓** | 5405 vs 4893; Δ +512 [+177, +924], dz +0.17, q 0.02, n=258; test Δ +395 (n=102) **✓** | 5050 vs 5302; Δ -252 [-664, +91.8], dz -0.11, q 0.66, n=122; test Δ +476 (n=62) |
| pages (incl. appendix) | 25.5 vs 20.2; Δ +5.28 [+3.32, +7.37], dz +0.29, q 0.00, n=308; test Δ +4.45 (n=133) **✓** | 25.2 vs 19.9; Δ +5.26 [+3.3, +7.35], dz +0.32, q 0.00, n=258; test Δ +3.93 (n=102) **✓** | 23.3 vs 23.7; Δ -0.459 [-2.65, +1.86], dz -0.04, q 0.98, n=122; test Δ +0.903 (n=62) |

### Limitations framing

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| dedicated Limitations section | 0.555 vs 0.494; Δ +0.0617 [+0.0195, +0.107], dz +0.16, q 0.02, n=308; test Δ +0.0752 (n=133) **✓** | 0.632 vs 0.585; Δ +0.0465 [+0.00388, +0.093], dz +0.13, q 0.16, n=258; test Δ +0.049 (n=102) | 0.0738 vs 0.131; Δ -0.0574 [-0.131, +0.0164], dz -0.14, q 0.66, n=122; test Δ +0 (n=62) |
| Limitations share of body | 0.0294 vs 0.0208; Δ +0.00865 [+0.00461, +0.0127], dz +0.23, q 0.00, n=308; test Δ +0.00463 (n=133) **✓** | 0.0334 vs 0.0247; Δ +0.00872 [+0.00344, +0.0135], dz +0.22, q 0.01, n=258; test Δ +0.00415 (n=102) **✓** | 0.00233 vs 0.00673; Δ -0.0044 [-0.00841, -0.000772], dz -0.21, q 0.63, n=122; test Δ +0.00219 (n=62) |
| specific dedicated limitations (annotated) | 0.56 vs 0.524; Δ +0.0358 [-0.00651, +0.0782], dz +0.09, q 0.26, n=307; test Δ +0.0606 (n=132) | 0.632 vs 0.616; Δ +0.0155 [-0.0271, +0.062], dz +0.04, q 0.78, n=258; test Δ +0.0392 (n=102) | 0.116 vs 0.157; Δ -0.0413 [-0.124, +0.0413], dz -0.09, q 0.87, n=121; test Δ +0 (n=61) |

### Problem framing

| measure | top vs accepted | award vs accepted | accepted vs rejected |
|---|---|---|---|
| framed as an understanding gap (annotated) | 0.163 vs 0.0945; Δ +0.0684 [+0.0163, +0.121], dz +0.15, q 0.05, n=307; test Δ +0.0682 (n=132) **✓** | 0.19 vs 0.101; Δ +0.0891 [+0.0271, +0.151], dz +0.18, q 0.02, n=258; test Δ +0.118 (n=102) **✓** | 0.0661 vs 0.14; Δ -0.0744 [-0.149, +0], dz -0.17, q 0.63, n=121; test Δ +0 (n=61) |
| framed as a capability gap (annotated) | 0.537 vs 0.661; Δ -0.124 [-0.195, -0.0521], dz -0.19, q 0.01, n=307; test Δ -0.0455 (n=132) **✓** | 0.531 vs 0.659; Δ -0.128 [-0.205, -0.0465], dz -0.19, q 0.01, n=258; test Δ -0.0882 (n=102) **✓** | 0.669 vs 0.612; Δ +0.0579 [-0.0579, +0.174], dz +0.09, q 0.85, n=121; test Δ -0.0492 (n=61) |

### Candidate rules and exemplars

- **`paper.style.abstract-number`** (checklist): When the main result is quantitative, the abstract states it as a concrete number taken from the evidence. — A/B: rule preferred on 7, baseline on 4 of 16 held-out units → **shipped**
  - evidence: findings.json top_vs_accepted/abs_has_number: top 0.763 vs accepted 0.594, paired diff +0.169 [95% CI +0.0909, +0.237], dz +0.26, q 0.003, n=308 train pairs; held-out diff +0.0301 n=133
  - exemplar: “To capture complex environment dynamics, NWM employs a Conditional Diffusion Transformer (CDiT), trained on a diverse collection of egocentric videos of both human and robotic agents, and scaled up to 1 billion parameters.” (A02391afc Navigation World Models)
  - exemplar: “To study the ability of retrieval systems to meet such information needs, we construct QUEST, a dataset of 3357 natural language queries with implicit set operations, that map to a set of entities corresponding to Wikipedia documents.” (A048a8fe1 QUEST: A Retrieval Dataset of Entity-Seeking Queries with Implicit Set Operation)
  - exemplar: “Pangu also enables, for the first time, effective few-shot in-context learning for KBQA with large LMs such as Codex.1” (A05958a56 Don’t Generate, Discriminate: A Proposal for Grounding Language Models to Real-W)
- **`paper.style.abstract-verbs`** (lint, metric {"name": "nominal_per_k", "section": "abstract", "op": "<=", "value": 80.45}): Write the abstract with verbs, not nominalisations: "we measure how X changes" rather than "a measurement of the change of X". — A/B: rule preferred on 4, baseline on 3 of 16 held-out units → **shipped** → **removed** (draft ablation: removal preferred, see Validation)
  - evidence: findings.json top_vs_accepted/abs_nominal_per_k: top 65.8 vs accepted 71.9, paired diff -6.11 [95% CI -10.4, -1.59], dz -0.15, q 0.027, n=308 train pairs; held-out diff -5.9 n=133
  - exemplar: “We describe an algorithm for MBR which gradually grows the number of samples used to estimate the utility while pruning hypotheses that are unlikely to have the highest utility according to confidence estimates obtained with bootstrap sampling.” (A007fd8af Faster Minimum Bayes Risk Decoding with Confidence-based Pruning)
  - exemplar: “We introduce a Navigation World Model (NWM), a controllable video generation model that predicts future visual observations based on past observations and navigation actions.” (A02391afc Navigation World Models)
  - exemplar: “Theory of Mind (ToM)—the ability to reason about the mental states of other people—is a key element of our social intelligence.” (A036488ca Minding Language Models’ (Lack of) Theory of Mind: A Plug-and-Play Multi-Charact)
- **`paper.style.contributions-prose`** (checklist): Do not pad the introduction with a generic bulleted contribution list; state the contributions in prose, and use a list only for genuinely distinct contributions. — A/B: rule preferred on 6, baseline on 4 of 16 held-out units → **shipped** → **removed** (draft ablation: removal preferred, see Validation)
  - evidence: findings.json top_vs_accepted/ann_contrib_bulleted: top 0.244 vs accepted 0.43, paired diff -0.186 [95% CI -0.257, -0.111], dz -0.28, q 0.003, n=307 train pairs; held-out diff -0.197 n=132
  - evidence: findings.json top_vs_accepted/intro_contrib_list: top 0.591 vs accepted 0.708, paired diff -0.117 [95% CI -0.195, -0.0455], dz -0.18, q 0.013, n=308 train pairs; held-out diff -0.18 n=133
- **`paper.style.intro-quantified`** (checklist): The introduction quantifies the main result (the same headline number as the abstract), not only "significant improvements". — A/B: rule preferred on 4, baseline on 2 of 16 held-out units → **shipped**
  - evidence: findings.json top_vs_accepted/ann_intro_quantified: top 0.599 vs accepted 0.456, paired diff +0.143 [95% CI +0.0717, +0.221], dz +0.21, q 0.008, n=307 train pairs; held-out diff +0.0758 n=132
  - evidence: findings.json top_vs_accepted/intro_number_per_k: top 30.8 vs accepted 22.4, paired diff +8.45 [95% CI +3.86, +12.8], dz +0.21, q 0.005, n=308 train pairs; held-out diff +3.49 n=133
  - exemplar: “It also achieves a top-1 accuracy of 87.3% on ImageNet-1K image classiﬁcation.” (A068d2fb7 Swin Transformer: Hierarchical Vision Transformer using Shifted Windows)
  - exemplar: “In two parallel experiments, we demonstrate that integrating awareness of story arcs enhances model diversity (outperforming vanilla prompting by 45%), whereas incorporating turning point information significantly improves narrative suspense and engagement (outperforming vanilla by 40%).” (A092dfb4d Are Large Language Models Capable of Generating Human-Level Narratives?)
  - exemplar: “We show that it can be a viable approach for annotation quality estimation: it is able to provide similar statistical guarantees as confidence intervals while requiring up to 50% less samples.” (A16fe4550 On Efficient and Statistical Quality Estimation for Data Annotation)
- **`paper.style.caption-takeaway`** (checklist): Every figure and table caption states its takeaway in a full sentence, not only what is plotted. — structural (not A/B-testable on abstract+introduction); shipped on corpus evidence
  - evidence: findings.json top_vs_accepted/caption_takeaway: top 0.692 vs accepted 0.633, paired diff +0.0593 [95% CI +0.0191, +0.1], dz +0.16, q 0.026, n=299 train pairs; held-out diff +0.0218 n=126
- **`paper.style.limitations-section`** (checklist): A dedicated Limitations section names specific limitations of this work (settings not tested, assumptions), not generic caveats. — structural (not A/B-testable on abstract+introduction); shipped on corpus evidence
  - evidence: findings.json top_vs_accepted/has_limitations: top 0.555 vs accepted 0.494, paired diff +0.0617 [95% CI +0.0195, +0.107], dz +0.16, q 0.018, n=308 train pairs; held-out diff +0.0752 n=133
- **`paper.style.intro-findings-first-person`** (checklist): The introduction states the main findings explicitly in first person ("we show/find/demonstrate that ..."). — A/B: rule preferred on 5, baseline on 2 of 16 held-out units → **shipped**
  - evidence: findings.json top_vs_accepted/intro_first_person_findings: top 1.57 vs accepted 1.12, paired diff +0.456 [95% CI +0.191, +0.719], dz +0.19, q 0.008, n=308 train pairs; held-out diff +0.459 n=133
  - exemplar: “Qualitatively, we observe improved video prediction and generation performance on single images (see Figure 1(c)).” (A02391afc Navigation World Models)
  - exemplar: “However, current reading comprehension datasets for theory of mind reasoning are simplistic and lack diversity, leading to brittle downstream models which, as we show, fail in the presence of even slight out-of-distribution perturbations.” (A036488ca Minding Language Models’ (Lack of) Theory of Mind: A Plug-and-Play Multi-Charact)
  - exemplar: “We show that simply using BERT-base with Pangu is sufficient for setting a new record on standard KBQA datasets, and larger LMs further bring substantial gains.” (A05958a56 Don’t Generate, Discriminate: A Proposal for Grounding Language Models to Real-W)
- **`paper.style.intro-verbs`** (lint, metric {"name": "nominal_per_k", "section": "introduction", "op": "<=", "value": 68.81}): Write the introduction with agents and verbs ("we show", "the model forgets"), not chains of nominalisations. — A/B: rule preferred on 5, baseline on 5 of 16 held-out units → **not shipped**
  - evidence: findings.json top_vs_accepted/intro_nominal_per_k: top 56.7 vs accepted 62, paired diff -5.22 [95% CI -8.06, -2.4], dz -0.20, q 0.005, n=308 train pairs; held-out diff -7.02 n=133
  - exemplar: “Finally, by training NWM on action- and rewardfree video data, such as Ego4D, we demonstrate improved video prediction and generation performance in unseen environments.” (A02391afc Navigation World Models)
  - exemplar: “However, current reading comprehension datasets for theory of mind reasoning are simplistic and lack diversity, leading to brittle downstream models which, as we show, fail in the presence of even slight out-of-distribution perturbations.” (A036488ca Minding Language Models’ (Lack of) Theory of Mind: A Plug-and-Play Multi-Charact)
  - exemplar: “We show that simply using BERT-base with Pangu is sufficient for setting a new record on standard KBQA datasets, and larger LMs further bring substantial gains.” (A05958a56 Don’t Generate, Discriminate: A Proposal for Grounding Language Models to Real-W)
- **`paper.style.teaser-figure`** (checklist): Page 1 carries a teaser figure that shows the main idea or result. — structural (not A/B-testable on abstract+introduction); shipped on corpus evidence → **removed** (draft ablation: removal preferred, see Validation)
  - evidence: findings.json top_vs_accepted/page1_visual: top 0.425 vs accepted 0.344, paired diff +0.0812 [95% CI +0.013, +0.153], dz +0.13, q 0.082, n=308 train pairs; held-out diff +0.015 n=133
- **`paper.style.related-work-late`** (checklist): Related work comes after the method and results unless the method cannot be understood without it. — structural (not A/B-testable on abstract+introduction); shipped on corpus evidence → **removed** (draft ablation: removal preferred, see Validation)
  - evidence: findings.json top_vs_accepted/related_work_after_method: top 0.238 vs accepted 0.119, paired diff +0.119 [95% CI +0.0553, +0.187], dz +0.22, q 0.009, n=235 train pairs; held-out diff +0.0928 n=97

### Per-type skeletons (training-split award/high-citation papers)

Shipped as writer outlines: no (draft ablation: removal preferred).

- **method** (n top=203, control=255): abstract context (71% vs ctrl 82%) → problem (55% vs ctrl 67%) → method (100% vs ctrl 100%) → result (98% vs ctrl 99%); intro context (52% vs ctrl 58%) → method (57% vs ctrl 55%) → contributions (52% vs ctrl 69%); contribution form top {'numbered_inline': 53, 'none': 64, 'prose': 38, 'bulleted_list': 48} vs control {'bulleted_list': 116, 'prose': 33, 'none': 37, 'numbered_inline': 69}
  - context: “Minimum Bayes risk (MBR) decoding outputs the hypothesis with the highest expected utility over the model distribution for some utility function.” (A007fd8af Faster Minimum Bayes Risk Decoding with Confidence-based Pruning)
  - problem: “However, the standard samplingbased algorithm for MBR is substantially more computationally expensive than beam search, requiring a large number of samples as well as a quadratic number of calls to the utility function, limiting its applicability.” (A007fd8af Faster Minimum Bayes Risk Decoding with Confidence-based Pruning)
  - method: “We describe an algorithm for MBR which gradually grows the number of samples used to estimate the utility while pruning hypotheses that are unlikely to have the highest utility according to confidence estimates obtained with bootstrap sampling.” (A007fd8af Faster Minimum Bayes Risk Decoding with Confidence-based Pruning)
  - result: “Using orders of magnitude less training data, we obtain relative improvements ranging from 8% up to 40% in Hits@3 across different knowledge graphs containing factual information.” (A037d4004 Complex Query Answering with Neural Link Predictors)
- **analysis_mechanism** (n top=41, control=33): abstract context (93% vs ctrl 85%) → method (98% vs ctrl 88%) → result (95% vs ctrl 100%) → implication (63% vs ctrl 52%); intro context (54% vs ctrl 61%) → contributions (61% vs ctrl 61%); contribution form top {'prose': 12, 'bulleted_list': 14, 'numbered_inline': 6, 'none': 9} vs control {'bulleted_list': 10, 'numbered_inline': 7, 'prose': 9, 'none': 7}
  - context: “For many differentially private algorithms, such as the prominent noisy stochastic gradient descent (DP-SGD), the analysis needed to bound the privacy leakage of a single training run is well understood.” (A0baf0f9e Hyperparameter Tuning with Renyi Differential Privacy)
  - method: “In this work, we ﬁrst illustrate how simply setting hyperparameters based on non-private training runs can leak private information.” (A0baf0f9e Hyperparameter Tuning with Renyi Differential Privacy)
  - result: “Our results improve and extend the work of Liu and Talwar (STOC 2019).” (A0baf0f9e Hyperparameter Tuning with Renyi Differential Privacy)
  - implication: “Our theoretical and quantitative results suggest that the numerical gaps between contrastive and noncontrastive methods in certain regimes can be closed given better network design choices and hyperparameter tuning.” (A157840ce On the duality between contrastive and non-contrastive self-supervised learning)
- **benchmark_dataset** (n top=36, control=33): abstract context (89% vs ctrl 64%) → problem (53% vs ctrl 73%) → method (89% vs ctrl 94%) → resource (89% vs ctrl 97%) → implication (53% vs ctrl 48%) → result (94% vs ctrl 94%); intro context (61% vs ctrl 55%) → method (50% vs ctrl 58%) → contributions (61% vs ctrl 67%); contribution form top {'prose': 10, 'none': 8, 'numbered_inline': 12, 'bulleted_list': 6} vs control {'numbered_inline': 7, 'bulleted_list': 12, 'prose': 9, 'none': 5}
  - context: “Formulating selective information needs results in queries that implicitly specify set operations, such as intersection, union, and difference.” (A048a8fe1 QUEST: A Retrieval Dataset of Entity-Seeking Queries with Implicit Set Operation)
  - problem: “Information about pretraining corpora used to train the current best-performing language models is seldom discussed: commercial models rarely detail their data, and even open models are often released without accompanying training data or recipes to reproduce them.” (A16b0ab40 Dolma: an Open Corpus of Three Trillion Tokens for Language Model Pretraining Re)
  - method: “To study the ability of retrieval systems to meet such information needs, we construct QUEST, a dataset of 3357 natural language queries with implicit set operations, that map to a set of entities corresponding to Wikipedia documents.” (A048a8fe1 QUEST: A Retrieval Dataset of Entity-Seeking Queries with Implicit Set Operation)
  - resource: “Crowdworkers also assess the relevance of entities based on their documents and highlight attribution of query constraints to spans of document text.” (A048a8fe1 QUEST: A Retrieval Dataset of Entity-Seeking Queries with Implicit Set Operation)
  - implication: “Our results indicate that fine-tuning the model on a diverse set of tasks and instructions leads to a reduced sensitivity to variations in instructions for each task1.” (A0b4d2450 MultiInstruct: Improving Multi-Modal Zero-Shot Learning via Instruction Tuning)
  - result: “We show that stateof-the-art approaches struggle to solve these problems with the best model (GPT-4o) solving only 16.3% of the end-to-end set, and 46.1% of the scenarios.” (A671f2578 SUPER: Evaluating Agents on Setting Up and Executing Tasks from Research Reposit)
- **empirical_study** (n top=22, control=13): abstract context (91% vs ctrl 77%) → gap (64% vs ctrl 38%) → method (91% vs ctrl 92%) → result (100% vs ctrl 100%) → implication (73% vs ctrl 54%); intro context (59% vs ctrl 85%) → contributions (50% vs ctrl 46%); contribution form top {'numbered_inline': 8, 'prose': 3, 'none': 7, 'bulleted_list': 4} vs control {'bulleted_list': 4, 'numbered_inline': 3, 'none': 4, 'prose': 2}
  - context: “Ontological knowledge, which comprises classes and properties and their relationships, is integral to world knowledge.” (A1da610ad Do PLMs Know and Understand Ontological Knowledge?)
  - gap: “However, existing PLM-probing studies focus mainly on factual knowledge, lacking a systematic probing of ontological knowledge.” (A1da610ad Do PLMs Know and Understand Ontological Knowledge?)
  - method: “In this paper, we focus on probing whether PLMs store ontological knowledge and have a semantic understanding of the knowledge rather than rote memorization of the surface form.” (A1da610ad Do PLMs Know and Understand Ontological Knowledge?)
  - result: “We find that in moderation, adding multilingual data improves low-resource language modeling performance, similar to increasing low-resource dataset sizes by up to 33%.” (A69503f17 When Is Multilinguality a Curse? Language Modeling for 250 High- and Low-Resourc)
  - implication: “However, both the memorizing and reasoning performances are less than perfect, indicating incomplete knowledge and understanding.” (A1da610ad Do PLMs Know and Understand Ontological Knowledge?)
- **systems**: insufficient top papers of this type (< 8) (n=1)

### Validation

Separation (`validate.py separation`): AUC of the judge score for award/high-citation papers vs controls, checklist-free vs with checklist. CIs: 2000-draw bootstrap that resamples whole venue-years (keeps the matching); "within venue-year" counts only positive/control pairs from the same venue-year; permutation p swaps the two arms within papers.

| split / judge / checklist | repeats | n pos / ctrl | AUC free | AUC checklist | Δ [95% CI] | perm. p | within venue-year free → checklist, Δ CI |
|---|---|---|---|---|---|---|---|
| train/gpt-5.5/checklist-9 (vs all controls) | 1 | 321 / 476 | 0.757 [0.6844, 0.8578] | 0.765 [0.6999, 0.8424] | +0.008 [-0.0315, 0.0398] | 0.5992 | 0.730 → 0.675, [-0.0853, -0.0265] |
| train/gpt-5.5/checklist-9 (vs accepted controls) | 1 | 321 / 333 | 0.743 [0.6687, 0.8336] | 0.745 [0.6784, 0.825] | +0.003 [-0.0332, 0.0351] | 0.8716 | 0.698 → 0.648, [-0.0729, -0.0206] |
| test/gpt-5.5/checklist-9 (vs all controls) | 3 | 136 / 215 | 0.725 [0.6344, 0.8397] | 0.758 [0.6773, 0.8435] | +0.033 [-0.0176, 0.0795] | 0.1434 | 0.641 → 0.641, [-0.0794, 0.0691] |
| test/gpt-5.5/checklist-9 (vs accepted controls) | 3 | 136 / 145 | 0.696 [0.6082, 0.8036] | 0.718 [0.6414, 0.8076] | +0.022 [-0.026, 0.0688] | 0.3903 | 0.583 → 0.593, [-0.0617, 0.0847] |
| test/gpt-5.5/checklist-5 (vs all controls) | 3 | 136 / 215 | 0.725 [0.6344, 0.8397] | 0.739 [0.6528, 0.8312] | +0.014 [-0.0353, 0.0598] | 0.4388 | 0.641 → 0.636, [-0.089, 0.0838] |
| test/gpt-5.5/checklist-5 (vs accepted controls) | 3 | 136 / 145 | 0.696 [0.6082, 0.8036] | 0.695 [0.6115, 0.793] | -0.001 [-0.0525, 0.0482] | 0.9655 | 0.583 → 0.587, [-0.088, 0.1046] |
| test/deepseek-v4-pro/checklist-5 (vs all controls) | 1 | 136 / 215 | 0.702 [0.6182, 0.8037] | 0.690 [0.6197, 0.7584] | -0.011 [-0.0889, 0.0598] | 0.7666 | 0.659 → 0.616, [-0.2066, 0.0853] |
| test/deepseek-v4-pro/checklist-5 (vs accepted controls) | 1 | 136 / 145 | 0.678 [0.601, 0.7712] | 0.646 [0.58, 0.7158] | -0.033 [-0.1001, 0.0341] | 0.4453 | 0.614 → 0.578, [-0.2205, 0.1141] |

Draft A/B and per-rule ablation (all 9 rules + outlines) (`validate.py drafts`; writer `gpt-5.6-terra`, judge `gpt-5.5` via lib.longform.documents.pipeline._judge_pair + lib.production.critic.blind_pairwise (both presentation orders, rubric totals decide each order); 26 units: bf16_mechanism, parallel_paper3 + 24 held-out accepted-ordinary papers not used for rule selection). Each unit: candidate preferred in both orders / split / baseline preferred in both orders. Units whose revision added a number not in the draft, per arm: {'A': 0, 'B': 2, 'B-paper.style.abstract-number': 1, 'B-paper.style.abstract-verbs': 0, 'B-paper.style.contributions-prose': 0, 'B-paper.style.intro-quantified': 1, 'B-paper.style.caption-takeaway': 0, 'B-paper.style.limitations-section': 0, 'B-paper.style.intro-findings-first-person': 0, 'B-paper.style.teaser-figure': 0, 'B-paper.style.related-work-late': 1, 'B-outlines': 0}.

| comparison / judge | candidate | split | baseline | sign-test p | mean score [95% CI] | rubric margin [95% CI] |
|---|---|---|---|---|---|---|
| B_vs_A/production | 21 | 4 | 1 | 0.0 | +0.77 [0.5769, 0.9231] | +8.8 [5.9615, 11.6538] |
| B_vs_A/production/drafts_only | 2 | 0 | 0 | 0.5 | +1.00 [1, 1] | +8.5 [6, 11] |
| B_vs_A/style_blind | 13 | 3 | 10 | 0.6776 | +0.12 [-0.2308, 0.4615] | +3.8 [-0.1154, 7.9615] |
| B_vs_A/style_blind/drafts_only | 0 | 0 | 2 | 0.5 | -1.00 [-1, -1] | -8.5 [-9, -8] |
| ablate:paper.style.abstract-number/style_blind | 6 | 10 | 10 | 0.4545 | -0.15 [-0.4231, 0.1538] | -2.2 [-5.5769, 1.1154] |
| ablate:paper.style.abstract-number/style_blind/drafts_only | 1 | 1 | 0 | 1.0 | +0.50 [0, 1] | +3.0 [1, 5] |
| ablate:paper.style.abstract-verbs/style_blind | 12 | 5 | 9 | 0.6636 | +0.12 [-0.2308, 0.4615] | +0.5 [-3, 3.9231] |
| ablate:paper.style.abstract-verbs/style_blind/drafts_only | 2 | 0 | 0 | 0.5 | +1.00 [1, 1] | +6.0 [5, 7] |
| ablate:paper.style.contributions-prose/style_blind | 9 | 9 | 8 | 1.0 | +0.04 [-0.2692, 0.3462] | -0.2 [-3.5385, 2.9231] |
| ablate:paper.style.contributions-prose/style_blind/drafts_only | 0 | 2 | 0 | 1.0 | +0.00 [0, 0] | +2.0 [1, 3] |
| ablate:paper.style.intro-quantified/style_blind | 8 | 7 | 11 | 0.6476 | -0.12 [-0.4231, 0.2308] | -2.1 [-5.7308, 1.5769] |
| ablate:paper.style.intro-quantified/style_blind/drafts_only | 1 | 0 | 1 | 1.0 | +0.00 [-1, 1] | -1.0 [-10, 8] |
| ablate:paper.style.caption-takeaway/style_blind | 9 | 8 | 9 | 1.0 | +0.00 [-0.3077, 0.3077] | -1.8 [-5.2308, 1.5385] |
| ablate:paper.style.caption-takeaway/style_blind/drafts_only | 1 | 1 | 0 | 1.0 | +0.50 [0, 1] | +3.5 [-1, 8] |
| ablate:paper.style.limitations-section/style_blind | 6 | 11 | 9 | 0.6072 | -0.12 [-0.4231, 0.1923] | -0.2 [-3.2308, 2.8462] |
| ablate:paper.style.limitations-section/style_blind/drafts_only | 0 | 2 | 0 | 1.0 | +0.00 [0, 0] | -1.0 [-3, 1] |
| ablate:paper.style.intro-findings-first-person/style_blind | 6 | 11 | 9 | 0.6072 | -0.12 [-0.3846, 0.1923] | -0.9 [-4, 2.1154] |
| ablate:paper.style.intro-findings-first-person/style_blind/drafts_only | 0 | 1 | 1 | 1.0 | -0.50 [-1, 0] | -2.5 [-7, 2] |
| ablate:paper.style.teaser-figure/style_blind | 12 | 7 | 7 | 0.3593 | +0.19 [-0.1538, 0.5] | +1.8 [-1.7308, 4.9231] |
| ablate:paper.style.teaser-figure/style_blind/drafts_only | 0 | 1 | 1 | 1.0 | -0.50 [-1, 0] | -0.5 [-5, 4] |
| ablate:paper.style.related-work-late/style_blind | 7 | 13 | 6 | 1.0 | +0.04 [-0.2308, 0.3077] | -0.1 [-3.1538, 2.7308] |
| ablate:paper.style.related-work-late/style_blind/drafts_only | 0 | 2 | 0 | 1.0 | +0.00 [0, 0] | +1.0 [0, 2] |
| ablate:outlines/style_blind | 11 | 8 | 7 | 0.4807 | +0.15 [-0.1923, 0.4615] | +0.6 [-3.0385, 3.9615] |
| ablate:outlines/style_blind/drafts_only | 1 | 1 | 0 | 1.0 | +0.50 [0, 1] | +3.5 [0, 7] |

Harm criterion: a style rule (or the outlines) is harmful when B without it is preferred in both orders on more units than full B (style-blind judge). Removed: outlines, paper.style.abstract-verbs, paper.style.contributions-prose, paper.style.related-work-late, paper.style.teaser-figure.

Draft A/B of the shipped configuration (`validate.py drafts`; writer `gpt-5.6-terra`, judge `gpt-5.5` via lib.longform.documents.pipeline._judge_pair + lib.production.critic.blind_pairwise (both presentation orders, rubric totals decide each order); 26 units: bf16_mechanism, parallel_paper3 + 24 held-out accepted-ordinary papers not used for rule selection). Each unit: candidate preferred in both orders / split / baseline preferred in both orders. Units whose revision added a number not in the draft, per arm: {'A': 0, 'B': 1}.

| comparison / judge | candidate | split | baseline | sign-test p | mean score [95% CI] | rubric margin [95% CI] |
|---|---|---|---|---|---|---|
| B_vs_A/rule_free | 12 | 11 | 3 | 0.0352 | +0.35 [0.0769, 0.6154] | +3.5 [0.9615, 6.0385] |
| B_vs_A/rule_free/drafts_only | 0 | 2 | 0 | 1.0 | +0.00 [0, 0] | -2.0 [-3, -1] |

Harm criterion: a style rule (or the outlines) is harmful when B without it is preferred in both orders on more units than full B (style-blind judge). Removed: none.
<!-- evidence:end -->
