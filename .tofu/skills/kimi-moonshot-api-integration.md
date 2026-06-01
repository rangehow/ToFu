---
name: kimi-moonshot-api-integration
description: Kimi (Moonshot AI) API: OpenAI-compatible, thinking.type format, K2.5 fixed temps (1.0/0.6), reasoning_content field, vision on K2.5 only
enabled: true
tags: [kimi, moonshot, api, provider, models, thinking]
created: 2026-04-09T09:01:11Z
updated: 2026-04-09T09:01:11Z
---

# Kimi (Moonshot AI) API Integration

## Endpoints
- Global: `https://api.moonshot.ai/v1`
- China: `https://api.moonshot.cn/v1`
- OpenAI-compatible `/v1/chat/completions`

## Models (as of 2026-04)
| Model | Context | Vision | Thinking | Output Price |
|-------|---------|--------|----------|-------------|
| kimi-k2.5 | 262K | ✅ | configurable | $3.00/M |
| kimi-k2-0905-preview | 262K | ❌ | configurable | $2.50/M |
| kimi-k2-thinking | 262K | ❌ | always-on | $2.50/M |
| kimi-k2-turbo-preview | 262K | ❌ | ❌ | $8.00/M |
| kimi-k2-thinking-turbo | 262K | ❌ | always-on | $8.00/M |

## Thinking Format
- `{"thinking": {"type": "enabled"}}` / `{"thinking": {"type": "disabled"}}`
- `kimi-k2-thinking`: thinking always on (ignore disable)
- `kimi-k2-thinking-turbo`: turbo variant, respects thinking flag despite name
- Reasoning output in `reasoning_content` field (same as DeepSeek)

## ⚠️ K2.5 Fixed Parameters
- temperature: 1.0 (thinking) or 0.6 (non-thinking) — other values → HTTP 400
- top_p: fixed 0.95 — other values → HTTP 400
- n, presence_penalty, frequency_penalty: must be default — strip from body

## Max Output Tokens
- K2/K2.5: 32,768
- moonshot-v1-*: 16,384

## Brand
- Icon: official LobeHub SVG (K-shaped mark + dot)
- Color: `#ccc` (light gray for dark theme — logo is monochrome black/white)
- Detection: `kimi` or `moonshot` in model/url
- Brand guide: https://moonshotai.github.io/Branding-Guide/

## Files Changed
- `lib/model_info.py`: `is_kimi()`, `_kimi_max_output()`, added to `_MODEL_MAX_OUTPUT`
- `lib/llm_client.py`: import is_kimi, Kimi-specific build_body branch
- `lib/llm_dispatch/discovery.py`: api.moonshot.ai domain, _VISION_PAT for K2.5
- `static/js/settings.js`: brand icon, color, pattern, provider template

