---
name: settings-local-endpoint-rows
description: Local provider settings UI — structured rows + LIVE per-endpoint metrics auto-polled from real traffic
enabled: true
tags: [frontend, settings, ui, dispatcher]
created: 2026-05-13T05:56:22Z
updated: 2026-05-13T08:30:00Z
---

# Local Provider Endpoint UI

When editing the local-deployment provider in Settings (brand === 'local'),
the endpoint list is rendered as **structured rows**, not a plain textarea.
Each row = status light + URL `<input>` + ↻ refresh-metrics button + ✕ delete.
Above the list is a toolbar: + 添加端点, 📝 批量编辑, 🔍 探测全部, 🗑 清空.

## Status comes from REAL TRAFFIC, not synthetic probes

The light colour and inline status strip reflect **live dispatcher stats
recorded from real chat traffic** — TTFT, end-to-end latency, output
throughput (tokens/sec), success rate. NO manual /models probe is needed.
The frontend auto-polls `/api/dispatch/endpoint-metrics` every 10s while
Settings is open.

The `↻` per-row button does NOT call /models — it just bumps the global
metrics poller for an immediate refresh.

Inline strip example (per row):
> TTFT **320ms** · 延迟 **1.5s** · 吞吐 **45 t/s** · 成功率 **100%** · 12 次请求 · 最近 30 秒前

## Backend

- `lib/llm_dispatch/slot.py` — `Slot` now also stores
  `throughput_ema`, `last_success_time`, `last_error_msg`.
  `record_success(latency_ms, ttft_ms=None, output_tokens=0)` updates
  the throughput EMA from `output_tokens / (latency_ms - ttft_ms)`.
- `lib/llm_dispatch/api.py` — all three `slot.record_success()` call
  sites pass `output_tokens` from `usage.completion_tokens` /
  `output_tokens`. The streaming path also forwards captured `ttft_ms`.
- `lib/llm_dispatch/dispatcher.py:get_slots_info()` — exposes the new
  fields plus `base_url` (needed for per-endpoint bucketing).
- `routes/common.py:/api/dispatch/endpoint-metrics` — aggregates slot
  stats by `base_url`. Weighted EMAs by `total_requests`. Returns
  `{endpoints: {url: {ttft_ms, latency_ms, throughput_tps,
  success_rate, total_requests, total_errors, rpm_current, rpm_limit,
  inflight, last_success_ts, last_error_ts, last_error_msg,
  consecutive_errors, available}}}`.

## Frontend (`static/js/settings.js`)

- `_localEndpointMetrics` — keyed by URL, populated by the poller.
- `_localEndpointStatus[url]` — transient probe-state cache, only used
  for "pending" animation while a one-shot probe is in flight; metrics
  take precedence once available.
- `_epRowState(url)` — single source of truth for light colour:
  - `consecutive_errors > 0 && last_error_ts > last_success_ts` → red
  - `success_rate < 0.8 && total_requests >= 5` → red
  - `total_requests > 0` → green
  - else → gray (no traffic yet)
- `_refreshLocalEndpointMetrics()` — fetches once, updates all visible
  rows in-place; called every 10s by `_localMetricsTimer`.
- `_startLocalMetricsPolling()` — kicked off after Settings loads
  the providers tab. `_stopLocalMetricsPolling()` runs in
  `closeSettings()`.

## Provider-card expansion state preservation

`_renderProvidersTab()` snapshots which `.stg-provider-card` is currently
`.expanded` (keyed by `provider.id`) and re-applies that class after
re-render. Without this, clicking "🔍 自动发现" / "🔍 探测全部端点" /
toggling any field would collapse the local card and snap focus back to
the first cloud provider. **DO NOT remove the `prevExpanded` snapshot.**

## Storage shape (`data/config/server_config.json`)

```jsonc
{
  "brand": "local",
  "endpoints": ["http://a/v1", "http://b/v1"],
  "base_url": "http://a/v1",   // auto-synced to endpoints[0]
  "api_keys": [""],
  "models": [...]
}
```

Backwards-compat: dispatcher / health checker / discovery code all fall
back to `[base_url]` when `endpoints` is missing.

## CSS

Class prefix `stg-ep-*` under the `Local Endpoint Rows` section in
`static/styles.css`. Light classes: `ok` (green), `error` (red),
`pending` (amber, animated), `unknown` (gray).
