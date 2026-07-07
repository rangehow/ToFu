---
name: swebench-tofu-vs-cc-failure-pattern-analysis-2026-05
description: SWE-bench verification-prompt pilot 2026-06-15: 2/8 robust failures flip (opus 13112+15695); glm 0/8. glm DOES run tests now (was 3→25-65 turns) but writes non-defensive patches (unconditional split). Don't ship as-is for glm.
enabled: true
tags: [swebench, analysis, tofu-vs-cc, model-behavior, system-prompt]
created: 2026-05-06T04:59:21Z
updated: 2026-06-15T10:59:47Z
---

# Tofu vs Claude Code — Failure Pattern Analysis

## PILOT FINAL RESULT 2026-06-15 (8 robust-failure instances, opus+glm)

Baseline: all 8 instances failed on all 3 tofu tiers before the change.
After the verification-prompt change (system-prompt regression bullet +
harness build_agent_prompt steps 7–8):

| instance | opus | glm |
|---|---|---|
| django-13112 | **PASS** 932B/22t | fail 792B/25t |
| django-15695 | **PASS** 643B/27t | fail 2648B/62t |
| django-14792 | fail 2035B/31t | fail 4211B/65t |
| django-16631 | fail 2455B/35t | fail 3340B/46t |
| pylint-4970 | fail 542B/11t | fail 1467B/12t |
| pytest-10051 | fail 568B/20t | fail 439B/15t |
| pytest-10356 | fail 1766B/37t | fail 1816B/34t |
| sphinx-9602 | fail 1383B/50t | fail 1652B/36t |

**OPUS flips 2/8. GLM flips 0/8.** Below the "≥4–5" bar I set, BUT
these are the hardest residual cases (4 are 15min–4hr difficulty and
need the actual fix, not just regression-checking). The 2 opus flips
are exactly the verification-gap cases; the other 6 are capability
gaps (wrong/incomplete fix), a different axis.

## glm "ignored the instruction"? NO — root cause is different
Pulled glm's 13112 transcript (raw_output now 2MB, toolSummary +
thinking intact). glm DID follow the prompt:
- Ran `field_deconstruction/tests.py` (35 pass) + migration autodetector
  tests. Turn count went 3 (old) → 25 (and 46–65 on other instances) —
  it now iterates and self-checks. The prompt IS changing glm behavior.
Two REAL gaps, neither is "prompt only works on opus":
1. **Non-defensive patch (the divergence):** glm wrote
   `app_label, model_name = model.split('.')` (unconditional) vs opus's
   `if '.' in model: ... else: ...`. Original line handled BOTH dotted
   and bare names; glm silently narrowed the input domain. The 2 broken
   P2P tests pass a bare name → ValueError. glm enumerated dotted cases
   in its reasoning but never considered the bare-name shape ITS OWN
   code broke. = weaker-model defensiveness gap.
2. **Misdirected regression sweep:** F2P + the 2 broken P2P all live in
   `migrations.test_state`, but glm picked test modules by file
   proximity (related.py→field_deconstruction) not by the issue's
   behavioral area (migration state). Ran the wrong module.

## Recommendation (do NOT just bolt more onto the shared prompt)
- The current prompt is a clear net-positive for opus and harmless/
  behavior-improving for glm (deeper iteration). SHIP IT for the system
  prompt + harness — it's a real gain at the top tier and doesn't
  regress glm.
- glm's two gaps are model-capability issues a prompt line won't
  reliably fix (n=1 here; a one-instance reaction is too thin to retune
  a shared prompt on). If pursuing: (a) a defensiveness nudge — "when
  replacing an expression, ensure your new code handles every input
  shape the old code did" — but VALIDATE on a larger glm sample first,
  don't ship blind; (b) test-selection — "run the tests in the module
  that exercises the BEHAVIOR you changed, found via the issue, not just
  tests near the file." Both are speculative until tested at scale.
- Next: a larger glm-only A/B (e.g. 30–50 instances) to measure whether
  the prompt nets positive/neutral/negative for glm before a full
  412×N re-run.

## Pilot mechanics recap (all confirmed this session)
- raw_output cap 50KB→2MB: WORKS (169KB glm 13112 transcript saved,
  toolSummary+thinking present; apiRounds carries only usage, the
  tool-call trace is in `toolRounds`/`toolSummary`).
- Runner endpoint fix /api/chat/*→/api/v1/chat/* was REQUIRED (404 else).
- Ran on isolated sibling chatui_swebench2 port 15001 (no main-server
  restart). No tmux/curl on host → setsid + urllib.
- Cost note: opus runs $2–20 each; glm $0.4–7. opus much faster
  (concurrency 4 vs 2); glm grinds 40–65 turns now.

(Earlier sections of this memo — 412-instance parity table, closed
over-engineering/compaction patterns, django-13112 reclassification —
still hold; see git history of this file if truncated.)

