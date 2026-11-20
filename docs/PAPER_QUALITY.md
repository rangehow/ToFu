# Paper quality gate and review dossier

Authority record for the Track D paper tooling: `lib/paper/checks.py`
(source/PDF quality gate), `lib/paper/review_dossier.py` (review dossier +
scaffold builders), `lib/tools/paper_quality.py` (schemas), and
`lib/tasks_pkg/handlers/paper_quality.py` (agent handler: `paper_check`,
`paper_review`, `paper_scaffold`). The build-side authority stays in
[`PAPER_BUILDS.md`](PAPER_BUILDS.md); this document owns everything that
answers "is the paper ready to submit" rather than "does it compile".

## Problem

A paper that compiles can still be unsubmittable: figures no `\ref` ever
mentions, hard-coded `Figure~3` numbering, captions without terminal
punctuation, skipped heading levels, missing graphics files, leftover TODOs,
non-embedded fonts (a hard IEEE/ACM requirement) and low-DPI raster art.
Paperpal sells exactly this gate; Tofu users need a LaTeX-native, free,
local equivalent, plus a structured way to ask the agent "review my paper"
that is rigorous instead of a vibe check.

## Decision

- **Rules are vendored as code, table-driven** (`_SOURCE_RULES` in
  `checks.py`), scanning the comment-stripped LIVE source tree with the
  same symlink-refusing bounded readers as paper discovery. The rule subset
  is TeXtidote/chktex-inspired but implemented as small pure functions over
  one parsed source model, so the gate runs with zero external dependencies
  and identical results on every host. Undefined citations/references are
  delegated to `citation_audit.audit_citations` (only its
  `undefined_citation`/`undefined_reference` rows join the gate) so .bib
  parsing and log-key dedupe stay single-sourced.
- **PDF-level checks ride the retained build**, never a recompile: the PDF
  is copied to a bounded temp file through the scheduler cache lease and
  probed with `pdffonts`/`pdfimages` when poppler-utils is installed
  (`shutil.which` guard, ≤30 s timeout, no shell, ≤1 MiB captured output).
  Page count comes from the build's own SyncTeX, same as the build handler.
- **The review is a dossier, not an LLM call.** `build_review_dossier`
  deterministically assembles a NeurIPS-style rubric, the paper's key
  content sliced to `max_chars` (abstract/introduction/conclusion in full,
  section heads + first paragraphs, all captions, the .bib entry list), a
  claims-audit protocol (supported / unsupported / overstated /
  contradicted), and a mandated prioritized revision plan. The CALLING
  model fills it in, keeping review authority, language, and judgement with
  the model the user is already talking to; the builder stays pure,
  testable, and free of hidden model hops.
- **The scaffold is a text proposal.** `build_scaffold` renders
  `main.tex` + `sections/<slug>.tex` stubs as text for the user to confirm;
  files are created only afterwards via the normal journaled write tools.
- **Layout overflow is judged by TeX, causes by the source.** TeX writes an
  `Overfull \hbox`/`\vbox` record only past the document's own tolerance
  (`\hfuzz`/`\vfuzz`, 0.1pt in the LaTeX kernel), so every record is an
  overflow by the document's own contract and is reported with no further
  threshold: `lib/paper/builds/badboxes.py` parses the final log once;
  `paper_build` diagnostics carry each box as an `overfull_hbox`/
  `overfull_vbox` warning, and the document pipeline's render gate refuses
  any. The venue registry (`lib/longform/documents/venues.py`) records no
  overfull tolerance for any venue, so none is invented here.
  The source rule `_check_length_specs` explains likely causes in width
  arguments (tabular `p/m/b` columns, `tabular*`/`tabularx` widths,
  `minipage`/`\parbox`, `\includegraphics[width=]`): a width that is not a
  TeX dimension (`p{5}`, `p{0..05\textwidth}`, `p{2ptt}`) is a
  `length_malformed` error; a factor above 1 of a line-width register
  (`\textwidth`, `\linewidth`, `\columnwidth`, `\hsize`) is a
  `length_exceeds_line` warning, because one box wider than its line
  overflows by construction. Known false positives: a table scaled or
  rotated outside its float, or a locally redefined register; inside a float
  that uses `\resizebox`/`\scalebox`/`adjustbox`/`sideways` the row drops to
  info. Calc/`\dimexpr` arithmetic is left to TeX and the log. Origin: a
  number formatter rewrote `p{0.05\textwidth}` as `p{5\textwidth}` (a
  2,400pt overflow) and every gate passed.
