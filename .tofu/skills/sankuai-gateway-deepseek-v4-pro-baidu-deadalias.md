---
name: sankuai-gateway-deepseek-v4-pro-baidu-deadalias
description: Meituan AIGC gateway: deepseek-v4-pro-baidu alias does NOT exist; only -tencent works for v4-pro
enabled: true
tags: [sankuai, deepseek, model-alias, config]
created: 2026-05-11T02:24:38Z
updated: 2026-05-11T02:24:38Z
---

# `deepseek-v4-pro-baidu` is a dead alias on the Meituan AIGC gateway

Observed 2026-05-10 / 2026-05-11. The Meituan AIGC gateway (`https://aigc.sankuai.com/v1/openai/native`) returns
HTTP 400 with body `{"status":400,"message":"请求格式有误，请检查 traceId=..."}` for **every** request to
`deepseek-v4-pro-baidu`. The deployment doesn't exist on this gateway.

Working aliases for `deepseek-v4-pro` on the same gateway: `deepseek-v4-pro-tencent` only. Do NOT add `-baidu`.

For the v3.2 family the gateway DOES support all four backends (`-tencent`, `-baidu`, `-huawei`, `-doubao`) — see `static/provider_templates/meituan.json:28`. The naming pattern is per-model, not universal.

For the v4-flash family the gateway supports `-huawei` and `-tencent`. Don't blindly add `-baidu` there either without probing.

## Symptom in logs

```
[ERROR] lib.llm_errors [translate-...]: [Translate][D:sankuai_key_0:deepseek-v4-pro-baidu]
Non-retryable API error (HTTP 400): API HTTP 400: {"status":400,"message":"请求格式有误..."}
```

`_classify_http_error` in `lib/llm_errors.py:359` falls through to a plain `Exception` for this
generic 400 (no token-limit pattern, no image error, no prompt-too-long, no stream-only marker),
which translate.py treats as a transient dispatch error and retries on a different model — so the
user-visible behavior is "translation eventually succeeds on a fallback model" but error.log gets noisy.

## Authoritative reference

`static/provider_templates/meituan.json` is the source of truth for which aliases to wire on the
sankuai gateway. The deepseek-v4-pro entry there has NO aliases. If a saved
`data/config/server_config.json` lists extra aliases, they were added by hand and may not exist
upstream — verify by probing before trusting them.

## Fix when you see this

1. Edit `data/config/server_config.json` → providers[sankuai].models — remove the dead alias from
   the `aliases` array of the affected model.
2. Restart the server. Slots are built once via `LLMDispatcher._initialized` guard
   (`lib/llm_dispatch/dispatcher.py:_build_slots`), so a hot-reload won't drop the dead slot.

