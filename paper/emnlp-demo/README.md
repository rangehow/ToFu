# Tofu — EMNLP Demo Paper (draft)

Self-contained ACL/EMNLP submission for the Tofu system.

## Files

- `tofu.tex` — main paper (4 content pages + unlimited refs)
- `tofu.bib` — bibliography
- `acl.sty`, `acl_natbib.bst` — official ACL style files (copied from the
  `Association for Computational Linguistics (ACL) conference` Overleaf
  template in `overleaf_cache/692a83fb82feceb233c4b0e7/latex/`)

## Build

```bash
pdflatex tofu
bibtex tofu
pdflatex tofu
pdflatex tofu
```

Uses `[review]` mode (double-blind, with line numbers). Switch to
`\usepackage[final]{acl}` for camera-ready.

## Content map (answers to demo-paper rubric)

| Question | Where |
|---|---|
| What problem does the system solve? | §1 Introduction (paragraphs 1–2) |
| Why is the system important / impact? | §1 (contributions), §6 target audience |
| What is novel? | §1 (3 numbered contributions), §3.3 compaction, §3.4 critic, §3.5 token-saving |
| Target audience? | §6 "Target audience" paragraph |
| How does the system work? | §3 (Overview, Tools, Compaction, Endpoint, Impl. details) |
| Comparison with existing systems? | §2 Related Work + §5 Table 2 (9-axis matrix) |
| License? | §6 + abstract: **MIT** |
| Evaluation / user studies? | §4 (SWE-bench Verified 64.2%, caching A/B, deployment); §6 on HCI plans |

## Notes for the author

- Placeholder `\includegraphics{figures/arch.pdf}` is commented out — add
  an architecture figure before submission (reuse
  `static/icons/tofu-welcome.svg` + a block diagram).
- Numeric claims (64.2%, 38%, 525+ tests) already match memories in
  `.chatui/skills/` — double-check exact numbers at submission time.
- Keep within **4 content pages** for short-paper review version; ACL
  style auto-enforces column/page width.
