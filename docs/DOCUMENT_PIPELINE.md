# Document writing pipeline

Authority record for the staged writing pipeline: `lib/longform/documents/`
(stages, gates, rules, venue kits, render QA, review calibration),
`lib/production/critic.py` (shared candidate critic and blind A/B),
`scripts/document_pipeline.py` (CLI), `.agents/skills/document-pipeline/`
(the skill other projects' Workers load) and
`evaluations/paper_review_calibration/` (review-gate calibration).

## Problem

The owner had to fix his documents (a resume, then papers) by re-prompting in
natural language, one correction at a time, and the same corrections came
back: caveats and hedging, missing experience, wrong paper counts, old
templates, unpolished figures, unclear novelty. Each fix lived only in one
conversation. Slides and film already have a fixed, checkpointed production
graph with gates, repair and blind judging; documents had none (owner,
2026-11-18: 「我们有没有办法把这个写作流程规范化？……有一个类似生产PPT或者视频一样的固定流程」).
The Global Router clarified that the first target is paper writing, with the
resume as the second document type.

## Decision

**One fixed graph for every document type, on the existing production
owners.** `run_document_pipeline` declares eleven typed nodes as
`AgentToolWorkflow`s and runs them through `lib.production.workflows.run_workflow`
→ `run_stages`: the same checkpoint, retry, suffix invalidation and
cancellation semantics slides, film and long-form reports use. A document
type (`types.py`: `paper`, `resume`, `longform`) varies required sections,
reviewer persona and rubric, and which audits apply; it never forks the graph.

| # | Stage | Persisted artifact (`<workdir>/artifacts/`) | Gate (machine check) |
|---|---|---|---|
| 1 | intake | `01-intake.json/.md`: brief, template kit provenance, owner directives, rule ids | required brief fields; judge model ≠ writer model; previous version exists; **paper.official-template**: kit fetched in this run from the venue's official archive, with upstream commit and archive SHA-256 |
| 2 | dossier | `02-dossier.json/.md`: facts with status/source, figures, bibliography, facts awaiting the owner | every fact sourced; local sources exist and contain every number the fact states; confirmed facts name the owner statement; figures exist, labelled, captioned; resume: first-author counts only from accepted papers |
| 3 | evidence | `03-evidence.json/.md`, `experiment_requests.json`: claim → evidence → strength table | a row per claim; no-evidence claims rated `none`; every weak/none claim has a structured experiment request to the project lead; at least one claim supported |
| 4 | novelty | `04-novelty.json/.md`: retrieved 2025–2026 candidates, positioning table, related-work BibTeX | a literature source answered; ≥1 retrieved 2025–2026 work positioned; each row has what-they-do / what-we-add, `delta_clear`, and cites a strong/moderate contribution |
| 5 | outline | `05-outline.json/.md`: sections with one reader takeaway each | exact section ids and order; distinct takeaways; only usable facts; no weak/unsupported claim; every supported claim used; every figure placed once; resume: complete coverage, roles only from owner-confirmed timeline facts |
| 6 | draft | `06-draft.json/.md`: LaTeX per section | rule lint (hedges/fences); **no invented numbers** (every decimal/percentage is in a usable fact); no pipeline internals in the text (dossier, fact ids, project lead); no pipeline internals in the text (dossier, fact ids, project lead); citations resolve; references resolve; no placeholders or stray `\section` |
| 7 | critique | `07-critique.json/.md`: venue-rubric scores, overall, owner checklist, findings, calibrated verdict | well-formed integer scores; every checklist item marked; findings name real sections |
| 8 | revise | `08-revise.json/.md`: rewritten sections, lint refusals, blind adoption check | revised sections pass the draft gate; a rewrite is adopted only if the blind pairwise judge prefers it in both orders |
| 9 | render | `09-render.json/.md`, `build/`: compiled PDF and QA report | compile success; 0 overfull boxes; 0 undefined refs/citations; body (a label after the conclusion, read from `.aux`) within the page limit; kit files byte-identical to the fetch; paper quality owner rows (figures captioned/labelled/referenced, fonts embedded, raster DPI) clean. One repair attempt feeds the failures back to the writer |
| 10 | blind | `10-blind.json/.md`: both presentation orders with scores and reasons | **paper.blind-ab**: the new version wins in both orders; otherwise nothing is delivered |
| 11 | deliver | `<doc_dir>/<output_dir>/`: PDF, `source/`, `manifest.json`, audit copies | delivered PDF hash equals the render-QA PDF |

Each artifact is written before its gate runs, so a refused stage is
inspectable. Model stages get one gated retry with the gate's errors as
feedback (the production stage retry budget); deterministic stages do not
retry. `intake` runs in its own non-resumable workflow, so the official
template is refetched every run; the remaining stages resume from checkpoints
bound to the digest of brief, kit commit/files, owner directives, rules and
dossier content. A rerun with unchanged inputs makes no model call.

**Owner corrections are rules, not prompts.** `rules.py` is the registry. Each
correction is one of: a `lint` rule (deterministic phrase check over the
drafted prose), a `gate` rule (enforced by the named stage gate), or a
`checklist` item (the independent reviewer must mark it pass/fail with a
quote; failures become revision findings). Writer prompts, reviewer
checklists and gates all read the registry, and each rule records its origin.
`scripts/document_pipeline.py rules <type>` prints them.

| Owner correction | Rule | Enforcement |
|---|---|---|
| newest official top-venue template | `paper.official-template` | intake gate + render drift check |
| polished figures | `paper.figure-qa` | render gate (`lib.paper.checks` rows, fonts, DPI) |
| strong story, real novelty, confident, no fences | `paper.confident-voice`, `paper.no-fences`, `paper.story`, `paper.real-novelty` | draft/revise lint + reviewer checklist |
| novelty and solid experiments | `paper.novelty`, `paper.solid-experiments`, `paper.claims-supported` | novelty and evidence gates; experiment requests |
| independent reviewer critique, then targeted revision | `paper.independent-review` | critique gate; revise adopts only on a blind win |
| no overfull boxes, no broken refs, page limit | `paper.compile-clean` | render gate |
| blind A/B against the previous draft | `paper.blind-ab` | blind gate |
| resume looked empty because cut experience was missing | `resume.complete-coverage` | outline gate |
| count only accepted papers as first-author | `resume.accepted-first-author` | dossier gate |
| lead with Tofu's distinctive features, performance brief | `resume.lead-distinctive` | reviewer checklist |
| crisp insight, not detail | `resume.insight-not-detail` | reviewer checklist |
| no "for researchers", no "directing AI", no caveats | `resume.confident-voice` | lint |
| the organisation move (owner-confirmed) | `resume.org-timeline` | outline gate: roles only from owner-confirmed facts |
| a one-page and a multi-page version | brief `variant` | intake gate; one run per variant |

Owner-specific facts (which papers were accepted, team names and dates) are
dossier facts with `status: confirmed` and `source.owner` naming his statement;
they stay in the private dossier, never in this repository. Facts awaiting his
confirmation are listed in `02-dossier.md` and `manifest.json`
(`awaiting_owner`) and cannot be written.

**My Context carries the owner's own cross-project directives.** Intake reads
the owner's active owner-stated or verified My Context items (read-only,
through `lib.memory.user_profile` `context_path` + `profile_items`, the same
selection the owner profile renders) and passes them to the writer prompts;
their count is in `01-intake.md`. The pipeline never writes My Context: an
item becomes active only when the owner states or confirms it
([context engineering](modules/context_engineering.md), "Owner profile").
Document-type rules live in the rule registry because they are product
policy for every document of that type, and My Context items are cross-project
and render into every Turn.

