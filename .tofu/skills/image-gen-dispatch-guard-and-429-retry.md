---
name: image-gen-multi-model-dispatch-and-429-retry
description: Image gen: 4 models (3 Gemini async + 1 OpenAI sync gpt-image-1.5) × 2 keys = 8 slots; _is_chat_compatible guard fix with _NON_CHAT_CAPS; aggressive 0.3s 429 retry cycling through slots; model names are case-sensitive (gpt-image-1.5 not GPT-image-1.5)"
enabled: true
tags: [python, image-gen, dispatch, 429, rate-limit, guard, gemini, openai, multi-model, case-sensitive]
created: 2026-03-29T15:53:34Z
updated: 2026-03-30T02:56:03Z
---

# Image Generation: Multi-Model Dispatch & 429 Retry

## Architecture
- **4 image_gen models** dispatched through the standard slot system:
  - `gemini-3.1-flash-image-preview` — Gemini async API ($0.015)
  - `gemini-3-pro-image-preview` — Gemini async API ($0.134)
  - `gemini-2.5-flash-image` — Gemini async API ($0.012)
  - `gpt-image-1.5` — OpenAI sync images/generations API ($0.043)
- **2 API keys** → **8 total slots** for image generation
- Dispatch picks the cheapest available slot by default

## Two API Families

### Gemini Async (submit + poll)
- `POST /v1/google/models/{model}:imageGenerate` → returns task ID string
- `GET /v1/google/models/{taskId}:imageGenerateQuery` → poll until status=1
- `inlineData.data` may be an S3 URL (not raw base64) — must download + b64encode
- `text` parts with `thought: true` are model thinking — filter out

### OpenAI Sync (one-shot)
- `POST /v1/openai/native/images/generations` → returns `data[0].b64_json` or `data[0].url`
- Supported models: gpt-image-1.5, gpt-image-1, gpt-image-1-mini, dall-e-3
- Size param: "1024x1024", "1536x1024", "1024x1536"
- Quality param: "auto", "high", "medium", "low" (NOT "hd"/"standard" like dall-e-3)

## Bug Fix 1: `_is_chat_compatible` Guard
The `_pick()` method had a guard that skipped slots whose capabilities are a subset of
`{embedding, image_gen}`. This was applied **unconditionally**, blocking all image_gen
dispatch even when `capability='image_gen'` was explicitly requested.

**Fix**: `_NON_CHAT_CAPS = frozenset({'embedding', 'image_gen'})` — skip guard when
`capability in _NON_CHAT_CAPS`.

## Bug Fix 2: Aggressive 429 Retry
- 429 retries are **unlimited** (up to 120 safety cap), do NOT count toward `max_retries`
- Sleep only **0.3s** between 429 retries — dispatch cooldown (0.5s) steers to different slot
- With 8 slots, cycling through all takes ~2.4s, then first slot's cooldown has expired
- Only non-429 errors count toward the 3-retry hard budget

## Bug Fix 3: Case-Sensitive Model Names
- FRIDAY API requires exact model names: `gpt-image-1.5` NOT `GPT-image-1.5`
- The error message from API tells you the exact supported model names

## Files
- `lib/image_gen.py` — Main image generation with dual-API support
- `lib/llm_dispatch/config.py` — DEFAULT_SLOT_CONFIGS with all 4 models
- `lib/llm_dispatch/dispatcher.py` — _NON_CHAT_CAPS guard fix in _pick()
- `~/.chatui/server_config.json` — Runtime slot config with model entries

