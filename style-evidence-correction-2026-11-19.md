# Internal style-evidence correction — 2026-11-19

**Evidence note only; not a runtime rule, experiment, or publication.** Basis:
reviewed checkpoint `bf09c6e7d382eef4b8b11f40fdec7752d488515c` and the
Coordinator's retained reviewer `a1ff3a36` findings. This note corrects the
interpretation of selected exemplars, not historical data. The preserved failed
Attempt checkpoint `6bb0ff2bce8a844cfea1bafe59c3a20b1e4d5b8d` is unreviewed:
its path inventory was inspected, but it was not merged or used as authority.
The current checkout is not identical to the reviewed basis; historical line
numbers below refer **only to bf09c6e7**, not to current files.

## Corrected, attributed paraphrases

These are paraphrases, not quotations or author-style templates. No fragment
is presented as a complete quotation; source locations and limits stay attached
to each retained item.

- **D1 — Soldaini et al., Dolma (ACL 2024), Abstract, PDF p. 1 [S1].**
  Limited disclosure of pretraining data, including releases of open models
  without their training data or reproducible recipes, obstructs research into
  how data affects model capabilities and limitations. The authors present an
  openly documented corpus and curation toolkit in response. This retains the
  problem-to-resource relationship without reproducing the reviewer-flagged
  truncated excerpt or implying that its omitted context was absent.
- **N1 — Bar, Zhou, Tran, Darrell and LeCun, Navigation World Models (CVPR
  2025; retained arXiv v2), Abstract, PDF p. 1 [S2].** The authors describe a
  controllable video-generation model using a Conditional Diffusion Transformer
  trained on human and robotic egocentric video, scaled to **1 billion
  parameters**. That number is **method/model scale**, not a measured navigation
  outcome. It does not exemplify a quantitative main-result requirement.
- **T1 — Tian, Huang, Liu, Jiang, Spangher, Chen, May and Peng, Are Large
  Language Models Capable of Generating Human-Level Narratives? (EMNLP 2024),
  Abstract and §1, PDF pp. 1–2 [S3].** In §1 the authors report two separate
  comparisons with vanilla prompting: **45%** for story-arc awareness and
  narrative diversity, and **40%** for turning-point information and narrative
  suspense/engagement. Their Abstract uses a broader **over-40%** headline for
  diversity, suspense and arousal. These are the authors' differently scoped
  summaries, not an identical metric or repeated headline number; this note
  neither reconciles them into a new statistic nor independently validates the
  reported improvements.

## Older rule claims that this evidence cannot support

- `paper.style.abstract-number`: N1 cannot support requiring a measured main
  result in every quantitative abstract. A digit-presence feature does not
  distinguish model scale, resource size, identifiers and measured outcomes.
- `paper.style.intro-quantified`: T1 cannot support requiring **the same headline
  number as the abstract**. Different metrics, interventions and scope must not
  be collapsed into numerical equivalence. Quantification alone is weaker than
  this rule's wording.
- `paper.style.caption-takeaway`, `paper.style.limitations-section` and
  `paper.style.intro-findings-first-person`: the retained corpus associations
  do not establish universal requirements or a causal writing benefit. The
  abstract/introduction intervention cannot establish caption or full-section
  efficacy. D1/N1/T1 supply no missing efficacy result for these rules.
- Mandatory verb style, categorical contribution-list removal, teaser figures,
  late related work and per-type outlines are not rescued by these examples.
  The retained historical ablation record removed abstract-verbs,
  contributions-prose, teaser-figure, related-work-late and outlines; this is
  not a fresh test or proof that each is universally harmful [S4].
- **Attribution is not preserved by shipping bare exemplar strings.** In the
  inspected checkout, `style_rules.json` stores unattributed quote strings and
  `rules.py:193–194` appends those strings to rule text, not their paper-specific
  bibliographic map. A generic corpus origin is not per-exemplar attribution
  [S5]. This note keeps each paraphrase attached to its own source; it does not
  fix the shipped representation.

## Frozen outcome and unresolved efficacy

**The preregistered outcome remains FAIL.** The registered criterion requires
zero unsourced numbers in arm B; the retained result records one flagged value,
`5`, for unit `C3449f48d`, an additional section reference rather than evidence
of an invented scientific result. `zero_unsourced_numbers_in_B` and `pass` are
both false [S6]. Neither this distinction nor favorable cached preferences
changes the registered gate, units, outputs, counts or verdict.

There is **no genuine development-untouched held-out style-efficacy result**
here. The accepted-paper units had already entered aggregate same-sign/AUC
analyses affecting development; exclusion from unit-level A/B selection does
not undo that exposure. Requested/local route labels do not establish upstream
model identities or assessor independence. The cached mandatory-writer study
also does not estimate the current reviewer-only intervention [S4, S6].
**Actual held-out benefit and the claim that Tofu improves with use remain
open**, not proven by source-grounded examples or descriptive corpus patterns.