**Reviewer calibration.** The critique prompt (`review.py::review_messages`)
asks for an `overall` 1–10 accept likelihood. `evaluations/paper_review_calibration/run.py`
sends the identical prompt over real ICLR 2025 submissions with public
decisions (OpenReview data mirrored on Hugging Face; the OpenReview API itself
returns a bot challenge from this host), picks the threshold maximizing
Youden's J on a calibration half and reports held-out AUC, accuracy and
balanced accuracy. Its summary is committed at
`lib/longform/documents/calibration/paper_review.json` with the judge model
and the prompt digest. The critique verdict applies that threshold only when
both match; otherwise it reports `calibrated: false` and why. The verdict is a
reported prediction recorded in the manifest, not a delivery gate: a paper
the calibrated reviewer would reject still reaches render and blind A/B,
because the owner decides submission. See "Calibration result" below.

**Shared critic.** The slides outline critic, the film script critic and the
document pipeline now share `lib/production/critic.py`: `critic_prompt`
(byte-identical prompt contract for the existing critics, specified by
`tests/longform/test_document_pipeline.py`), `parse_critic_choice`, and
`blind_pairwise`, which judges a candidate against a baseline twice with
swapped positions under anonymous labels and prefers the candidate only when
it wins both. Documents pass whole texts (`digest_chars=None`); plan critics
keep their original cut.

