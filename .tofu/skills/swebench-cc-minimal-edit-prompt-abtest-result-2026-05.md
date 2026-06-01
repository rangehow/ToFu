---
name: swebench-cc-minimal-edit-prompt-abtest-result-2026-05
description: A/B test: prepending CC's minimal-edit code-style block to user prompt did NOT improve resolve rate on 8-instance test (1 fix, 1 break, 6 same). Inconclusive but slightly negative.
enabled: true
tags: [swebench, abtest, prompt-engineering, minimal-edit, experiment-result]
created: 2026-05-06T18:12:36Z
updated: 2026-05-06T18:12:36Z
---

# A/B test result: CC minimal-edit code-style block

Date: 2026-05-07
Test set: 8 instances tofu had failed but cc had resolved (4 tofu-glm, 4 tofu-minimax)
Each instance run twice — baseline prompt vs same prompt + CC's getSimpleDoingTasksSection
prepended to the user message.

## Result table

| | tofu-glm | tofu-minimax | Total |
|---|---|---|---|
| baseline resolved | 3/4 | 0/4 | 3/8 |
| ccstyle resolved | 2/4 | 1/4 | 3/8 |

Per-instance flips:
- ✅ FIXED by ccstyle: mwaskom__seaborn-3069/tofu-minimax (0→2 F2P, 86→94 P2P, 1815→1173 chars)
- 🔴 BROKEN by ccstyle: django__django-13406/tofu-glm (3/3→1/3 F2P, 469→1624 chars — patch grew 3.5×)
- Same: 6 of 8

Patch-size delta:
- glm: avg patch grew 1334c → 1895c (+42%) — opposite of intent
- minimax: avg patch grew 1228c → 1444c (+18%) — also opposite

Turn-count delta:
- glm: 36.5 → 34.0 (slight drop)
- minimax: 16.2 → 57.2 — HUGE increase (but baseline was tainted by 2 harness timeouts; real comparison harder)

## Caveats / harness issues
2 of 4 minimax baseline arms hit max_poll_s=1800 and were aborted before
finishing.  Their patches were extracted from git diff but the model
likely had not converged.  The 16.2 avg_turn for baseline is misleading
because of the zero-round counts.  Only mwaskom-3069 had a clean 
baseline-vs-ccstyle comparison; ccstyle FIXED it there.

The 1 fix and 1 break give us no statistically meaningful signal on
n=8.  But two negative trends are clear:

1. **The prompt did not shrink patches** — both tools produced LARGER
   patches with the addendum.  The "three lines is better than a
   premature abstraction" rule is being interpreted backward by these
   models, possibly because the bullet draws attention to *abstraction*
   as a thing to consider.
2. **Turns went up, not down**, especially for minimax.  Seeing the
   "verify before reporting" bullet may have made the model run more
   tests, not less exploration.

## Hypothesis
Stochastic variance dominates at n=8.  The 1 fix may be noise;
the 1 break also.  We'd need n≥30 per arm to detect a 5pp effect
with statistical power, which costs ~$50 of inference per arm-pair.

## Why CC's prompt works for CC but not direct port for us
CC's prompt is part of a tightly-tuned ENVIRONMENT: tool descriptions
written to bias toward edits, no web_search, system prompt vs user
prompt placement, native-Anthropic quirks.  Lifting just the 5 bullets
into our user message is a partial port that the models don't take as
seriously.

For real impact we'd probably need to:
1. Inject as part of the SYSTEM prompt (not user message), via
   lib/tasks_pkg/system_context.py.
2. Pair with disabling web_search/MCP for SWE-bench tasks (Pattern C
   mitigation).
3. Re-run on a larger n with controlled max-rounds limit.

## Files
- debug/swebench_abtest_minimal_edit_prompt.py — A/B harness
- /tmp/swebench_capture/abtest_results.json — full result data
- /tmp/swebench_capture/abtest_set_small.json — input set

## Recommendation
Don't ship the prompt change as-is.  The signal is too weak and the
one observed regression (django-13406, 3/3 → 1/3) outweighs the win.
If we revisit, do it as system-prompt injection + tool-disable combo,
on n≥20 per arm.

