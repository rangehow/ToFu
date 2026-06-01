---
name: emnlp-demo-tofu-paper-project
description: EMNLP Demo Tofu paper on Overleaf — project ID, structure, compile OK
enabled: true
tags: [overleaf, emnlp, paper, tofu]
created: 2026-04-29T14:23:58Z
updated: 2026-04-29T14:23:58Z
---

# EMNLP Demo: Tofu paper on Overleaf

- **Overleaf project**: "[EMNLP Demo] Tofu"
- **Project ID**: `69f2114b31a22a8b1f4fcca7`
- **Files**:
  - `main.tex` — full draft (intro, related work, overview, 4 novel elements, impl, eval, license, limitations, conclusion)
  - `custom.bib` — bibliography (25+ refs incl. TinyScientist, ResearStudio, Marcel)
  - `acl.sty`, `acl_natbib.bst` — ACL style, copied from reference project `692a83fb82feceb233c4b0e7`
- **Compile status**: ✅ Success (PDF ~140KB)

## Key conventions
- Used `\usepackage{pifont}` with `\cmark` / `\xmark` for feature table — **must be in preamble, not after** `\maketitle` (original draft had a compile bug)
- Reference papers fetched from aclanthology.org/2025.emnlp-demos.{41,69,13}.pdf
- Template uses `\usepackage[review]{acl}` for anonymous review

## Reusable ACL template source
When creating new ACL papers via Overleaf MCP, copy `latex/acl.sty` and `latex/acl_natbib.bst` from the reference project `692a83fb82feceb233c4b0e7` ("Association for Computational Linguistics (ACL) conference (1)").
