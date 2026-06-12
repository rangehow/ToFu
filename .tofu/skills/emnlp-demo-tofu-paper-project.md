---
name: emnlp-demo-tofu-paper-project
description: EMNLP Demo ToFu paper on Overleaf — ACTIVE project ID 6a1e782e9ba0ae3d7727a668, main acl_latex.tex; compaction ablation table added (sec:compaction-ablation)
enabled: true
tags: [overleaf, emnlp, paper, tofu]
created: 2026-04-29T14:23:58Z
updated: 2026-06-05T09:02:40Z
---

# EMNLP Demo: Tofu paper on Overleaf

- **Overleaf project**: "[EMNLP Demo] ToFu"
- **ACTIVE Project ID**: `6a1e782e9ba0ae3d7727a668` (from the URL the author gave 2026-06-05). NOTE: an older memory had `69f2114b31a22a8b1f4fcca7` — that is stale; use the 6a1e... id.
- **Main file**: `acl_latex.tex` (NOT main.tex). ~42KB, `\usepackage[preprint]{acl}`. Also `custom.bib`.
- **Compile**: ✅ success (PDF ~3.4MB) via overleaf MCP compile_project.

## Preamble caveats (what's available / NOT)
- Available: booktabs, xcolor[table], pifont (\ding), graphicx, tikz, pgfplots, fontawesome5, inconsolata, times.
- NOT loaded: `makecell` — do NOT use `\makecell`; use plain header cells or short labels.

## Compaction ablation added (2026-06-05) — sec:compaction-ablation
Replaced the commented-out "Harness ablation" TODO with a real \subsection{Compaction ablation} + Table tab:compaction-ablation, placed right after the "More computation does not mean better performance" paragraph in Evaluation. Also added \label{sec:compaction} to the "Three layers context compaction" subsection (the ablation \ref's it).

Data from maps_workdir_pro8b (deepseek-v4-pro, 50 medium/hard SWE-bench Verified instances, 8-arm run). Reported as ablation-by-diff (NO external brand names, per author instruction):
- ToFu (full) = tofu arm: 72.0% (36/50), $1.95, 1.39M tok, 24.1 turns (L2 semantic fired 27x)
- − semantic compaction = claude-code arm (L1 steps only, L2 off): 66.0% (33/50), $1.86, 1.32M, 23.4
- − compaction = no-compaction arm: 64.0% (32/50), $1.82, 1.28M, 24.1
Story: monotonic Pass@1 drop as layers removed; cost within ~8% so it's WHAT is kept, not token spend. Compaction-call tokens counted (cost-counting fix) so no hidden overhead.

## Reusable ACL template source
Copy `latex/acl.sty` + `latex/acl_natbib.bst` from reference project `692a83fb82feceb233c4b0e7` when scaffolding new ACL papers.

