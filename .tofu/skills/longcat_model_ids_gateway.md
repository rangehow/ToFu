---
name: longcat_model_ids_gateway
description: LongCat valid model IDs on Sankuai AIGC gateway; longcat-pro-0403 was dead
enabled: true
tags: [llm, dispatch, longcat, meituan]
created: 2026-04-18T03:13:14Z
updated: 2026-04-18T03:13:14Z
---

# LongCat model IDs on the Meituan (aigc.sankuai.com) gateway

## Valid public LongCat IDs (per longcat.chat/platform/docs)
- `LongCat-Flash-Chat` (auto-routes to `LongCat-Flash-Chat-2603`)
- `LongCat-Flash-Thinking` (auto-routes to `LongCat-Flash-Thinking-2601`)
- `LongCat-Flash-Thinking-2601` ← currently used in our config (free, thinking+tools)
- `LongCat-Flash-Chat-2603`
- `LongCat-Flash-Lite`, `LongCat-Flash-Omni-2603`

## Correct request shape
- `enable_thinking=True|False` (LongCat flag; handled in `build_body()` at `lib/llm_client.py:1192`)
- `temperature=1.0` when thinking enabled, else ≤0.7
- Tool calling: plain OpenAI-style `tools=[…]` + `tool_choice` — gateway translates to model-native `<longcat_tool_call>` format internally
- Max output: LongCat-Flash-Thinking-2601 supports up to 262_144 tokens (our `_MODEL_MAX_OUTPUT` cap is conservative 65536)

## Dead ID removed 2026-04-18: `longcat-pro-0403`
- Never existed in public docs / change-log
- Gateway returned `{"error":{"message":"配置不存在"}}` HTTP 400 — routing-layer rejection, not payload issue
- Removed from 4 locations:
  - `lib/llm_dispatch/config.py` DEFAULT_SLOT_CONFIGS
  - `lib/pricing.py` MODEL_PRICING
  - `static/provider_templates/meituan.json`
  - `data/config/server_config.json` (sankuai provider)

## Diagnosing "配置不存在" errors from aigc.sankuai.com
This message is emitted by the gateway's routing layer *before* the body is inspected.
It means: the model ID isn't registered for the caller's AppId. Check:
1. Typo in model_id / case mismatch
2. Model decommissioned on gateway — cross-check against provider's current public docs
3. AppId not whitelisted for private/preview model

