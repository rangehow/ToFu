---
name: chatui-qwen36-model-update-2026-04
description: Qwen DashScope full model update 2026-04-03: 18 models in dispatch, 19 in pricing, 21 in CNY tiers, comprehensive provider template with qwen3.6-plus, qvq, vl, coder, flash, long, third-party
enabled: true
tags: [models, qwen, pricing, update]
created: 2026-04-03T02:37:35Z
updated: 2026-04-03T02:44:49Z
---

# Qwen DashScope Full Model Update (2026-04-03)

## Models Added/Updated

### Provider Template (settings.js `_PROVIDER_TEMPLATES` → `qwen`)
The template is what users see when clicking "Apply" on Qwen (DashScope) in Settings.
Previously had only 5 models. Now has 21 models across all categories:

- **Flagship**: qwen3.6-plus (NEW ¥2/¥12), qwen3.5-plus (¥0.8/¥4.8)
- **Max**: qwen3-max (¥2.5/¥10), qwen-max (¥2.4/¥9.6)
- **Plus**: qwen-plus (¥0.8/¥2)
- **Flash**: qwen3.5-flash (NEW ¥0.2/¥2), qwen-flash (¥0.15/¥1.5)
- **Turbo**: qwen-turbo (¥0.3/¥0.6)
- **Reasoning**: qwq-plus (¥1.6/¥4)
- **Visual Reasoning**: qvq-max (¥8/¥32), qvq-plus (¥2/¥5)
- **Vision-Language**: qwen3-vl-plus (¥1/¥10), qwen3-vl-flash (¥0.15/¥1.5), qwen-vl-max, qwen-vl-plus
- **Coder**: qwen3-coder-plus (¥4/¥16), qwen3-coder-flash (¥1/¥4)
- **Long context**: qwen-long (¥0.5/¥2)
- **Third-party on DashScope**: deepseek-v3.2 (¥2/¥3), deepseek-r1 (¥4/¥16)

### Files Changed
1. `static/js/settings.js` — Provider template models list
2. `static/js/core.js` — `_qwenModelTiers` per-model tiered pricing (21 entries)
3. `lib/__init__.py` — `MODEL_PRICING` (19 Qwen entries) + `QWEN_PRICING_CNY` (21 entries)
4. `lib/llm_dispatch/config.py` — `DEFAULT_SLOT_CONFIGS` (18 Qwen entries)
5. `lib/model_info.py` — `is_qwen()` now detects QVQ, `_qwen_max_output()` handles QVQ

### Key Architecture
- `QWEN_PRICING_CNY` uses per-model tiered pricing (not flat) since models have different tier breakpoints
- `is_qwen()` matches 'qwen', 'qwq', OR 'qvq' in model name
- QVQ models get max_output=32768, QwQ/Coder get 65536

### Pricing Source
https://help.aliyun.com/zh/model-studio/model-pricing (updated 2026-04-02)

