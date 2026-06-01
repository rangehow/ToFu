---
name: reactive-compact-bypass-threshold-gate
description: reactive_compact must call force_compact_if_needed(force=True) — heuristic vs API tokeniser disagrees
enabled: true
tags: [compaction, tokens, bug-pattern]
created: 2026-05-13T05:31:30Z
updated: 2026-05-13T05:31:30Z
---

# reactive_compact must skip the local-heuristic gate

## The trap
`force_compact_if_needed` calls `_should_force_compact`, which compares
`_count_tokens_authoritative(messages)` against
`int(usable_context * _SUMMARY_TRIGGER_RATIO)` (default 0.90).

For unknown / non-Claude / non-Gemini providers, the authoritative
counter falls all the way down to the CJK heuristic — which is typically
**10–30 % below** the gateway's real tokeniser, especially for tool-heavy
mixed-language prompts. On smaller windows (e.g. glm5.1 @ 192 K) the
0.90 trigger leaves only ~10 % headroom — narrower than the heuristic
under-count. Result: the API has already rejected the prompt as too
long, but the local heuristic still says "no need to compact" → reactive
compaction silently no-ops, the head-truncate fallback fires instead,
and we lose the better summarised output.

Observed 2026-05-13 task d551dd42 conv mp2qryrrnmbhtn: API counted
112 820 input tokens, heuristic said 100 443, threshold was 137 352 →
`_should_force_compact` → False, even though the API had just rejected.

## The fix
`reactive_compact` calls `force_compact_if_needed(..., force=True)`. The
API has already definitively rejected the prompt; the threshold question
is moot.

`force` is a keyword-only parameter on `force_compact_if_needed`. Default
is `False`, so the proactive call sites in
`lib/tasks_pkg/orchestrator.py` and `smart_summary_compact()` keep the
threshold gate.

## Related
- `_SUMMARY_TRIGGER_RATIO = 0.90` is calibrated for 1M-context Claude
  (still 100 K headroom). For sub-300 K windows it's near the heuristic
  noise floor — consider making it context-window-dependent if proactive
  compaction also turns out to fire too late on smaller models.
- `_count_tokens_authoritative` invalidates the usage_cache early in
  reactive_compact, so the last-known-exact prompt_tokens from the
  provider isn't available either — another reason the heuristic is
  alone at the gate.

