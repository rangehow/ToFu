---
name: meituan-deepseek-v4-and-gpt-image-2-added
description: Meituan gateway additions 2026-04-27: deepseek-v4-flash(+huawei alias), deepseek-v4-pro, gpt-image-2
enabled: true
tags: [models, meituan, deepseek, gpt-image, pricing]
created: 2026-04-27T23:17:06Z
updated: 2026-04-27T23:17:06Z
---

# Meituan Gateway — New Models (2026-04-27)

Added via user request to the Meituan AIGC gateway provider template.

## Models added

| Model | Input (¥/M) | Output (¥/M) | Notes |
|---|---|---|---|
| `deepseek-v4-flash` | 1 | 2 | Already existed (DeepSeek direct); added to Meituan template |
| `deepseek-v4-flash-huawei` | 1 | 2 | NEW alias of v4-flash on Meituan Huawei-cloud mirror |
| `deepseek-v4-pro` | 12 | 24 | Already existed; added to Meituan template |
| `gpt-image-2` | text ¥36/M, image ¥57.6/M | image ¥216/M | NEW; OpenAI-style sync images API |

## Files touched
- `lib/pricing.py` — MODEL_PRICING (deepseek-v4-flash-huawei, gpt-image-2), QWEN_PRICING_CNY flat tiers for all 3 deepseek-v4 IDs.
- `lib/llm_dispatch/config.py` — DEFAULT_SLOT_CONFIGS (added v4-flash-huawei + gpt-image-2) + MODEL_ALIAS_GROUPS `{deepseek-v4-flash, deepseek-v4-flash-huawei}`.
- `static/provider_templates/meituan.json` — added 3 rows (v4-pro, v4-flash w/ alias, gpt-image-2).
- `lib/image_gen.py` — _OPENAI_IMAGE_MODELS includes `gpt-image-2`; docstring Models updated.
- `static/js/image-gen.js` — _IG_ALL_MODELS + _IG_MODEL_SHORT include `gpt-image-2` (short name "GPT Image 2").

## Pricing conversion (CNY → USD at 7.24)
- v4-flash: ¥1/¥2 → $0.138/$0.276 (stored as $0.14/$0.28, already matched).
- v4-pro: ¥12/¥24 → $1.66/$3.31 (kept existing $1.74/$3.48 from DeepSeek-direct).
- gpt-image-2: text-in ¥36 → $4.97/M input; image-out ¥216 → $29.83/M output.

## Did NOT change
- `static/js/settings.js` DeepSeek provider template (that's for the direct DeepSeek API, not Meituan). Meituan template is loaded at runtime from the JSON file.
- `bundle-*.js` minified bundles auto-regen; not hand-edited.

