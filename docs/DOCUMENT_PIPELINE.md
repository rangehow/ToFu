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

**Registry enforcement roles.** Writer context renders `lint` and `gate`
rules as mandatory constraints. `checklist` means reviewer assessment:
these items are excluded entirely from initial writer context, including
the five unproven style items. Reviewers still assess all checklist items
through the existing protocol, and their findings can drive targeted revision.
This applies equally to paper story/claims and resume presentation checklists;
mandatory lint/gate requirements and explicit owner directives are unchanged. This corrects the former rendering of
all registry entries as mandatory, which made the five unproven paper-style
items mandatory despite the failed registered A/B. No flag, new rule kind,
or experiment re-scoring is introduced; see `PAPER_STYLE_EVIDENCE.md`.

**One fixed graph for every document type, on the existing production
owners.** `run_document_pipeline` declares eleven typed nodes as
`AgentToolWorkflow`s and runs them through `lib.production.workflows.run_workflow`
→ `run_stages`: the same checkpoint, retry, suffix invalidation and
cancellation semantics slides, film and long-form reports use. A document
type (`types.py`: `paper`, `resume`, `longform`) varies required sections,
reviewer persona and rubric, and which audits apply; it never forks the graph.

| # | Stage | Persisted artifact (`<workdir>/artifacts/`) | Gate (machine check) |
|---|---|---|---|
| 1 | intake | `01-intake.json/.md`: brief, template kit provenance (landing page, archive URL, commit, SHA-256, year, page limit, anonymity rule), owner directives, rule ids, existing-draft snapshot | required brief fields; no field outside the brief schema (see "Brief fields"); `role` ∈ revise/final/rebuttal/camera_ready, `final` and `camera_ready` only with a draft; `paper_type`, `track` and the role's stage have a published venue limit; `source` is the draft main; judge model ≠ writer model; page limit ≤ the venue's body limit; previous version exists; **paper.official-template**: the brief's venue/`venue_year`/track/stage resolves to a pinned edition (else refused: not pinned, not released, superseded; a `template_draft_on` draft is never submission-ready and refuses `final`/`camera_ready`), the archive fetched in this run matches its pinned sha256; **paper.template-freshness**: the venue's pages and archive still say what the pin records |
| 2 | dossier | `02-dossier.json/.md`: facts with status/source, figures, bibliography, facts awaiting the owner | every fact sourced; local sources exist and contain every number the fact states; confirmed facts name the owner statement; figures exist, labelled, captioned; resume: first-author counts only from accepted papers |
| 3 | evidence | `03-evidence.json/.md`, `experiment_requests.json`: claim → evidence → strength table; each cited source file is sent as its cited span, never whole | a row per claim; no-evidence claims rated `none`; every weak/none claim has a structured experiment request to the project lead; at least one claim supported |
| 4 | novelty | `04-novelty.json/.md`: retrieved 2025–2026 candidates, positioning table, related-work BibTeX | a literature source answered; ≥1 retrieved 2025–2026 work positioned; each row has what-they-do / what-we-add, `delta_clear`, and cites a strong/moderate contribution |
| 5 | outline | `05-outline.json/.md`: sections with one reader takeaway each | exact section ids and order; distinct takeaways; only usable facts; no weak/unsupported claim; every supported claim used; every figure placed once; resume: complete coverage, roles only from owner-confirmed timeline facts |
| 6 | draft | `06-draft.json/.md`: LaTeX per section | rule lint (hedges/fences); **no invented numbers** (the provenance check below, per section and per edit); no pipeline internals in the text (dossier, fact ids, project lead); citations resolve; references resolve; no placeholders or stray `\section` |
| 7 | critique | `07-critique.json/.md`: venue-rubric scores, overall, owner checklist, findings, calibrated verdict | well-formed integer scores; every checklist item marked; findings name real sections |
| 8 | revise | `08-revise.json/.md`: rewritten sections, lint refusals, blind adoption check, dropped patches with their reason | revised sections pass the draft gate; a rewrite is adopted only if the blind pairwise judge prefers it in both orders; a patch dropped by the reference check is reported as `N edits dropped: reference check (errors)`, never as "not needed" |
| 9 | render | `09-render.json/.md`, `build/`: compiled PDF and QA report | compile success; 0 overfull boxes; 0 undefined refs/citations; body (a label after the conclusion, read from `.aux`) within the page limit; kit files byte-identical to the fetch; paper quality owner rows for the sources the document actually inputs (figures captioned/labelled/referenced, fonts embedded, raster DPI) clean; no venue-forbidden package; **paper.template-conformance**: every row of the conformance gate on the final build (official style files loaded unmodified per the `.fls`, no layout overrides, layout equal to the official sample, review mode, page limit, anonymity, required sections; see "Venues"). Repairs repeat while each one strictly lowers the failure measure; the best render is kept |
| 10 | blind | `10-blind.json/.md`: both presentation orders with scores and reasons, rubric totals over both orders | **paper.blind-ab**: the new version wins in both orders; otherwise nothing is delivered. One win each is reported as **no preference** with both orders' scores, and as position bias when the judge picked the same position twice |
| 11 | deliver | `<doc_dir>/<output_dir>/`: PDF, `source/`, `manifest.json` (with `provenance`), audit copies; `11-deliver.md` the provenance report | **deliver.provenance**: every number and citation in the delivered text traces to a source, else nothing is written and each unsourced item is named; delivered PDF hash equals the render-QA PDF |

Each artifact is written before its gate runs, so a refused stage is
inspectable. Model stages get one gated retry with the gate's errors as
feedback (the production stage retry budget); deterministic stages do not
retry. `intake` runs in its own non-resumable workflow, so the official
template is refetched every run; the remaining stages resume from checkpoints
bound to the digest of every input they read: the brief, kit commit/files,
owner directives, the full rule texts and origins, the existing-draft
snapshot, the bytes of `dossier.json` and of every file it names (sources,
figures, bibliography), and the stage implementation (the package sources,
its calibration and the shared critic). A changed input reruns its stage and
every later one; a rerun with unchanged inputs makes no model call. (The
pilot run on the mechanism paper reused a stale dossier because the version
hashed only brief, kit, directives and rule ids.)

**Model requests stay within the endpoint.** Every model call is measured
before it is sent; a request over the endpoint's body cap
(`lib/http_transport/body_policy.py` `DEFAULT_BODY_CAP`) is refused with the
stage label and how to narrow it, instead of failing with HTTP 413. The
evidence stage sends each cited source file as its cited span: the fact's
`source.lines` (`"a-b"`, `[a, b]`, or `{path: span}`), otherwise the lines
(JSON/JSONL: the leaves, with their key paths) holding the numbers the fact
states (its decimals and percentages when it has any), plus a CSV/TSV header.
A file with no cited span is named, not inlined.

**Owner corrections are rules, not prompts.** `rules.py` is the registry. Each
correction is one of: a `lint` rule (deterministic phrase check over the
drafted prose), a `gate` rule (enforced by the named stage gate), or a
`checklist` item (the independent reviewer must mark it pass/fail with a
quote; failures become revision findings). Writer prompts, reviewer
checklists and gates all read the registry, and each rule records its origin.
`scripts/document_pipeline.py rules <type>` prints them.

