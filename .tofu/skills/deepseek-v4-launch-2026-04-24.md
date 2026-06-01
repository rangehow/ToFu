---
name: deepseek-v4-launch-2026-04-24
description: DeepSeek V4 (Apr 2026) - deepseek-v4-pro / -flash, 1M ctx, dual modes, legacy retires 2026-07-24
enabled: true
tags: [models, deepseek, pricing, update]
created: 2026-04-24T08:08:57Z
updated: 2026-04-24T08:08:57Z
---

# DeepSeek V4 Launch — 2026-04-24

## Release summary
DeepSeek released two new models on 2026-04-24, same `base_url`
`https://api.deepseek.com` (OpenAI + Anthropic compatible).

| Model | Params | Context | Input | Cache hit | Output | Modes |
|---|---|---|---|---|---|---|
| `deepseek-v4-pro` | 1.6T / 49B act | 1M | $1.74 | $0.145 | $3.48 | Thinking + Non-Thinking (effort: high/max) |
| `deepseek-v4-flash` | 284B / 13B act | 1M | $0.14 | $0.028 | $0.28 | Thinking + Non-Thinking |

Both Apache 2.0; weights on Hugging Face.

## Deprecation
- `deepseek-chat` and `deepseek-reasoner` retire **2026-07-24 15:59 UTC**.
- Until then, they silently route to `deepseek-v4-flash` (both modes).

## Thinking format
V4 uses `thinking.type = "enabled"` (Claude-style dual-mode API) — different
from V3 reasoner, which was a separate model. See
`lib/llm_dispatch/discovery.py::_THINKING_FORMAT_HINTS`.

## Files updated in this project
- `lib/pricing.py` — MODEL_PRICING entries (cacheReadMul derived from disclosed
  cache-hit prices: Pro 0.083, Flash 0.20).
- `lib/llm_dispatch/config.py` — DEFAULT_SLOT_CONFIGS entries.
  - Pro: rpm=30, latency=3000, cost=0.003, caps {text, thinking, cheap}
  - Flash: rpm=60, latency=2000, cost=0.0002, caps {text, thinking, cheap}
- `static/js/settings.js` — DeepSeek provider template (kept legacy entries
  until July since they still work as aliases).
- `bootstrap.py` — setup wizard default: `deepseek-chat` → `deepseek-v4-flash`.
- `lib/swarm/registry.py::_MODEL_FAMILIES['deepseek']` — light/standard=flash,
  heavy=pro.
- `lib/llm_dispatch/discovery.py::_THINKING_FORMAT_HINTS` — added
  `deepseek-v4` → `thinking_type` (before the `deepseek-reasoner → none` row).

## Cheap-tag classification
Both V4 models are "cheap" by the project threshold (input < $3, output < $15).
Pro is genuinely cheaper than Sonnet despite being a flagship-class model — a
major shift vs previous frontier pricing.