**Reused owners.** Stage execution: `lib/production/stages.py`,
`workflows.py`. Compile: `lib.paper.latex_layout.compile_project`. Figure,
font, DPI and source checks: `lib.paper.checks` (`run_source_checks`, and the
new file-level `run_pdf_file_checks` beside `run_pdf_checks`). Literature
search: `lib.research.literature.AcademicDiscovery` with
`lib.paper.arxiv.search_arxiv`. Network: `lib.http_transport.client`. Model
calls: `lib.llm_dispatch.api.dispatch_chat` (CLI processes route it to the
running server's OpenAI-compatible endpoint through the existing
`evaluations/slides_bench/llm_bridge.py`, as the slides benchmark does).

## Running it on a paper (any project)

A Worker in another project runs the pipeline on its own paper directory; it
needs read/write access to that directory only. Load the
`document-pipeline` skill, or:

1. In the paper directory, write `document_brief.json`:
   `type` (`paper`), `venue` (`acl` or `iclr`), `title`, `authors`, `topic`,
   optional `novelty_queries`, `audience`, `goal`, `voice`, `lang`,
   `page_limit` (body pages), `writer_model`, `judge_model` (a different
   model), `project_lead` (who receives experiment requests), `previous_pdf`
   (the current draft, for the blind A/B) and `output_dir`.
2. Write `dossier.json`: `facts` (`id`, `kind` ∈ claim/contribution/result/
   background, `text`, `status` ∈ confirmed/sourced/unconfirmed, `source`
   `{path|url|owner}`, `evidence` = ids of result facts or figure labels),
   `figures` (`label` `fig:…`, `file`, `caption`, `supports`) and `bib`
   (BibTeX file).
3. Run, from the Tofu checkout, with TeX Live (latexmk; pgfplots/inconsolata
   when the paper uses them) and poppler on `PATH`:
   `.venv/bin/python3 scripts/document_pipeline.py run <paper_dir>`.
   Exit 0 = delivered to `<paper_dir>/<output_dir>/`; exit 2 = a gate refused,
   with the stage, errors and artifact folder printed. Fix the input (or answer
   the experiment requests) and rerun; unchanged stages resume.
4. `scripts/document_pipeline.py status <workdir>` lists the stage artifacts.

The sample paper `tests/longform/fixtures/sample_paper/` is a complete,
synthetic example of both files.

## Alternatives considered

- **A writing prompt template or a longer system prompt.** Rejected: it is
  the re-prompting the owner asked to end. Nothing checks it, so a correction
  can silently regress.
- **A new document engine beside `lib/production`.** Rejected (principle 4):
  it would duplicate checkpoints, retry and invalidation. The graph is a
  workflow declaration; only stage callbacks and gates are new.
