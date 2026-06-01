---
name: sankuai-cache-dual-convention-extraction-bug
description: Sankuai gateway reports cache via Anthropic fields for Claude and OpenAI fields for GLM/MiniMax — SWE-bench runner only read Anthropic, silently 0-cached all non-Claude models
enabled: true
tags: [swebench, caching, sankuai, gateway, bug-fix, extraction]
created: 2026-05-04T13:09:06Z
updated: 2026-05-04T13:09:06Z
---

# Sankuai Gateway — Cache Usage Dual-Convention Extraction Bug

## Discovered 2026-05-04

The sankuai OpenAI-compatible gateway (`aigc.sankuai.com/v1/openai/native`)
reports per-request cache hits via **two different conventions** depending
on the model family:

| Model family | Cache-read field | Note |
|---|---|---|
| `aws.claude-*` | `usage.cache_read_tokens` (top-level) | Anthropic convention |
| `glm-5.*` | `usage.prompt_tokens_details.cached_tokens` (nested) | OpenAI convention |
| `MiniMax-M2.*` | `usage.prompt_tokens_details.cached_tokens` (nested) | OpenAI convention |
| `Doubao-*` | `usage.prompt_tokens_details.cached_tokens` (nested, auto-cached) | OpenAI convention |
| `qwen-*`, `deepseek-*` | likely OpenAI convention | needs probe |

**Critical semantic difference:** the OpenAI convention's `cached_tokens`
is a SUB-COUNT of `prompt_tokens` (NOT additive). So correct pricing requires:

```python
uncached_prompt = max(prompt_tokens - cached_tokens, 0)  # priced at full input rate
cache_read      = cached_tokens                          # priced at cache_read rate
```

If you naively read `cache_read_tokens=0` for GLM/MiniMax and bill all of
`prompt_tokens` at full rate, you overstate cost by roughly 2-3× for any
workload with turn-over-turn prefix continuity.

## Where the bug hit us

1. **SWE-bench runner (`debug/swebench_runner.py`)** —
   `run_tofu_inference` and its safety-timeout sibling summed only
   `ru.get('cache_read_tokens', 0)`. Result: every GLM/MiniMax instance
   in the old 412-run report had `cache_read=0`. This fooled the
   "0% hit on MiniMax" post-mortem into blaming our client or the
   marker logic when the gateway was caching all along.

2. **In the live rerun (2026-05-04)**: 215 instances were already done
   under the buggy extractor. Dry-run of `debug/swebench_recompute_cache_costs.py`
   showed:
     * tofu-glm: 23.7M tokens were cache hits → Δcost −$38.77, cache_hit_rate 0% → 70%
     * tofu-minimax: 36.9M tokens were cache hits → Δcost −$36.61, 0% → 80%
     * tofu-opus: 0 changes (Anthropic convention always worked)

## Fix applied
- `lib/...` — no change (this is an external-client reporting issue)
- `debug/swebench_runner.py` — usage loop now reads BOTH fields, takes
  `max()`, and subtracts OpenAI-cached from prompt to avoid double billing.
- `debug/swebench_recompute_cache_costs.py` — new post-processor that
  rebuilds inference metrics from `details/*.json` `raw_output` apiRounds
  using the same dual-convention logic. Uses a regex fallback when the
  runner's 50-KB truncation mangles the JSON.

## Checklist for anywhere else that parses sankuai usage dicts
Any new code that consumes `apiRounds` usage must do:
```python
_cr_a = u.get('cache_read_tokens', 0)                       # Anthropic
_det  = u.get('prompt_tokens_details') or {}
_cr_o = _det.get('cached_tokens', 0) if isinstance(_det, dict) else 0
cache_read_final = max(_cr_a, _cr_o)
# If it's OpenAI-style, cached is PART of prompt_tokens, not additive:
if _cr_o > 0 and _cr_a == 0:
    uncached_prompt = max(prompt_tokens - _cr_o, 0)
```

Search for `cache_read_tokens` across the repo whenever touching this
area; audit every hit to confirm it also falls back to `prompt_tokens_details.cached_tokens`.

