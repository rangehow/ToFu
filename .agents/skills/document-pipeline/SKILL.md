---
name: document-pipeline
description: Write or revise a research paper (and later resumes) through Tofu's fixed staged pipeline instead of ad-hoc prompting: brief → dossier → evidence audit with experiment requests → novelty positioning → outline → draft → independent venue-rubric review → targeted revision → compile/figure QA → blind A/B vs the previous draft → delivery. Use for 论文写作/paper drafting, polishing a paper for ACL/EMNLP/ICLR, or "run the writing pipeline on my paper".
---

# Staged document pipeline

Authority: `docs/DOCUMENT_PIPELINE.md` in the Tofu repository (stages, gates,
rules and the reasons for them). Code: `lib/longform/documents/`. This guide
tells a Worker in any project how to run it on its own paper directory.

## What you need

- The Tofu repository root (`TOFU`: the shared checkout of the tofu project,
  the directory holding `scripts/document_pipeline.py`).
- The running Tofu server (model calls go through its
  `http://127.0.0.1:15000/v1/chat/completions` routing and keys).
- TeX Live with `latexmk` on `PATH` (plus the packages your paper uses), and
  poppler (`pdftotext`, `pdffonts`, `pdfimages`). Check with
  `command -v latexmk pdffonts`; the render stage reports a missing tool
  instead of guessing.
- Write access to your paper directory only. The pipeline writes
  `<paper_dir>/.pipeline/` (work, artifacts, checkpoints) and
  `<paper_dir>/<output_dir>/` (delivery). It never edits your sources.

## Steps

Follow "Runbook for a project lead" in `docs/DOCUMENT_PIPELINE.md` (the
authority for brief fields, exit codes and what to do on each refusal). In
short:

1. `scripts/document_pipeline.py venues` lists the registered venues (ACL,
   ICLR, COLM, ICML, NeurIPS, AAAI, CVPR) with their page limits and
   anonymity rules; `check-venue <venue>` fetches the newest official kit and
   compiles its sample.
2. Write `document_brief.json` in the paper directory. For a paper that
   already exists as LaTeX, add `"draft": {"main": "main.tex"}` (or pass
   `--draft main.tex`): the pipeline audits and revises that draft by
   targeted edits, keeps your figures, never invents results and A/Bs
   against it. Without a draft it writes from `dossier.json`
   (example: `tests/longform/fixtures/sample_paper/`). A finished draft
   takes `"role": "final"`: copy edits only, and render repair may fix only
   references, floats and layout. Intake refuses fields outside the brief
   schema ("Brief fields" in the authority) instead of ignoring them.
3. Name the writer explicitly (`writer_model`, or `--writer-model`); keep
   `judge_model: gpt-5.5` (the calibrated reviewer). Claude writers refuse
   frontier-LLM research content (`content_filter`); pick another family.
4. Run `cd "$TOFU" && .venv/bin/python3 scripts/document_pipeline.py run
   <paper_dir> [--draft main.tex]`. Exit 0 delivered (PDF, `source/`,
   `revision.patch` for drafts, reports, `models.json`); 2 a gate refused
   (act on it, rerun: unchanged stages resume); 1 could not proceed.
5. `status <paper_dir>/.pipeline` lists stage artifacts and the model behind
   every call; `rules paper` lists every rule and where it is enforced.

Do not edit generated artifacts to pass a gate, and do not swap the judge to
the writer model. Report the delivered path, the blind verdict with both
orders' scores ("no preference" means one win each, often position bias), the calibrated review verdict and any experiment requests.