- **Extending `lib/longform/recipe.py` (topic → markdown report).** Rejected
  as the vehicle: it has no venue kit, render or review stages and its
  sections are web-research summaries. The new package lives beside it in the
  long-form owner and reuses the same workflow helpers.
- **Storing owner corrections only as My Context items.** Rejected for
  type rules: My Context is cross-project, renders into every Turn and only
  the owner can activate items, so an agent cannot make a correction durable
  there by itself; and a prompt line is not a check. My Context remains the
  owner's channel and is applied to every run.
- **A hand-picked accept threshold for the review gate.** Rejected by the
  owner's route note; the threshold comes from calibration or is reported as
  absent.
- **Judging only one presentation order.** Rejected: LLM judges have a
  position bias, so a single-order win does not show preference.
- **Vendoring venue templates.** Rejected: the rule is the newest official
  template, so intake refetches every run and refuses unregistered venues.

## Limits

- Provider content filter: on 2026-11-18 the Claude routes (claude-opus-5.5,
  claude-opus-5) answered the sample paper's outline prompt with
  `finish_reason: content_filter` and empty content on every attempt, while
  each half of the prompt alone passed. The pipeline reports this as
  `<model> returned no content … (finish_reason=content_filter)`; the sample
  brief uses `gpt-6.1-sol` as writer. The cause is upstream and was not
  diagnosed further.

- Registered venues: ACL family (`acl-org/acl-style-files`, used by ACL, EMNLP,
  NAACL, ARR) and ICLR (`ICLR/Master-Template`, newest `iclrYYYY/`). NeurIPS
  and ICML publish yearly archives at year-specific URLs; add them to
  `venues.py` with their official URL when needed.
- The blind judge and the reviewer read extracted text, not page images;
  render QA is the visual gate.
- The resume type's rules and gates are implemented and specified by tests;
  an end-to-end resume run is not part of this change.
- The calibrated reviewer predicts decisions from text the judge may have seen
  in training (ICLR 2025 is public); treat its AUC as agreement with real
  decisions.

## Calibration result

Run 2026-11-18, judge `gpt-5.5`, prompt digest `331462ef…` (committed in
`calibration/paper_review.json`; raw rows and the parquet cache under the
private `.eval-runs/paper-review-calibration/`). 100 ICLR 2025 submissions
with mirrored full text, 50 accepted (Oral/Spotlight/Poster) and 50
rejected, chosen by id hash; split by id hash into 48 calibration and 52
held-out papers (23 accepted).

| Measure | Value |
|---|---|
| Threshold (`overall` ≥, Youden's J on the calibration half) | 4 (J = 0.44, calibration AUC 0.78) |
| Held-out AUC | 0.749, bootstrap 95% CI [0.621, 0.870] |
| Held-out accuracy / balanced accuracy at the threshold | 0.673 / 0.666 |
| Held-out true-positive / true-negative rate | 0.609 / 0.724 |

The judge is harsh in absolute terms (accepted papers score 2–6, median 4),
so only the calibrated threshold is meaningful, never the raw score. Any
change to the review prompt or the checklist changes the digest; the verdict
then reports `calibrated: false` until the evaluation is rerun.

**Data rules.** Rules found by evidence (the corpus analysis of award-winning
and highly cited papers against matched controls) plug in through
`lib/longform/documents/style_rules.json` (`rules.py::DATA_RULES_PATH`):
`lint` rules (phrases, or a `metric` checked by `style_metrics.check`) and
`checklist` items with exemplars, each with `origin` and `evidence`. Gate
rules need code and are refused from data. A new checklist item requires the
calibration rerun above.

## Verification

`tests/longform/test_document_pipeline.py` specifies rule application, every
stage gate, the critic contract, a full scripted run over the sample paper
with a real TeX compile (delivery, resume without model calls) and a blind
refusal that delivers nothing. Neighbors: `tests/production/`,
`tests/slides/` and `tests/motion_video/` critic users, `tests/paper/test_paper_quality.py`.
