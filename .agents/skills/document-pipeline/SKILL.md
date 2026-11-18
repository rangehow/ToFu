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

1. Put `document_brief.json` in the paper directory:
   ```json
   {"type": "paper", "venue": "acl", "title": "...", "authors": "Anonymous",
    "topic": "search phrase for closest 2025-2026 work",
    "novelty_queries": ["optional extra search phrases"],
    "audience": "...", "goal": "...", "voice": "confident, precise, no hedging",
    "lang": "en", "page_limit": 8,
    "writer_model": "gpt-6.1-sol", "judge_model": "gpt-5.5",
    "project_lead": "who receives experiment requests",
    "previous_pdf": "path/to/current_draft.pdf", "output_dir": "delivered"}
   ```
   `venue` is `acl` (ACL/EMNLP/NAACL/ARR style files) or `iclr` (newest
   `iclrYYYY`); the official template is fetched fresh every run. The judge
   must be a different model from the writer. The review gate is calibrated
   for `judge_model: gpt-5.5` (see the authority doc); another judge runs
   uncalibrated and says so.
2. Put `dossier.json` beside it: every fact you allow the paper to state.
   `facts[]`: `id`, `kind` (`contribution`, `claim`, `result`, `background`),
   `text`, `status` (`sourced`, `confirmed` with `source.owner`, or
   `unconfirmed` = never written), `source` (`{"path": "results/x.csv"}`, a
   list of paths, `{"url": ...}` or `{"owner": "..."}`), and for claims
   `evidence` (ids of result facts and/or figure labels). Every number in a
   fact must appear in its local source files. `figures[]`: `label`
   (`fig:...`), `file` (PDF preferred), `caption`, `supports`. `bib`: your
   BibTeX file. Example: `tests/longform/fixtures/sample_paper/` in Tofu.
3. Run:
   ```sh
   cd "$TOFU" && .venv/bin/python3 scripts/document_pipeline.py run <paper_dir>
   ```
   Exit 0: delivered to `<paper_dir>/<output_dir>/` (PDF, `source/`,
   `manifest.json`, evidence/novelty/review/blind/render reports).
   Exit 2: a stage gate refused; the stage and its errors are printed, and
   every artifact so far is in `<paper_dir>/.pipeline/artifacts/`
   (`NN-<stage>.json/.md`, raw model replies under `model-replies/`).
4. Act on a refusal instead of rewording around it:
   - `evidence`: answer `experiment_requests.json` (send it to the project
     lead), add the results to the dossier, rerun.
   - `novelty`: the delta against a named 2025-2026 paper is unclear; sharpen
     the contribution or add the evidence that separates it.
   - `render`: overfull boxes, undefined references or page overrun remained
     after one automatic repair; fix the cited section.
   - `blind`: the judge preferred the previous draft; read `10-blind.md`.
   Rerun the same command: unchanged stages resume without model calls.
5. `scripts/document_pipeline.py rules paper` lists every rule (the owner's
   standing corrections) and where it is enforced.

Do not edit generated artifacts to pass a gate, and do not swap the judge to
the writer model. Report the delivered path, the blind verdict with both
orders' scores, the calibrated review verdict and any experiment requests.