- **Papers built outside the build service** (a project Makefile) run
  `scripts/document_pipeline.py tex-check <main.tex> --log <build/main.log>`
  (`run_tex_checks`): the length rows plus one row per overfull box and any
  TeX error in the log. A missing log or a log that stopped on an error is
  an error row, never a pass; exit 2 on any error or warning row. File
  attribution follows the log's open-file stack and TeX reports a
  paragraph's lines when it ends, so a range can start in an `\input` file
  already closed (reported under the parent file).
- All three tools are **read-only** (no write partition, no approval gate);
  the handler reuses `paper_build`'s document resolution and receipt lookup.

## Alternatives considered

- **Subprocess TeXtidote/chktex**: extra install for every user, Java/
  Perl runtime weight, and output formats that drift between versions.
  Vendored rules are deterministic, unit-testable, and cover the exact
  academic-QA subset we need; the trade-off is a smaller rule corpus, which
  the table-driven structure is designed to grow.
- **A second model call inside `paper_review`** (server-side reviewer
  subagent): doubles latency and cost, hides the review criteria from the
  user, and splits authorship of the critique away from the conversation.
  The dossier keeps one visible reviewer and makes the protocol explicit.
- **Mandatory poppler**: rejected; poppler is an optional probe. A missing
  tool degrades to one info row ("check unavailable"), never a failure —
  font embedding remains the user's responsibility, the row says so.

## Bounds

- Source scan: ≤256 files, ≤4096 walked entries, 8 MiB total across the
  tree, per-file cap `source_file_bytes`; ≤64 rows per run, ≤16 per rule.
- PDF probe: temp copy ≤256 MiB; each probe ≤30 s, ≤1 MiB output; ≤8
  non-embedded font rows, ≤16 low-DPI rows.
- Dossier: `max_chars` default 24000, hard-capped with a truncation marker;
  scaffold: ≤32 sections, titles ≤120 chars, slugs deduplicated.

## Failure modes

- Malformed TeX (unbalanced braces, truncated environments): rule scanners
  degrade locally; one failing rule is skipped, the rest of the gate still
  reports. The gate never changes the build verdict.
- Missing/again-inaccessible sources, busy artifact cache, absent PDF:
  one info row each (`pdf_unavailable`); the handler notes when no
  successful retained build exists and points at `paper_build`.
- `\includegraphics` targets built from macros or URLs are not statically
  resolvable and are skipped rather than mis-flagged; `\graphicspath`
  directories are honored only inside the project root.
- Sections delegating to `\input` children suppress the single-subsection
  heuristic instead of guessing across files.

## Verification

`python3 -m pytest tests/paper/test_paper_quality.py -x -q` covers every rule
(fires + stays quiet), graphics resolution, poppler parsing and absence,
report shape, dossier budget/content, scaffold slugs, and the handler
against a tmp project root. `tests/paper/test_tex_layout_gate.py` covers the
length rule (corrupted, malformed, valid and unjudgeable widths, scaled
floats), the overfull parser, build diagnostics, `run_tex_checks`, and real
pdfTeX compiles of a corrupted column and of valid wide layouts (skipped
without `pdflatex` on PATH). Regression neighbors:
`tests/paper/test_paper_citation_audit.py`, `tests/paper/test_paper_build_tool.py`.