| Owner correction | Rule | Enforcement |
|---|---|---|
| pinned official template for the venue's year | `paper.official-template`, `paper.template-freshness`, `paper.template-conformance` | intake gate + render conformance gate |
| polished figures | `paper.figure-qa` | render gate (`lib.paper.checks` rows, fonts, DPI) |
| strong story, real novelty, calibrated claims, no verbal fences (owner, Turn e09572cb) | `paper.no-fences`, `paper.no-overclaim`, `paper.calibrated-claims`, `paper.story`, `paper.real-novelty` | draft/revise lint + reviewer checklist |
| learn style and structure from best/highly cited papers | `paper.style.*` (data rules) | lint + reviewer checklist; evidence in [PAPER_STYLE_EVIDENCE.md](PAPER_STYLE_EVIDENCE.md) |
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

**No invented numbers or citations: a hard gate.** `provenance.py` traces
every number in an output (percentages, decimals, counts, CI bounds,
p-values, table cells, figure labels such as "Figure 1") and every
`\cite`/`\bibitem` key to the sources: the draft (draft mode), each usable
dossier fact's text and the whole text of the result files it cites, the
figure captions, and the bibliography (dossier and novelty). A value that
rounds a source value at the precision written (0.8734 → 0.873), or restates
a source fraction as a percentage where the output writes a % sign (and a
source percentage as a fraction), is sourced and recorded with the source
value. Layout markup is not content: comments, command options, label/ref/
cite/file/package/environment arguments, box sizes and dimensions are
blanked first, and a digit inside a name (BF16, GPT-2, `\nm{gap_7b}`) is part
of that name. The same check gates each drafted or revised section and each
revise edit (`draft.no-invented-numbers`), and `deliver` reruns it on the
delivered text (an existing draft's changed files, or every written
section) before it writes anything: any unsourced item refuses delivery as
`deliver.provenance: unsourced number|citation <value> at <file:line>:
<sentence>`. `scripts/document_pipeline.py provenance <workdir>` runs it
read-only over a past run's applied edits and revised/rendered text, and
`validate.py provenance-audit` over the style studies' revisions.
Numbers spelled as words ("three") are not checked.

Why a whole-pipeline gate: the earlier check covered only decimals and
percentages at edit time, so an integer, a count or a repair edit could pass,
and the style study kept its own number extractor. That extractor reported
the style study's one "added number" (held-out unit C003cf5e3, arm B): the
source wrote "Figure.1" and the revision "Figure 1", and the extractor
skipped a digit after a period. The gate traces it, and
`tests/longform/test_document_provenance.py` keeps that case with a
mutation that must be refused. Alternatives considered: matching numbers
against the evidence audit's claims only (misses table cells and counts the
audit does not quote), and an LLM fact-checker (not deterministic; it can
miss what a token match cannot).

**Provenance audit, 2026-11-18** (read-only; nothing edited). Pipeline runs:
the bf16 mechanism copy `pipeline_out/` runs 3–7 (`attempt3`…`attempt6`,
`mechanism-input/.pipeline`; 6–46 outputs each, up to 761 numbers and 138
citations), the hand-built diagnostic run 7 build, and the sample ACL paper
(`.eval-runs/document-pipeline/sample-e2e-20261118`,
`sample-draft-e2e-20261118` plus its `first-run-before-gate.patch`): **0
unsourced, 0 rounded**. Runs 1–2 produced no text. No pipeline run of Paper 3
and no resume run exists; Paper 3 was revised only in the style studies.
Style studies (`validate.py provenance-audit` →
`evaluations/paper_style_corpus/provenance_audit.json`): 457 cached
revisions (the shipped configuration's arms A and B on all 26 units, the
ablation arms, the 16-unit rule A/B, the `ab` mode's bf16/Paper 3 pair),
15,948 numbers and 397 citations. Each revision is checked against the unit
it revised, and a draft is checked against every committed version of its
file, because Paper 3 changed after the study. Unsourced:

| Revision | Unit | Item | Location | What it is |
|---|---|---|---|---|
| `drafts_ab/rev-9591dadd433da025de8c.txt` (ablation arm) | C0822766b | number 1.2 | line 29, "1.2 LIMITATIONS" | a section number the writer invented for an added heading (source has only "1.1") |
| `drafts_ab/rev-e7885d7aa9d9e8e06b52.txt` (ablation arm) | C0822766b | number 1.2 | line 26, "1.2 LIMITATIONS" | same |

Neither is in the shipped configuration's 52 revisions. 11 values traced by
rounding, all "§6" written for a source "§6.1" (`rule_ab`, unit C6b934f47).

**The A/B judge is style-blind; the reviewer reads the registry.** The
writer, the lints and the critique reviewer read every rule from
`rules_for()` with its origin, so a correction recorded in `rules.py` reaches
them in the same change (the pilot's corrected voice rule once lost to a
hardcoded "penalize hedging"). The blind and revise A/B judge
(`pipeline._judge_pair`: the delivery gate and the revise adoption check)
reads no rule: its prompt is the type's reviewer persona and rubric, the task
(title, audience, goal), the venue's requirements (venue, paper type, track,
role, body-page limit) and the two documents. A judge told the rules rewards
rule-following itself: on 26 drafts the rule-fed judge preferred the
style-rule revision 21–4–1 while a judge not told the style rules gave
13–3–10 (p = 0.68, [PAPER_STYLE_EVIDENCE.md](PAPER_STYLE_EVIDENCE.md)).
`test_ab_judge_is_style_blind_task_venue_and_documents_only` refuses any
registry rule id, text or origin in the judge prompt.

