---
name: anthropic-prompt-tokens-uncached-only-convention
description: Anthropic API returns prompt_tokens as uncached-only (NOT total) — detection heuristic and cost calculation fix in calcCostCny
enabled: true
tags: [javascript, anthropic, cache, pricing, bug-fix, frontend]
created: 2026-04-04T13:52:24Z
updated: 2026-04-04T13:52:24Z
---

# Anthropic prompt_tokens Convention — Uncached Only

## The Bug
Anthropic (and OpenAI-compatible proxies for Claude) returns `prompt_tokens` / `input_tokens` as **only the uncached portion** of input, NOT the total.

Three fields are **additive**:
```
total_input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

This differs from the OpenAI convention where `prompt_tokens` = total input (including cached).

## Impact on Cost Calculation
`calcCostCny()` in `static/js/core.js` originally computed:
```js
const si = Math.max(0, inp - cacheWrite - cacheRead);
```
This assumed `inp` was the total. When `inp` is uncached-only, `si` clamps to 0, causing:
1. Token tag shows tiny number (e.g. "66 → 12.3k" instead of "3.9M → 12.3k")
2. "Input" line in tooltip shows uncached count, misleadingly small
3. `noCacheInputUsd` is nearly zero, so cache savings never shown
4. The **total cost is still correct** (by accident: `si=0 + cw*1.25x + cr*0.1x` = correct)

## Detection Heuristic
```js
if (inp <= cacheWrite + cacheRead && (cacheWrite > 0 || cacheRead > 0)) {
  // Anthropic convention: inp is uncached only
  si = inp;
  totalInput = inp + cacheWrite + cacheRead;
} else {
  // OpenAI convention: inp is the total
  si = inp - cacheWrite - cacheRead;
  totalInput = inp;
}
```

## Evidence (from conversation mnkcw5pdwi4i3y)
- R1: `prompt_tokens=3, cache_write=16562, cache_read=0` → 3 is clearly uncached only
- All 64 rounds: `prompt_tokens=1` per round (always ~1 uncached token)
- Accumulated: `prompt_tokens=66, cache_write=1200268, cache_read=2656118`

## Confirmed by Langfuse issue #12306
Anthropic's own documentation: `input_tokens` is "uncached only — Anthropic's unusual naming".

## Files Changed
- `static/js/core.js` — `calcCostCny()`: detect convention, compute `si` and `totalInput` correctly
- `static/js/ui.js` — token tag display uses `totalInput`, tooltip shows uncached correctly
- `static/js/bundle-479eb2c9.js` — same fixes mirrored

