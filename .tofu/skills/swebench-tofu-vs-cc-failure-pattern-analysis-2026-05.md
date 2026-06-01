---
name: swebench-tofu-vs-cc-failure-pattern-analysis-2026-05
description: 3-case deep analysis of tofu losses vs cc wins on same instance: stochastic variance + over-engineering bias + tool over-availability are the gaps, NOT harness bugs
enabled: true
tags: [swebench, analysis, tofu-vs-cc, model-behavior, system-prompt]
created: 2026-05-06T04:59:21Z
updated: 2026-05-06T04:59:21Z
---

# Tofu vs Claude Code — Failure Pattern Analysis (2026-05-06)

3 deep-dive case studies of "tofu-X failed, cc-X resolved on the same
instance". Used `debug/swebench_capture_conversation.py` to re-run
problematic instances and capture full tool-call traces.

## Three failure patterns observed

### Pattern A — Stochastic edge-case blindness
Example: `django-13112/tofu-minimax`. First sample produced a patch that
removed an else branch; capture re-run with the SAME prompt produced
the correct branch-preserving fix on the second try. Model is *capable*,
but at temp=0 the sankuai gateway still has variance.
- **Mitigation**: Sample-of-2 with self-scoring or shortest-passing-patch
  selection. Estimate: recovers ~5-8pp resolve rate.

### Pattern B — Over-engineering / scope creep
Example: `django-13315/tofu-glm`. CC's fix: `.distinct()` (4 chars).
Tofu's fix: 4 separate edits across 3 files with manual `seen` sets and
a new `MultipleObjectsReturned` catch. Tofu also pulled the real PR via
GitHub MCP but converted the simple solution into a cross-subquery
`Exists()/OuterRef()` rewrite. Pattern repeats in `django-13406`:
CC's 2-line setter check vs Tofu's new attribute threaded through 3
methods.
- **Mitigation**: SWE-bench-mode system prompt addition: "prefer the
  minimal edit that fixes the failing test; do not refactor; preserve
  existing branches when adding conditions; if a one-line fix works,
  use it." Estimate: 3-5pp gain.

### Pattern C — Tool over-availability
SWE-bench tofu has `web_search`, `fetch_url`, `mcp__github__*` enabled
by default. The capture trace for `django-13315` shows the model went
to GitHub, fetched the actual merged PR's files, and STILL produced an
over-engineered patch (different from the PR's clean fix). The tools
help on average (notool variants are 7-15pp worse) but they tempt
exploration.
- **Mitigation**: For SWE-bench specifically, set
  `searchMode: 'off'`, `fetchEnabled: False`, `mcpEnabled: False` in
  the per-task config (these knobs already exist in MODEL_PRESETS via
  `config_overrides`, just empty). Mirrors CC's tool environment more
  faithfully. Estimate: harder to call but neutral-to-positive.

## What's NOT the gap (ruled out)

- **Compaction**: not observed in any of the 3 traces.
- **Cache misses**: cache hit rate 64-100% in this run.
- **Tool retries / 429s**: dispatch handles these cleanly. None of the
  failure cases hit retry exhaustion.
- **Endpoint/critic mode**: not enabled, but adding it is a different
  axis than these patterns. (Worth A/B-testing separately.)

## Concrete files
- `debug/swebench_capture_conversation.py` — re-run + capture
- `/tmp/swebench_capture/TOFU_VS_CC_ANALYSIS.md` — full case write-up

## Tactical next steps (in priority order, all need user approval per §10)
1. SWE-bench system-prompt addition for minimal-edit bias.
2. SWE-bench config_overrides: turn off web_search/fetch/mcp.
3. Enable endpoint mode (planner→worker→critic) for SWE-bench arm.
4. Sample-of-2 with patch selection.

## Aggregate signals support these conclusions

- Tofu vs CC same-tier resolve gap is now ~5pp (was 15pp before harness fixes).
- 60 of these "same-tier loss" cases exist; spot-checking 4 of them all
  show one of patterns A/B/C above. None showed harness pathology.
- 33 of the 70 raw failures were "everyone fails" (dataset noise/flakes),
  not Tofu-specific.

So the remaining gap is genuinely a model-orchestration cliff, not a
runner bug. Closing it requires prompt/config changes, not infrastructure.