Any later runtime mitigation—retaining source identity beside each exemplar,
using attributed paraphrases, or admitting only semantically checked
measurement examples—is **prospective only**, unimplemented and unevaluated by
this note. No new registration, model call, evaluation, program-writing
workflow, external retrieval, private-paper access, deployment or publication
was performed. Shipped rules/code, source dataset, registration and historical
results were left untouched.

## Exact source map

All external URLs below were **already linked in the reviewed basis**. They
identify the retained source; none was fetched in this task. PDF identity was
checked locally against the bf09c6e7 `evaluations/paper_style_corpus/manifest.csv` rows. Local paths beginning
`data/` resolve under the registered shared data root; no PDFs or extracted
records are copied into this deliverable.

| Key | Attribution and exact retained location | Identity / historical pointer |
|---|---|---|
| S1 / D1 | Luca Soldaini et al., *Dolma: an Open Corpus of Three Trillion Tokens for Language Model Pretraining Research*, ACL 2024. [Retained source](https://aclanthology.org/2024.acl-long.840.pdf). `data/paper-style-corpus/pdf/A16b0ab40.pdf`, p. 1 Abstract; `data/paper-style-corpus/records/A16b0ab40.json`, `abstract`. | PDF SHA-256 `d79faf818a2fc693b5d6cef0c1c97626e737acfca108fcaf9700cefcbdede60b`; bf09c6e7 `evaluations/paper_style_corpus/manifest.csv` row `A16b0ab40`; `docs/PAPER_STYLE_EVIDENCE.md:817–819` contains the complete 36-author attribution. |
| S2 / N1 | Amir Bar; Gaoyue Zhou; Danny Tran; Trevor Darrell; Yann LeCun, *Navigation World Models*, CVPR 2025. [Retained source](https://arxiv.org/pdf/2412.03572). `data/paper-style-corpus/pdf/A02391afc.pdf`, p. 1 Abstract (v2, 11 April 2025); `data/paper-style-corpus/records/A02391afc.json`, `abstract`. | PDF SHA-256 `33b6532143148b60d991f7cfc0c6ff2c168a54c8dcd285e4dbee5eec84c51a5e`; bf09c6e7 `evaluations/paper_style_corpus/manifest.csv` row `A02391afc`; `docs/PAPER_STYLE_EVIDENCE.md:777–779`; criticized example at line 705. |
| S3 / T1 | Yufei Tian; Tenghao Huang; Miri Liu; Derek Jiang; Alexander Spangher; Muhao Chen; Jonathan May; Nanyun Peng, *Are Large Language Models Capable of Generating Human-Level Narratives?*, EMNLP 2024. [Retained source](https://aclanthology.org/2024.emnlp-main.978.pdf). `data/paper-style-corpus/pdf/A092dfb4d.pdf`, pp. 1–2, Abstract and §1; `data/paper-style-corpus/records/A092dfb4d.json`, `abstract` and `sections[role=introduction].text`. | PDF SHA-256 `20bd61c88e73b20f41f6c9ba6bb11f535f18c276ecd88dd49aceffe572f33130`; bf09c6e7 `evaluations/paper_style_corpus/manifest.csv` row `A092dfb4d`; `docs/PAPER_STYLE_EVIDENCE.md:801–803`; criticized example at line 719. |
| S4 | Historical analysis and limits, not a new measurement: bf09c6e7 `docs/PAPER_STYLE_EVIDENCE.md`, “Retained-only style-effect audit”, “Limits”, “Candidate rules and exemplars”, and “Validation”. | Inspect with `git show bf09c6e7d382eef4b8b11f40fdec7752d488515c:docs/PAPER_STYLE_EVIDENCE.md`; do not substitute the differing current file. |
| S5 | Static snapshot `d142263462` (shared baseline), inspected `lib/longform/documents/style_rules.json:9–13,21–25` and `lib/longform/documents/rules.py:182–203`. | Static inspection only; bare strings are appended at lines 193–194. No runtime execution or edits. |
| S6 | [Frozen registration](evaluations/paper_style_corpus/PREREG_HELDOUT_AB.md), “Tests and success criterion”; [retained result](evaluations/paper_style_corpus/validation_heldout_ab.json), `provenance.B.per_unit` and `outcome`; historical interpretation in bf09c6e7 `docs/PAPER_STYLE_EVIDENCE.md`, “Held-out A/B (pre-registered)” and “Retained-only style-effect audit”. | Registering commit `579282395f`; bf09c6e7 is the reviewed interpretation basis. Result field names and failure were inspected, not recomputed or re-scored. |

## Acceptance record

1. **Met for this note:** all retained paper items are attributed paraphrases
   with exact source/location/identity; no truncated quotation or numeric-type
   or abstract/introduction equivalence claim. The note alone is added;
   historical data and shipped behavior are not corrected in place.
2. **Met:** FAIL remains frozen; genuine held-out/current-intervention efficacy
   and use-improves claims stay open. Mitigations are prospective only; no
   external action or experiment was performed. This is source/interpretation
   verification, not an efficacy validation.
