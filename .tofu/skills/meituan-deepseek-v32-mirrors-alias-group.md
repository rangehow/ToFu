---
name: meituan-deepseek-v3.2-mirrors-alias-group
description: Meituan gateway hosts DeepSeek V3.2 via 4 cloud mirrors as alias group with tiered CNY pricing
enabled: true
tags: [models, meituan, deepseek, pricing, alias-group]
created: 2026-04-21T03:48:27Z
updated: 2026-04-21T03:48:27Z
---

# Meituan DeepSeek V3.2 Cloud Mirrors — Alias Group

Meituan AIGC gateway exposes DeepSeek V3.2 through 4 interchangeable cloud deployments:
- `deepseek-v3.2-tencent` (canonical)
- `deepseek-v3.2-baidu`
- `deepseek-v3.2-huawei`
- `deepseek-v3.2-doubao`

## Pricing (CNY per 1M tokens, tiered by context length)

| Direction | ≤32K | >32K |
|---|---|---|
| Input  | ¥2 | ¥4 |
| Output | ¥4 | ¥6 |

USD fallback in `MODEL_PRICING` uses cheapest tier: `input=0.28, output=0.55` (at 7.24 CNY/USD).

## Integration points

1. **`static/provider_templates/meituan.json`** — one row:
   ```json
   { "model_id": "deepseek-v3.2-tencent",
     "aliases": ["deepseek-v3.2-baidu", "deepseek-v3.2-huawei", "deepseek-v3.2-doubao"],
     "capabilities": ["text", "cheap"], "rpm": 60, "cost": 0.0006 }
   ```
2. **`lib/llm_dispatch/config.py`**:
   - `MODEL_ALIAS_GROUPS` contains `{'deepseek-v3.2-tencent', '-baidu', '-huawei', '-doubao'}`
   - `DEFAULT_SLOT_CONFIGS` has entries for all 4 (caps=`{text, cheap}`, rpm=60, cost=0.001)
3. **`lib/pricing.py`**:
   - `MODEL_PRICING` — 4 USD entries
   - `QWEN_PRICING_CNY` — 4 tiered entries `input: [(32_000, 2.0), (1_000_000, 4.0)], output: [(32_000, 4.0), (1_000_000, 6.0)]`

## Note on relation to existing `deepseek-v3.2`

The plain `deepseek-v3.2` entry (no suffix) lives on DashScope/Qianfan with different pricing (¥2/¥3 flat), so it is **NOT** in the same alias group. Kept separate on purpose.

## Failover behavior

Requests to any of the 4 IDs can transparently fail over to siblings on 429/503 via the dispatcher's `MODEL_ALIASES` lookup (see `lib/llm_dispatch/dispatcher.py` L644–702).

