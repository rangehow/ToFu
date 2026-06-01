---
name: per-provider-pricing-overrides
description: Provider-scoped pricing: PROVIDER_PRICING + lookup_pricing(model, provider_id) override the flat MODEL_PRICING when same model_id is hosted by multiple gateways at different rates
enabled: true
tags: [pricing, provider, convention]
created: 2026-05-08T13:42:30Z
updated: 2026-05-08T13:42:30Z
---

# Per-Provider Pricing Overrides (added 2026-05-08)

## Why
The flat `MODEL_PRICING` table in `lib/pricing.py` keys by `model_id`
only. When the same model is sold on multiple gateways at different
prices (e.g. `kimi-k2.6` at $0.6/$2.8 on Moonshot direct vs
$0.898/$3.729 on Tencent TokenHub) the global table can hold only one,
silently mis-billing the others.

## Mechanism

### Storage
- `lib/pricing.py::PROVIDER_PRICING` = `{provider_id: {model_id: {input, output, ...}}}`.
- Mutated through `set_provider_pricing(provider_id, model_id, info)` and
  `clear_provider_pricing(provider_id)`.
- Snapshot via `get_provider_pricing_snapshot()`.

### Lookup
- `lookup_pricing(model_id, provider_id=None)` resolves in order:
  1. `PROVIDER_PRICING[provider_id][model_id]` if `provider_id` given.
  2. `MODEL_PRICING[model_id]` global fallback.
  3. `None`.

### Population (one-shot, on every `/api/server-config` call)
`routes/config.py::server_config()` iterates `providers[*].models` and:
- Calls `clear_provider_pricing(provider_id)` first (idempotent reload).
- For each model with a `pricing` dict (input + output required),
  calls `set_provider_pricing(...)` and surfaces it in the response as
  `provider_pricing[provider_id][model_id]`.
- Templates without `pricing` are unaffected — they fall back to the
  global `MODEL_PRICING`.

## Provider template schema
Each `models[]` row in `static/provider_templates/<vendor>.json` may add:
```json
{
  "model_id": "kimi-k2.6",
  "capabilities": ["text", "cheap"],
  "rpm": 30,
  "cost": 0.003,
  "pricing": {
    "input": 0.898,
    "output": 3.729,
    "cacheWriteMul": 1.0,
    "cacheReadMul": 0.169,
    "name": "Kimi K2.6 (TokenHub)"
  }
}
```

## Cost-calc consumers
Both backend and frontend now accept `provider_id`:
- `routes/daily_report.py::_calc_msg_cost_cny(usage, model, provider_id='')`
- `static/js/core.js::calcCostCny(usage, modelOrPreset, providerId)`
  — uses `_providerPricingCache` populated from
  `data.provider_pricing` in `static/js/main.js`.

Each persisted message carries `provider_id` (added in
`lib/tasks_pkg/manager.py::_sync_result_to_conversation`), so
historical conversations bill correctly after this change.

## Cheap-tag note
The `cheap` capability tag is still global (computed from
`MODEL_PRICING` only). Rationale: it's a model-family judgment, not a
per-deployment one — TokenHub's surcharge doesn't change whether
`kimi-k2.6` qualifies as "cheap" for routing.

## Tencent TokenHub specifically
- Base URL: `https://tokenhub.tencentmaas.com/v1` (Guangzhou).
  Singapore mirror at `tokenhub-intl.tencentmaas.com`.
  Backup CN domains: `*.tencentmaas.cn`.
- Auth: `Authorization: Bearer sk-...`.
- Model catalog (chat): `hy3-preview`, `hunyuan-2.0-thinking-20251109`,
  `hunyuan-2.0-instruct-20251111`, `hunyuan-role-latest`,
  `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v3.2`,
  `deepseek-v3.1-terminus`, `deepseek-r1-0528`, `deepseek-v3-0324`,
  `glm-5`, `glm-5-turbo`, `glm-5.1`, `glm-5v-turbo`, `kimi-k2.5`,
  `kimi-k2.6`, `minimax-m2.5`, `minimax-m2.7`.
- Image / video / 3D models exist but use different endpoints — NOT
  added to the chat template.
- Token Plan subscription endpoint is separate:
  `https://api.lkeap.cloud.tencent.com/plan/v3` with `sk-tp-...` keys.
  Not added (subscription-only, no per-token billing).

