---
name: chatui-new-models-benchmark-2026-03
description: MiniMax model family: M2/M2.1/M2.5/M2.7/M2-her, api.minimax.chat endpoint, reasoning_split=True, vision stripping for non-vision models in build_body, pricing in CNY tiers
enabled: true
tags: [benchmark, models, minimax, reasoning, thinking, api]
created: 2026-03-21T14:50:20Z
updated: 2026-04-03T06:50:00Z
---

# New Model Benchmark Results (2026-03-21)

## ⚠️ MiniMax Endpoint Selection (CRITICAL)

MiniMax has **three** API domains. The correct one depends on your API key type:

| Endpoint | Key Type | Notes |
|---|---|---|
| `https://api.minimax.chat/v1` | **Coding Plan keys (`sk-cp-...`)** | ✅ Works, fastest (~5s) |
| `https://api.minimaxi.com/v1` | China-registered keys | ✅ Works, slower (~8s) |
| `https://api.minimax.io/v1` | **International keys only** | ❌ Returns 401 "invalid api key (2049)" for Coding Plan keys |

**Our key is a Coding Plan key (`sk-cp-n-...`), so we MUST use `api.minimax.chat/v1`.**

The official docs at `platform.minimax.io` reference `api.minimax.io` as the OpenAI-compatible endpoint,
but that only works with international/pay-as-you-go keys. Coding Plan keys must use `api.minimax.chat`.

Additionally: all three domains fail DNS resolution from internal DNS (10.10.10.10),
so MiniMax domains must NOT be in `proxy_bypass_domains` — they need the corporate proxy.

## Chat Models

### gemini-3.1-pro-preview
- **Both keys**: ✅ working
- **RPM**: ~5 per key (burst), ~34-43 per key (steady) — very slow responses (~6-7s avg)
- **Vision**: ✅ Correctly identifies image content
- **Thinking**: Uses internal reasoning (visible as `reasoning_tokens` in usage, NOT exposed via `reasoning_content` in streaming deltas). `enable_thinking=True` activates it. When `enable_thinking=False`, reasoning_tokens=0 and responses are faster.
- **CRITICAL BUG**: With `max_tokens=50`, the model spends all tokens on reasoning → `message: null` in response. Production code in `llm_client.py:chat()` already handles this with `choices[0].get('message') or {}`.
- **Pricing**: $1.25/M input, $10.0/M output
- **`is_gemini()` match**: ✅ Yes (substring 'gemini')
- **`build_body`**: Sends `enable_thinking=True` + Gemini max_tokens clamping (65536) automatically

### MiniMax-M2.7
- **Both keys**: ✅ working
- **RPM**: ~90-103 per key
- **Vision**: ❌ Accepts image_url payloads without error but CANNOT actually see image content (responds generically)
- **Thinking**: Always-on `<think>` tags embedded in content. NOT controllable via API `enable_thinking`. The existing `_mm_mode` state machine in `_stream_chat_once()` parses these correctly.
- **Pricing**: $0.30/M input, $1.20/M output (same as M2.5)
- **`is_minimax()` match**: ✅ Yes (substring 'minimax')
- **Latency**: ~2.3s average

### gemini-3.1-flash-image-preview
- **key_0**: ✅ but extremely rate-limited (RPM < 2, often 429)
- **key_1**: ❌ HTTP 500 "bound must be positive" — no allocation on this key
- **Image generation**: Model responds to image gen prompts with TEXT describing what it would generate (no actual image data returned via OpenAI-compatible API). The `inline_data` format expected by `image_gen.py` was never observed. Likely requires native Gemini API or `response_modalities` parameter not supported by this proxy.
- **Text-only**: Works fine as a regular chat model
- **Pricing**: $0.25/M input, $1.50/M output

## Embedding Models

| Model | Dim | key_0 | key_1 | Latency | RPM |
|-------|-----|-------|-------|---------|-----|
| text-embedding-v4 | 1024 | ✅ | ✅ | ~320ms | ~105/key |
| text-embedding-3-small | 1536 | ✅ | ✅ | ~3000ms | ~68/key |
| text-embedding-3-large | 3072 | ✅ | ✅ | ~5000ms | ~32/key |

**Recommended**: `text-embedding-v4` — 10x faster than alternatives, good quality.

## Files Modified
- `lib/__init__.py`: MINIMAX_MODEL→M2.7, added GEMINI_PRO_PREVIEW_MODEL, IMAGE_GEN_MODEL, EMBEDDING_MODELS, pricing
- `lib/llm_dispatch/config.py`: Added slots for new models with correct RPM/caps
- `lib/tasks_pkg/model_config.py`: Preset 'minimax' now routes to MiniMax-M2.7 (via MINIMAX_MODEL constant)
- `lib/embeddings.py`: New module — embed_texts, cosine_similarity, semantic_search
- `lib/image_gen.py`: New module — generate_image via chat completions
- `routes/common.py`: Added /api/images/generate endpoint
- `index.html`: Updated MiniMax label to M2.7
- `static/js/main.js`: Updated all MiniMax label references to M2.7