**Rule admission.** A new style or figure rule (`paper.style.*`,
`paper.figure.*`, `paper.table.*`, or any writer/lint rule claimed to improve
quality) enters the registry only after a style-blind A/B: documents written
or rendered with the rule against without it, judged by `_judge_pair` in
both presentation orders, with win/tie/loss and a sign test or bootstrap CI
recorded in the rule's evidence document. A rule that does not win is not
admitted; a shipped rule whose ablation shows harm is removed. Owner
corrections (rules whose origin is the owner's words) are policy and need
no A/B, but they never reach the judge either.

**One judge for every quality A/B.** `_judge_pair` is the only judge of a
quality comparison: revise adoption, delivery, the style studies
(`validate.py drafts` and `rules` through `pipeline_judges`) and the
figure/table before/after (`evaluations/figure_style_ab/`). A table or
figure pair is a pair of renders: a candidate `{"part": …, "images": [png]}`
sends the same style-blind prompt followed by the images in candidate order.
The rule-A/B study's own area-chair prompt was removed. A second judge with
its own prompt measures something the delivery judge does not.
`tests/longform/test_document_judge.py` refuses any other pairwise or
pick-a-version prompt in `lib/longform`, the CLI and those evaluations, and
any `blind_pairwise` whose judge does not come from `_judge_pair`.
Per-render rubric scores may be kept as diagnostics, never in a verdict.

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
because the owner decides submission. The ICLR 2025 historical set is
same-conference-year ID-hash split and balanced text-available selection,
not a time/venue-separated test; the 100 valid unique cached judgments comprise
50 rejects, 47 posters, 2 spotlights and **one oral**. The stored accept/reject
AUC is 0.749 (bootstrap 95% CI 0.621–0.870), balanced accuracy 0.666;
oral AUC and probability calibration cannot be inferred from these figures.
Training contamination and sampling/reviewer bias remain unresolved, and the
mirror's reuse license is not verified. `review_verdict` now returns
`calibrated: false` rather than applying the old threshold, even when its
model and prompt digest match. The evaluation script refuses publication via
`--write-calibration` for this dataset; its separated-split helper rejects
missing years, duplicated IDs/titles and single-year or single-class holdouts.
No new model run or oral prediction has been verified. See "Calibration result"
below.

**Shared critic.** The slides outline critic, the film script critic and the
document pipeline now share `lib/production/critic.py`: `critic_prompt`
(byte-identical prompt contract for the existing critics, specified by
`tests/longform/test_document_pipeline.py`), `parse_critic_choice`, and
`blind_pairwise`, which judges a candidate against a baseline twice with
swapped positions under anonymous labels and prefers the candidate only when
it wins both. One win each is `no preference`, returned with both orders'
scores, their totals over both orders and `position_bias` (the same position
won twice). Documents pass whole texts (`digest_chars=None`); plan critics
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
`run` launches through the common deliverable launcher (kind `document`,
[`modules/production.md`](modules/production.md#headless-deliverable-launch-contract)):
its preflight checks inputs, writable outputs and `latexmk` before the route
is installed, and the outcome is recorded in `<workdir>/launch_result.json`.

## Venues: pinned official templates, per year

A paper built on the wrong year's template, or one that otherwise breaks the
venue's format, can be desk-rejected (owner, 2026-11-18). So template
correctness is a hard gate with three parts: a versioned registry that pins
each venue/year/track to its official archive, a refusal when no such pin
exists, and a conformance check of every rendered PDF.

### Registry

`lib/longform/documents/venue_templates.json` (format
`tofu.venue-templates/1`, owned by `venue_registry.py`) holds one **edition**
per venue, year and kit. A `pinned` edition records:

- `archive`: the official download URL, its sha256 and the date retrieved
  (`upstream_commit` for GitHub snapshots). `archive_source` is the official
  page that links the archive, with a verbatim quote.
- `files`: the sha256 of every kit file the build copies. `owned_files` are
  the venue's own style files, which the gate enforces; `main_style_file` must
  be loaded; `style_pattern` (e.g. `icml\d{4}`) recognises another year's
  style file by name.
- `page_limits` per paper type and stage (`submission`, `rebuttal`,
  `camera_ready`). A stage the venue states no limit for is absent and
  refused. `max_total_pages` covers the venues whose extra pages hold only
  references (AAAI, IJCAI).
- `anonymity` (the rule, `double_blind`, `pdf_markers`: the anonymous author
  block the review build prints, `forbid_acknowledgements`), `modes`
  (options or commands forbidden or required per stage), `required_sections`
  (exact official heading plus official `alternatives`, optional
  `todo_markers` for checklists, `stages`/`tracks`), `exempt_sections`,
  `forbidden_packages`, `tracks` (track → style option).
- `layout`: the official sample's page size, text block, margins, column
  layout, body font size and family, and baseline. These are measured by the
  gate's own probe when the edition is pinned.

Every rule carries `source: {url, quote}`: an official https page (or
`archive_member`, a file inside the official archive) and a verbatim quote.
The freshness check re-reads those quotes.

The other statuses are:

- `not_released`: an official source explicitly says the template/call is not
  published yet (e.g. a "coming soon" statement), with the quoted evidence.
- `unverified`: HTTP 404, connection failure or incomplete evidence prevents
  verification. This is **not proof that no template has been released**.
  Both statuses refuse normal builds; an explicit `draft_fallback` may be
  selected only as a labelled non-submission-ready draft.
- `superseded`: the cycle has closed. The edition is kept so its style files
  are recognised as the wrong year.
- `unpinned`: the kit is published but could not be hashed, with the reason.

`validate_catalogue` refuses an edition without these facts. Tests run it on
the shipped file.

Editions on 2026-11-18 (`scripts/document_pipeline.py venues` prints the live
list):

| Edition | Status | Kit (sha256 prefix) | Body pages (submission / camera-ready) | Required in the paper |
|---|---|---|---|---|
| `icml-2027` | pinned | media.icml.cc icml2027.zip `52384a2a` | 8 / 9 | Impact Statement; AI Disclosure Statement (policy takes precedence over kit heading) |
| `icml-2028`, `icml-workshop-2027` | unverified / explicitly coming soon | CFP 404 / "will be posted here soon" | — | — |
| `neurips-2026` (tracks `main`, `eandd`) | pinned | media.neurips.cc …NeurIPS_2026.zip `82473931` | 9 / 10 | NeurIPS Paper Checklist, no `\answerTODO` |
| `neurips-2027` | unverified (CFP, Dates 404) | draft on `neurips-2026` only | — | — |
| `iclr-2027` | pinned | media.iclr.cc iclr-2027-style-files.zip `0d940dfa` | 9 / 10 (rebuttal 10) | AI use statement |
| `iclr-2028` | unverified | draft on `iclr-2027` only | — | — |
| `colm-2026` | pinned | COLM-org/Template tag 2026 = `963ddd1d` | 9 / — | — |
| `colm-2027` | unverified | draft on `colm-2026` only | — | — |
| `arr-rolling`, `acl-2027`, `naacl-2027`, `eacl-2027` | pinned | acl-style-files commit `d5adc823` (`35b158b7`); branch head checked | long 8 / 9, short 4 / 5 | Limitations |
| `eacl-srw-2027` (workshop) | pinned | same ACL kit | long 8/9, short 4/5, thesis proposal 8/9 | Limitations |
| `emnlp-2026` | superseded (held Oct 2026) | ACL kit | — | — |
| `emnlp-2027` | unverified (site unreachable) | — | — | — |
| `cvpr-2027` | pinned | author-kit-CVPR2027-v1-latex.zip `da1ec5db` | 8 / — | LLM Usage Statement (desk-reject if absent) |
| `cvpr-workshop-2027` | unpinned | Common style known; named workshop CFP/length rules unknown | — | Refuse submission readiness |
| `iccv-2027`, `eccv-2028` | unverified (404) | — | — | — |
| `eccv-2026` | superseded | paolo-favaro/paper-template `da8c09c4` | — | — |
| `aaai-2027` | pinned | aaai.org/authorkit27 `9406a545` | 7 (9 total, 8-9 references only) / — | — (ethics statement counts toward the 7 pages; 21 forbidden packages) |
| `ijcai-2027` | pinned (kit published before the CFP; rules from `ijcai27.tex`) | FormattingGuidelines-IJCAI-27.zip `1f829e86` | 7 (9 total) / — | — |
| `kdd-2027` | unpinned (ACM template page answers 403; Cycle 2 call not out) | — | — | — |

Workshops require a named workshop's official CFP as well as the common style.
EACL 2027 SRW has such evidence; the generic CVPR workshop entry does not and
is refused. Other unnamed workshops are likewise refused, never inferred from
the parent conference. A failed HTTP request establishes unknown availability,
not non-release. `not_released` entries require a quoted affirmative official
statement. NeurIPS main/E&D track is checked from the official style's effective
flags at compiled document begin/end, not inferred from source option spelling.
The official style's pinned default is main; arbitrary intermediate TeX state
changes are outside this boundary probe's guarantee.

### Explicit owner portfolio policy (not generic venue eligibility)

Problem: the owner's portfolio targets CCF-A conferences and never COLM. A global venue blacklist would incorrectly
restrict other users. Decision: the trusted portfolio launcher passes
`run_document_pipeline(..., portfolio_policy='owner-ccf-a')` or CLI
`--portfolio-policy owner-ccf-a` on both `run` and `conformance`. For the common
launcher JSON contract, set `parameters.portfolio_policy: "owner-ccf-a"`
(`contracts/deliverable_launch_v1.schema.json`); `_run_document` propagates
it to the same recipe port. This is not a
brief-controlled switch, not an owner-ID constant, and not path inference.
Generic invocation (`None`) keeps COLM and other venue support unchanged.
Project leads must wire their actual build/submission launchers; a passing
helper test does not prove their Makefiles select it. A per-project handoff
is required before claiming deployment to the owner's workflow.

`portfolio_policy.py` reads `ccf_official_list.json`: exact official **seventh
edition (2026), announced 2026-03-31, corrected 2026-04-09**, linked by
[CCF's official index](https://www.ccf.org.cn/Academic_Evaluation/By_category).
The file pins the exact linked PDF URL and SHA-256 and quotes individual rows
with PDF page numbers. The old category HTML disagrees with that PDF, so it
is not the authority: PDF p57 lists ICLR A; p58 lists IJCAI B. Main A venues
verified there are AAAI, NeurIPS, ACL, CVPR, ICCV, ICML, ICLR (p57) and
SIGKDD (p35); EMNLP/ECCV/NAACL/IJCAI are B (p58). Unlisted is unknown,
not a model-memory non-A classification. The list excludes short/demo/
findings/workshop papers from publication-recognition scope. That recognition
scope is reported separately and does not change a target conference's tier.

The owner's gate refuses COLM unconditionally, unknown/non-A targets, and
ARR without a named CCF-A `commitment_venue` (e.g. `acl`, only if that is the
actual full-paper commitment). It re-fetches the official PDF through the
shared network authority and compares its pin. Unavailable source, changed
PDF, missing edition/date/URL or unknown tier fails owner submission
readiness; it never silently falls back to memory or the stale HTML table.
Template conformance and tier eligibility are separate checks; both must
pass. Tests cover their composition and CLI policy propagation.

### Resolution: no pin, no build

**Consumer boundary.** This edition authority belongs to the staged document
pipeline, not every paper compiler. Ordinary browser preview constructs
`PaperBuildRequestV1` in `frontend/src/features/paper/preview-controller.ts`;
`routes/api_v1/paper_builds.py` delegates it to
`lib/paper/builds/service.py::start_preview`. Its optional `venue_year` field
enforces the conference year for a **local** ordinary preview: the handler
reads template references (`\usepackage`, including a comma-separated package
list, `\RequirePackage`, and included preambles via `\input`/`\include`)
inside the authorized root and matches them to `venue_registry` editions. When
those editions agree on one pinned year, a different `venue_year` is refused
before any build is scheduled, naming that expected year and the received year.
A shared style name that spans several pinned years (`acl.sty` is the rolling
ARR edition, ACL 2027 and superseded EMNLP 2026) cannot pin one expected year
and is not checked. An omitted field is the documented default for older
documents: it asserts no year and previews as before. A source that names no
pinned template is not year-checked. Overleaf and remote-desktop previews
refuse `venue_year` because their template is not readable on this host; they
never accept and ignore it. The current browser controller does not set the
field, so the user-facing preview asserts no year; direct API callers can. The
preview still does not resolve page limits, tracks or submission readiness.
`lib/tasks_pkg/handlers/paper_quality.py::_check_verdict`
combines source QA and retained-build PDF checks, not edition conformance.
Its passing headline is not proof of an exact-year template or conference
submission eligibility. No conference-submission operation is provided by
these boundaries. Adding one requires an explicit venue/year consumer contract,
not an implicit latest-edition choice or a broader compiler-success gate.

`tests/longform/test_venue_year_integration.py` exercises the existing retained
`neurips-2026` catalogue identity across December 31 / January 1: a synthetic
brief with a valid blank prior PDF passes intake with zero errors, then normal
render orchestration assembles the year-specific frame and reaches a compiler
spy followed by the exact-edition conformance boundary. Missing year,
unverified 2027 and unregistered 2028 refuse before any fetch; a missing prior
PDF still refuses under the unchanged intake gate. Archive/freshness/compiler
and conformance boundaries are spies: the synthetic kit bytes are not original
assets and are not hash-verified. This establishes orchestration admission and
handoff only, not a successful TeX build or submission readiness. The spied
conformance result deliberately refuses the render gate as unverified; the test
does not manufacture a submission-ready result.

The same test file exercises `start_preview` itself against a synthetic source
that loads the retained `neurips_2026` template: `venue_year: 2026` passes the
year check and reaches the start-bound boundary; `2027`/`2028` are refused with
both expected and received years named; a comma-separated package list and a
template loaded from an included preamble are still checked; a shared `acl`
style does not invent an expected year; Overleaf and desktop previews refuse
the field; and an omitted field keeps the older source previewing under the
documented default. This is preview year equivalence only, **not**
preview/submission equivalence: page limits, tracks, conformance and readiness
are still the submission path's. No catalogue pin, gate severity, or CCF scope
is changed.

`venue_registry.resolve(venue, year, track, paper_type, stage, draft_on)`
backs `venues.venue_terms` and the intake stage:

- Year-addressed venues need the brief's `venue_year`. ARR (`arr`) is the only
  rolling edition.
- A year missing from the registry is refused: "no icml 2031 template is
  pinned … a previous year's template is never used silently".
- An `unverified` year is refused with the evidence, e.g. "NeurIPS 2027:
  unable to verify template availability (checked 2026-11-18:
  https://neurips.cc/Conferences/2027/CallForPapers -> HTTP 404) …".
- Only with brief `template_draft_on: "<earlier edition>"` does the build use
  that edition. It is then labelled `DRAFT on the NeurIPS 2026 template …;
  not submission-ready`; `submission_ready` stays false in intake, render and
  the delivery manifest; and `role: final` or `camera_ready` is refused.
- `superseded` and `unpinned` editions are refused.

Intake fetches the pinned archive (`fetch_pinned_kit`) and refuses when its
sha256 differs from the pin. For a GitHub snapshot it instead accepts the
same commit marker plus every pinned member hash, because GitHub regenerates
snapshot zips. Intake and freshness call the **same** `verify_pinned_archive`
implementation; matching ZIP comments alone cannot establish freshness.
Unrecorded redirect destinations are `unverifiable` and refused, even when the
returned bytes match. Verified archive destinations are pinned as `final_url`;
other official source redirects require an explicit `source_redirects` record.
Intake then runs the freshness check (below) and refuses unless the
verdict is `current`. Offline, nothing is submission-ready, because nothing
can be verified.

**Limits per paper.** The stage comes from `role`: `submission` for
`revise`/`final`, `rebuttal` for `rebuttal`, `camera_ready` for
`camera_ready`. Frames switch the style option with the stage (`[accepted]`,
`[final]`, `\iclrfinalcopy`, CVPR/AAAI without `[review]`/`[submission]`).
Rejected alternative: reading `role: final` as camera-ready. `final` marks a
finished draft, which is usually still a submission, and giving it the extra
page would pass a paper the venue then rejects.

**Exempt and required headings are exact words.** A heading ends the
page-limited body, or satisfies a requirement, only if its words equal an
official heading (any case, `\section*` through `\subsubsection*`). ICML 2027
has an unresolved source discrepancy. Its [CFP](https://icml.cc/Conferences/2027/CallForPapers)
and [AI policy](https://icml.cc/Conferences/2027/UseOfGenerativeAIByAuthors)
require **AI Disclosure Statement** and explicitly exempt it from the page
limit. The pinned kit's `example_paper.tex` says **AI Use Statement**.
The explicit CFP/policy requirement takes precedence: the gate requires
`AI Disclosure Statement`; `AI Use Statement` alone does not prove compliance.
Both headings remain page-limit exempt for counting, but exemption is not
satisfaction of a required section. The catalogue records both sources in
`policy_conflicts`; a pass is not a guarantee of acceptance.

**Kit-required packages.** Render refuses, before compiling, a package the
kit's style files `\RequirePackage` that neither the kit nor `kpsewhich`
provides, and names the `tlmgr install` command (`icml2027.sty` requires
`forloop`).

### Conformance gate (every rendered PDF)

`conformance.check_paper(paper_dir, main, edition, stage, track, …)` copies
the tree to scratch; the paper is never written. It then inserts a body-end
marker after the last counted text, in whichever `\input` file holds it, and
compiles with latexmk `-usepretex` (`latex_layout.compile_project(pretex=)`).
The probe writes the effective layout after the preamble and after the last
page. Standard kernel figure/table boxes (including starred variants) carry a
deferred page write; every recorded body float must have a matching in-range
shipout before the general page-limit row can pass. The report retains the
text marker's `body_pages` separately from `body_float_pages`.

A terminal page break followed by a control/group with no positive literal
prose or standard float evidence leaves marker placement unresolved. LaTeX's
`\pagebreak` optional argument (`[ 4 ]`, newlines and comments included) is
consumed as one delimited lexical unit; when its extent or a following tail
cannot be established, the gate refuses as uninstrumentable instead of reading
the argument or a macro argument as prose. This
uncertainty survives a literal child input containing the exempt heading;
the gate leaves the scratch source uninstrumented and returns `unverifiable`,
not a marker-only measured overflow. It never trims groups to guess where a
macro's arguments end. This conservative check is not a TeX interpreter:
arbitrary macros, conditionals, custom output routines, and nonstandard floats
remain outside the proven scope. Rows:

| Check | Fails when |
|---|---|
| `compile` | no PDF or TeX errors |
| `template.official-files` | from the `.fls` recorder: the edition's main style file was not loaded; a loaded venue-owned `.sty`/`.cls` (or the `.bst` named in the `.blg`) differs from its pin; a kit file copied into the paper was edited; a loaded style file is another pinned edition's (by hash) or another year's (by `style_pattern`) |
| `template.no-overrides` | the sources override geometry, fonts or spacing: `geometry` (except COLM's `[pass]`), `\setlength`/`\addtolength` or assignment of text block, margins, column separation, baseline, display/caption/float skips, `\linespread`, `\baselinestretch`, redefined `\normalsize`, layout packages (`savetrees`, `fullpage`, `setspace`, `titlesec`, …), a class point size the official sample does not use, a preamble `\fontsize…\selectfont` |
| `template.forbidden-packages` | a package the edition forbids (AAAI) |
| `template.layout` | any probed dimension, font size or family differs from the official sample's (column state only at the start; appendices may switch to one column) |
| `template.mode` | a submission build uses a detected camera-ready option or command, or lacks a required review option |
| `template.track` (NeurIPS) | effective compiled main/E&D/other-track flags at document boundaries disagree with the requested track, or no measurement exists |
| `content.forbidden-sections` | an anonymous IJCAI 2027 submission contains a Contribution Statement heading |
| `content.references-only-extra-pages` | AAAI 2027 uses extra pages and the expanded-source tail is not demonstrably references-only; appendices are not generically exempt |
| `page-limit` / `page-limit.total` | the body marker or a standard body float ships past the stage's limit (exempt headings excluded), marker/float evidence is unverifiable, or the PDF exceeds the total |
| `anonymity.*` (double-blind submission) | the anonymous author block is missing; a listed identity, an e-mail address or PDF `/Author` metadata appears; an acknowledgements section is present where forbidden |
| `required-sections.*` | a required heading is missing (similar headings are named) or a checklist keeps a TODO answer |

AAAI 2027's pages 8–9 are exclusively references, not generic appendix space.
An appendix entirely within the seven-page content budget is allowed. If a
longer PDF contains an appendix, the current first-exemption marker cannot prove
its placement; the gate refuses as unable to verify rather than treating it as
exempt. The content helper accepts a separate last-nonreference-page proof, but
the ordinary `bodyend` value is deliberately not passed as that proof.
A `thebibliography` environment name or `bibitem` label alone is likewise not
proof: enclosed content is scanned for common headings, and over-budget manual
bibliographies remain `unverifiable` without independent non-reference-page
evidence. This is not a claim that a clean manual bibliography violated a rule.
Ordinary generated `bibliography`/`printbibliography` tails retain the structural
check; all body/total page gates still apply. The common heading scanner in
`lib.paper.latex_layout` is reused by conformance and content rules, covering
section/paragraph/subparagraph, stars, optional short titles and balanced nested
formatting. These source checks do not interpret arbitrary TeX macro/conditional
semantics or certify bibliographic meaning.

Render runs the gate on its final build and adds every failing row to the
render failures (`paper.template-conformance: …`), so a detected failure blocks delivery. Delivery uses the exact checked PDF,
not an earlier separately compiled PDF. The existing delivery gate compares the
candidate bytes against both render-QA and recorded conformance SHA-256 **before
creating the delivery directory**, publishes those same checked bytes, then
checks delivered bytes again. Venue papers require a nonempty conformance hash;
missing proof is refused. A mismatch with the inspected PDF refuses delivery with a distinct
`delivered PDF differs from the template-conformance PDF` error, independent
of portfolio policy. The manifest records edition, pin,
conformance rows and `submission_ready`. Static names/URLs checks cannot prove
semantic anonymity; boundary layout probes do not inspect every local TeX
group; quoted-rule matching cannot establish completeness of future rules.
The gate reports these limitations and must not be described as guaranteeing
no desk rejection. TeX length/overfull diagnostics also run on its probe log.
URL rows distinguish `identity_match` (a URL contains a supplied author
identity) from `manual_review` (an unverified repository/tracking URL).
A third-party licence URL is not a proven identity leak: it is surfaced as
`REVIEW` and `manual_review` in JSON, conservatively withholding readiness
until reviewed, rather than blanket-allowing all GitHub links. The marker
walk refuses external TeX inputs before writing, and archive extraction
rejects escaping paths; read-only audits must not write into formal sources.

Knowledge's project gate checks the same things: vendored style-file hashes,
no preamble geometry/font/spacing overrides, year-specific anonymous mode,
page limit, exact headings, identity leaks, plus negative tests. This
catalogue format and `scripts/document_pipeline.py conformance` are the
general version, so a project needs no gate of its own. Its make target calls:

```sh
scripts/document_pipeline.py conformance <paper_dir> --venue icml --year 2027 \
    [--track T] [--paper-type P] [--stage submission|rebuttal|camera_ready] \
    [--main main.tex] [--identities "Name One,Name Two"] [--json]
```

Exit 0 conforms, 2 is refused or non-conforming; every row is printed.

### Freshness and updating a venue template

`scripts/document_pipeline.py venue-freshness [edition …]` re-fetches every
pinned edition's archive and every quoted rule page, and probes each edition's
`newer_edition_probes` (a redirect to an old edition does not count). It also
checks a moving kit branch's head (`upstream_head`; ACL's
`commits/master.atom`) and re-tries a `not_released` edition's URLs. Verdicts:
`current`; `changed` (archive hash, a rule quote gone, a newer edition page
answering, the branch moved, an unreleased page now answering); or
`unverifiable` (a source could not be fetched or redirected to an unrecorded
destination). Redirected future-year pages never establish a current verdict.
Exit 0 only when all are
current. Intake runs it for the paper's edition on every venue run. On
2026-11-18 the earlier report of all 24 editions as `current` was too strong:
404/connection-failed editions are now `unverified`, not proven unreleased,
and unpinned editions cannot be current. The check runs again immediately
before rendering; render/delivery do not resume stale readiness. Probe timeouts
are unverifiable, not evidence of absence. Quote matching detects changed or
removed recorded rules, not every possible newly added rule; independent
review of the current CFP remains necessary.

**Updating a venue template** (a new year, or `changed`):

1. Read the venue's official CFP / author-kit page. Never copy an older
   edition.
2. `scripts/document_pipeline.py venue-pin <official archive URL> --sample
   <the kit's example .tex> [--kit-dir D] [--suffix .sty --suffix …]`. It
   prints the archive sha256, kit file hashes, the sample's measured `layout`
   and the anonymous lines the sample prints.
3. Add or replace the edition in `venue_templates.json`: status `pinned`, the
   measured fields, and each rule with its official URL and a verbatim quote
   (page limits per type and stage, anonymity markers, required and exempt
   headings, modes, tracks, `newer_edition_probes`). Mark the previous year
   `superseded` once its cycle closes. For a venue that has only announced
   dates, use `unverified` unless an affirmative official statement proves the
   kit is not released; HTTP failure alone is never such proof.
4. `scripts/document_pipeline.py venue-freshness <edition>` must print
   `current`. `scripts/document_pipeline.py check-venue <edition>` must
   compile the official sample. Run `tests/longform/test_venue_conformance.py`.

Rejected alternatives:

- **Probing for the newest kit on every run** (the design until
  2026-11-18). When the coming year's page was missing, it fell back to the
  current year's kit. That is exactly the silent wrong-year build the owner
  forbade, and it had no recorded hash or rules to compare against.
- **Vendoring kits in each project.** That makes copies with no freshness
  check. Parallel's `papers/assets/venues/` mirror stays a project record;
  this catalogue is the authority.
- **Checking only the main style file's hash.** A paper can load the right
  file and still shrink margins in its preamble. The recorder check, override
  scan and layout probe each catch what the others miss.

## Existing drafts

Both pilot papers already exist as LaTeX. `run --draft main.tex` (brief:
`"draft": {"main": "main.tex"}`, optional `"pdf"`) revises that draft
instead of writing from the dossier, through the same eleven stages:

- **intake** snapshots the draft tree into `<workdir>/source/` (the owner's
  files are never written) and compares its style files with the fresh kit.
  Outdated copies are replaced in the build, and the replacement is reported.
- **dossier** reads the draft's own figures and `.bib`. `dossier.json` is
  optional: facts there add to what the draft states.
- **Judges read the expanded text.** Review, evidence, outline and the A/B
  judges read the draft with its own macros expanded
  (`existing_draft.macros`/`expand_macros`: `\newcommand`/`\def` without
  conditionals, and keyed tables such as `\nm{key}` defined by
  `\expandafter\def\csname nm@key\endcsname{0.42}`). The pilot's reviewer
  read raw `\nm{...}` and penalised "unresolved placeholders". A key the
  table lacks stays visible as written. Edits still address the raw sources.
- **evidence** extracts every claim the draft makes as a verbatim quote of
  the expanded or raw text (the gate refuses paraphrases), with the
  figure/table labels it cites. Weak or uncited claims become experiment
  requests to the project lead.
- **novelty** positions the draft's claims against the closest 2025-2026 work.
- **outline** is the draft's own section list plus one reader takeaway each.
- **draft** imports the text unchanged and records its rule findings (hedges,
  fences) as revision targets.
- **revise** asks the writer for `{old, new}` edits. An edit applies only if
  `old` occurs exactly once in the draft and `new` passes the draft gate: no
  number absent from the draft and the dossier, no citation outside the
  draft's bibliography, no hedges. It also must not drop a number stated by a
  claim the evidence audit rated strong or moderate
  (`draft.keep-supported-results`, added after the first live run, where the
  writer emptied the abstract's supported results), and must not reference a
  label the paper does not define. A cleveref list `\cref{a,b}` names two
  labels (the pilot's reference check read it as one undefined label and
  silently dropped all 24 edits). The edited paper is adopted only if the
  blind judge prefers it in both orders. `08-revise.md` shows the outcome as it
  happened, the patch and every refused edit with its reason.
- **render** rebuilds the draft on the newest kit and repairs by targeted
  edits. Figures are never edited: the owner's figure files pass through
  unchanged unless the figure-QA rows fail them. `09-render.md` lists every
  refused repair edit.
- **`role: final`** marks a finished draft. Revise accepts copy edits only:
  the reviewer's findings are listed in `08-revise.md` as not applied (they
  reword claims; the pilot's run 4 applied two such findings as added caveat
  sentences and overran the page limit), the writer is asked only for the
  rule findings, and an edit that changes a number (after macro expansion),
  a citation or a reference, or adds or removes a sentence, is refused. Render repair may only fix references, floats and
  layout: an edit whose prose words (markup and reference words such as
  "Figure" removed) differ from the old text is refused as a prose rewrite
  and listed. In the pilot, fixing five unreferenced floats applied 21
  prose rewrites; all 21 are refused under this rule.
- **blind** compares against the draft's PDF (`draft.pdf`), or the
  unmodified draft compiled.
- **deliver** writes `source/` (the owner's tree with the accepted edits) and
  `revision.patch` against the draft, so the lead can apply or review it.

The main file must sit at the top of the directory the pipeline runs on (it
is compiled with its own relative `\input` paths).

The page-limited body ends at the first of the bibliography, `\appendix` or
an unnumbered section the venue exempts (the edition's `exempt_sections`,
taken from the official kit). The body-end label is attached to the last
counted text, so it records the page where that text ends. On its own line
before the next heading it would record the heading's page, one page late
whenever a page break falls between them. When the body ends inside an
unnumbered section the venue does not exempt, the render failure names it.
For ICML 2027, `AI Disclosure Statement` is the required CFP/policy heading
and is exempt. A report counting it toward the body limit is incorrect.
Always audit current source/PDF hashes; old pipeline copies are distinct
from current formal papers.

## Model choice and provenance

The brief names `writer_model` and `judge_model` per run, and `run
--writer-model M` overrides the writer for that run (recorded in the brief, so
checkpoints bind to it). A brief without `writer_model` uses the documented
routing default (`docs/MODEL_REGISTRATION.md`); the pipeline refuses if that
default is the judge. Every model call is appended to
`artifacts/models.json` (stage call, role, requested model, model the
provider reported). A call answered by any other model than the one requested
fails the stage, so nothing switches silently. The manifest carries the full
list, and `status <workdir>` prints it.

The Claude routes refuse frontier-LLM research content with
`finish_reason: content_filter`. Another assignment showed this is bound to
history and content, not to the prompt. The stage then fails with that reason
and the instruction to name a writer from another model family. Rerunning
with `--writer-model` resumes from the failed stage.

## Runbook for a project lead

From your paper directory (the one holding `main.tex`), with the running Tofu
server, TeX Live (`latexmk`) and poppler (`pdftotext`, `pdffonts`) on `PATH`:

1. **Check the venue once:**
   `cd $TOFU && .venv/bin/python3 scripts/document_pipeline.py check-venue icml-2027`.
   Exit 0 means the pinned official kit compiles here. Exit 2 prints the TeX
   errors, usually a missing TeX Live package (`tlmgr install <name>`).
2. **Write `document_brief.json`** in the paper directory:
   ```json
   {"type": "paper", "venue": "icml", "venue_year": 2027, "title": "…", "authors": "Anonymous",
    "topic": "search phrase for the closest 2025-2026 work",
    "novelty_queries": ["optional extra phrases"],
    "audience": "ICML reviewers", "goal": "what the paper must convince them of",
    "voice": "confident, precise, no hedging", "lang": "en",
    "writer_model": "gpt-6.1-sol", "judge_model": "gpt-5.5",
    "project_lead": "who runs requested experiments",
    "draft": {"main": "main.tex", "pdf": "main.pdf"},
    "output_dir": "pipeline_out"}
   ```
   `page_limit` is optional (the venue's limit applies; a smaller value is
   allowed). Keep `judge_model: gpt-5.5`, the calibrated reviewer.

   **Brief fields.** Required: `type`, `title`, `audience`, `goal`, `voice`,
   `lang`, `judge_model`, `output_dir`; papers also `venue`, `project_lead`,
   `topic`. Optional: `writer_model`, `page_limit`, `previous_pdf`, `draft`
   (`main`, `pdf`), `role` (`revise` default; `final`: copy edits and
   layout-only render repair; `rebuttal`: revision under the venue's rebuttal
   limit; `camera_ready`: as `final`, under the camera-ready limit),
   `paper_type` (ACL `long`/`short`), `track` (NeurIPS `main`/`eandd`),
   `source` (the draft main file, checked),
   `venue_year` (required for every year-addressed venue; picks the pinned
   edition), `template_draft_on` (an earlier edition to draft on while the
   target year is not released; never submission-ready), `novelty_queries`,
   `authors`; non-paper types `template_dir`, `main_template`, `engine`,
   resumes `variant`. Intake refuses any other field rather than ignoring it.
3. **Optional `dossier.json`, built from your results:** facts the paper may
   state beyond the draft (`id`, `kind` claim/contribution/result,
   `text` with the numbers, `status: sourced`, `source.path` → the result file
   that contains every number). The dossier gate checks each number against
   its file. Without a dossier, the draft is the only evidence.
4. **Run:**
   `cd $TOFU && .venv/bin/python3 scripts/document_pipeline.py run <paper_dir> --draft main.tex`
5. **Read the exit code:**

   | Exit | Meaning | What to do |
   |---|---|---|
   | 0 | Delivered to `<paper_dir>/<output_dir>/`: PDF, `source/`, `revision.patch`, `manifest.json`, the evidence/novelty/review/blind/render reports, `models.json` | Review `revision.patch`, apply it to your draft if you agree, and send `experiment_requests.json` to whoever runs experiments |
   | 2 | A stage gate refused. The stage, its errors and `<paper_dir>/.pipeline/artifacts/` are printed | `evidence`: answer the experiment requests. `novelty`: sharpen the delta. `render`: fix the cited overfull box, page overrun or unreferenced figure. `blind`: the judge preferred your draft, or had no preference (one win each, often position bias); read `10-blind.md` for both orders' scores. Writer `content_filter`: rerun with `--writer-model` from another family. Then rerun; unchanged stages resume |
   | 2 | `timeout` or `harness_error` (printed with the outcome record) | A harness error is a defect: report it with `<workdir>/launch_result.json` |
   | 1 | `setup_error` or `infra_error`, before any model call when the preflight catches it: missing brief, dossier/draft or a file the brief names, unwritable workdir or `<paper_dir>/<output_dir>`, no `latexmk`; or a storage or model-endpoint failure during the run | Read the failed `preflight` rows in the printed record (also `<workdir>/launch_result.json`); fix and rerun |
6. `scripts/document_pipeline.py status <paper_dir>/.pipeline` lists the stage
   artifacts and the model behind every call.

## Running it on a paper (any project)

A Worker in another project runs the pipeline on its own paper directory; it
needs read/write access to that directory only. Load the
`document-pipeline` skill, or:

1. In the paper directory, write `document_brief.json`:
   `type` (`paper`), `venue` (one of `scripts/document_pipeline.py venues`), `title`, `authors`, `topic`,
   optional `novelty_queries`, `audience`, `goal`, `voice`, `lang`,
   optional `page_limit` (body pages), `writer_model`, `judge_model` (a different
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
- **Telling the A/B judge the registry rules** (the judge until
  2026-11-18). Rejected: the gate then measures rule compliance, which the
  lints and the reviewer already check, and rewards an unproven rule
  (21–4–1 vs 13–3–10 style-blind). Withholding only the rules under test was
  also rejected: every rule is under test when it is added, and an owner
  rule in the judge still favours text that parrots it.
- **Vendoring venue templates.** Rejected: see "Venues" (the pinned registry
  is the one authority; intake refetches and verifies it every run).

## Limits

- Claude writers refuse frontier-LLM research content (`content_filter`);
  see "Model choice and provenance".
- A venue that edits a quoted rule's wording makes freshness report `changed`
  until the edition is re-pinned; that blocks runs for that edition (safe
  direction). KDD 2027 is unpinned (ACM's template page refuses this client);
  fresh-paper frames exist for ICML, NeurIPS, ICLR, COLM, ACL, CVPR and AAAI,
  while IJCAI and ECCV serve existing drafts and the conformance gate only.
- The layout probe compares the state after the preamble and after the last
  page; a geometry change confined to the middle of the body is caught only
  by the source override scan.
- AAAI's frame uses the kit's mandated preamble; its forbidden-package list is
  copied from AuthorKit27 and enforced on the sources the paper inputs.
- The delivery judge and the reviewer read extracted text, not page images;
  render QA is the visual gate. The same judge reads images only for a
  table/figure pair.
- The provenance gate matches values, not meaning: a sourced number reused
  for another quantity passes (the evidence audit judges claims), small
  integers are present in almost every source, and numbers written as words
  are not checked.
- The resume type's rules and gates are implemented and specified by tests;
  an end-to-end resume run is not part of this change.
- The calibrated reviewer predicts decisions from text the judge may have seen
  in training (ICLR 2025 is public); treat its AUC as agreement with real
  decisions.

## Calibration result

Current selection receipts require valid row shape, unique IDs and complete
per-class coverage, bound to corpus, selector and selected bytes. The completed
receipt travels into scored rows, cache checks and summary provenance. Empty or
underfilled selections refuse rather than acquiring a `completed` receipt.
Selection visits each class in deterministic ID-hash order until the quota is
filled or no candidates remain; an arbitrary prefix could miss eligible papers.
Only nonblank text paragraphs before the references or bibliography count toward
body eligibility, and the same filtered paragraphs supply the scored text.
The calibration bridge checks the actual compatible response choice for an
affirmative normal finish, a text message, and no tool or function calls before
projecting the row. Usage metadata and parseable review JSON are not completion
evidence. These provenance checks do not relax the publication guard.

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
then reports `calibrated: false` until the evaluation is rerun. The
2026-11-18 voice correction (checklist `paper.calibrated-claims` replacing
the old fence item) and listing each checklist item's origin (so the
reviewer reads the owner's words, as the A/B judge does) changed the digest,
so the critique verdict is uncalibrated until that rerun.

**Data rules.** Rules found by evidence (the corpus analysis of award-winning
and highly cited papers against matched controls) plug in through
`lib/longform/documents/style_rules.json` (`rules.py::DATA_RULES_PATH`):
`lint` rules (phrases, or a `metric` checked by `style_metrics.check`) and
`checklist` items with exemplars, each with `origin` and `evidence`. Gate
rules need code and are refused from data. A new checklist item requires the
calibration rerun above. The same file may carry per-paper-type
`outlines` (abstract move order, introduction paragraph roles, one exemplar
per move); `rules.py::outline_guidance` renders them into `_writer_context`,
so the outline, draft and revise prompts read them from the registry like
every rule. The shipped `paper.style.*` rules, their effect sizes,
exemplars, the skeletons and the validation are recorded in
[PAPER_STYLE_EVIDENCE.md](PAPER_STYLE_EVIDENCE.md); the file is generated by
`evaluations/paper_style_corpus/validate.py`, never edited by hand.

**What the style evidence is worth (2026-11-18).** The powered validation is
negative: the checklist does not make a judge separate award/high-citation
papers from matched controls (held-out AUC Δ +0.014 [−0.035, +0.060]), and
on 26 drafts a style-blind pipeline judge does not prefer revisions written
with the style rules (all rules: 13 vs 10, p = 0.68; both pilots lose). Four
rules and the outlines were removed by ablation; 5 checklist items remain as
reviewer prompts, not as a quality lever. The blind-A/B judge then listed
every registry rule, so it favoured text that followed them (21–1 in the
same test); it is now style-blind (see "The A/B judge is style-blind"), and
a style rule is admitted only with a style-blind A/B behind it. Re-judged by
the rule-free judge, the shipped 5 items vs none give 12 vs 3 (11 split) on
the same 26 units that selected them, and no preference on both real drafts
(bf16, Paper 3), which the rule-fed judge had passed for the style version.

## Verification

`tests/longform/test_venue_terms.py` specifies the per-paper venue terms:
limits by paper type and stage, refusal of unpublished stages and unknown
tracks, ICLR's exact exempt headings at subsection level, the NeurIPS track
option, the pinned COLM tag, the kit-required package check and the brief's
`paper_type`/`track`/`role` through intake and its gate.
`tests/longform/test_document_pipeline.py` specifies rule application, every
stage gate, the critic contract, a full scripted run over the sample paper
with a real TeX compile (delivery, resume without model calls), a blind
refusal that delivers nothing, the page-limit bound, model provenance and no-silent-switch, and an
existing-draft run with a real compile: targeted edits only, an invented
number refused, the outdated style file replaced, the owner's figure bytes
and draft untouched. The pilot-defect specifications cover checkpoint
invalidation by `dossier.json` and cited files, macro expansion before
review/evidence scoring, role/source/unknown-field intake checks, the
role=final render repair (5 references applied, 21 prose rewrites refused
and listed), truthful dropped-patch reporting, cleveref lists, no-preference
blind reporting, the style-blind A/B judge, cited excerpts of a
multi-MB file and the pre-send body-cap refusal. Real fetch-and-compile per venue is
`scripts/document_pipeline.py check-venue`, run by hand (network).
`tests/longform/test_venue_conformance.py` specifies the registry (the shipped
catalogue validates and covers every supported venue; pinned kits verified
byte-for-byte; regenerated GitHub snapshots accepted only by commit and file
hashes), freshness (changed archive, vanished quote, newer edition, redirects
ignored, unreleased pages noticed, unverifiable when offline) and, with a real
TeX compile against a synthetic pinned kit, the gate failing first on an
old-year template, a modified style file, an overlong body, a deanonymised
build, a missing or TODO checklist and a preamble layout override, and
passing a conforming paper. The CVPR 2027 yearless-file assertion uses the
retained edition/source mapping with synthetic counterpart bytes: matching
`cvpr.sty` and bibliography pins pass, changed bytes under those same names
fail, and missing/unsupported venue years refuse resolution. It does not
claim verification of historical CVPR assets or source-level `confYear`
metadata; that requires a separately verified edition-specific field contract.
`test_venue_terms.py` adds resolution refusals
(missing year, unreleased year, unpinned, superseded) and the labelled draft.
`tests/longform/test_document_provenance.py` specifies the provenance gate
(the style-study case, rounding and percent records, layout vs content,
deliver refusing before it writes); `tests/longform/test_document_judge.py`
the single judge entry point and its image pairs. Neighbors: `tests/production/`,
`tests/slides/` and `tests/motion_video/` critic users, `tests/paper/test_paper_quality.py`.
