---
name: auto-discovery-models-from-v1-endpoint
description: Model auto-discovery, pricing enrichment with input/output preservation, and cheap tag criteria: input < Sonnet input ($3/1M) AND output < Sonnet output ($15/1M)
enabled: true
tags: [python, javascript, dispatch, auto-discovery, models, first-boot, provider, architecture]
created: 2026-03-31T07:52:46Z
updated: 2026-04-09T13:31:09Z
---

# Model Auto-Discovery & Pricing

## Cheap Tag Criteria (Updated 2026-04-09)
- **Rule**: A model is cheap iff `input_price < $3.0/1M` AND `output_price < $15.0/1M` (both strictly less than Sonnet)
- Defined in `lib/llm_dispatch/config.py`: `CHEAP_INPUT_THRESHOLD = 3.0`, `CHEAP_OUTPUT_THRESHOLD = 15.0`
- `is_model_cheap(model_id)` checks `MODEL_PRICING` for real input/output prices
- Server-side `_reeval_cheap_tags()` in `routes/config.py` re-evaluates cheap tags on every config load
- Image-generation models (dall-e, gpt-image-*, gemini-*-image*) are NOT tagged cheap even if price qualifies

## Pricing Data Flow
1. `MODEL_PRICING` in `lib/__init__.py` — hardcoded reference for ~80 known models (input/output per 1M tokens)
2. `enrich_models_with_pricing()` in `lib/llm_dispatch/discovery.py` — fetches from OpenRouter, preserves `input_price`/`output_price` on model dicts
3. `discover_models()` also backfills `input_price`/`output_price` from `MODEL_PRICING` for known models
4. `/api/server-config` merges pricing from both `MODEL_PRICING` and provider model configs into `model_pricing` dict sent to frontend
5. Frontend `_modelPricingCache` populated from this merged dict

## Models that qualify as cheap (notable examples)
- All nano/mini/flash/lite models
- gpt-4o ($2.50/$10), gpt-4.1 ($2/$8), gpt-5 ($1.25/$10), gpt-5.2 ($1.75/$14)
- o3 ($2/$8), o3-mini ($1.10/$4.40), o4-mini ($1.10/$4.40)
- gemini-2.5-pro ($1.25/$10), gemini-3.1-pro-preview ($2/$12)
- All Qwen/DeepSeek/Doubao models (Chinese API prices much lower than Sonnet)

## Models NOT cheap
- All Claude Opus models (input $5+)
- All Claude Sonnet models (input=$3, output=$15 — not strictly less)
- gpt-5.4 (output=$15, not strictly less), gpt-5.4-pro ($30/$180)
- gpt-4-turbo ($10/$30)
- glm-5/glm-5.1 (input $3.45)
- grok-3 (input=$3, output=$15 — not strictly less)

