---
name: local-endpoints-vllm-sglang-design
description: Local self-hosted LLM endpoints: ONE provider with `endpoints: [...]`, multi-URL slot fan-out, mandatory no-proxy host registration
enabled: true
tags: [llm-dispatch, local-endpoints, vllm, sglang, proxy, convention]
created: 2026-05-13T04:28:54Z
updated: 2026-05-13T04:28:54Z
---


# Local self-hosted LLM endpoints (vLLM / SGLang / Ollama)

One engine-agnostic `local` brand for all OSS-engine deployments.
A single provider entry with `brand=='local'` carries an `endpoints: [str, ...]`
list — the dispatcher fans out slots across (endpoint × key) so it
load-balances across the fleet automatically. Do NOT create one
`local_*` provider per URL.

## Provider data shape (server_config.json)

```jsonc
{
  "id": "local",
  "name": "本地部署模型",
  "brand": "local",
  "enabled": true,
  "endpoints": [               // ← multi-URL fleet
    "http://10.0.0.5:8000/v1",
    "http://10.0.0.6:8000/v1"
  ],
  "base_url": "http://10.0.0.5:8000/v1",  // first endpoint, kept for legacy callers
  "api_keys": [""],            // empty string is OK — most OSS engines have no auth
  "models": [...],
  "thinking_format": "..."
}
```

Backwards-compat: if `endpoints` is absent/empty, the dispatcher falls back
to `[base_url]`.

## Slot fan-out (`lib/llm_dispatch/dispatcher.py:_build_slots_from_providers`)

For each provider, slots are built across **endpoints × keys × models**.
Local providers with empty `api_keys` get a single blank-key slot. Each
slot's `key_name` is suffixed `_ep<idx>` when there are multiple
endpoints so per-slot cooldowns don't clobber across endpoints.

## Proxy bypass (CRITICAL — caused a real outage)

Self-hosted endpoints often live on private OR pseudo-private (corporate
public-but-internal) IPs that the corporate `https_proxy` can't reach.
RFC1918-only detection is NOT enough: e.g. `33.x.x.x` is publicly-routable
but only routable from inside Meituan's network.

`lib/proxy.py:register_no_proxy_host(host)` is the canonical fix. It:
- adds the literal host to `_registered_hosts` (matched in `proxies_for`)
- appends to the `no_proxy` env var so any third-party `requests` call
  without our `proxies=` wrapper still bypasses

Three call sites register hosts automatically:
1. `lib/llm_dispatch/dispatcher.py:_build_slots_from_providers` for every
   endpoint of every `brand=='local'` provider — runs at server boot.
2. `lib/llm_dispatch/discovery.py:probe_provider` when `is_local` is True.
3. `lib/llm_dispatch/health_local.py:_check_endpoint` on every poll
   (cheap, idempotent).

If a future code path probes a local endpoint without going through one
of these, it MUST call `register_no_proxy_url(url)` first or the very
first request will time out via the corp proxy.

## Health checker (`lib/llm_dispatch/health_local.py`)

Per-endpoint, not per-provider:
- Probes each endpoint in `provider['endpoints']`.
- Cools down only the slots whose `base_url` matches the dead endpoint
  (`_cooldown_endpoint_slots`).
- On recovery, clears that endpoint's cooldowns and unions served-model
  sets across live endpoints. Resync trigger:
  - `not configured_ids` (provider was added when down), OR
  - `union_served != configured_ids` (drift), OR
  - `max(success_streak per endpoint) % RESYNC_EVERY == 0`.
- Filters `discover_models` output to the union — protects against a
  transient endpoint outage dropping that endpoint's private models.

## Frontend convention (`static/js/settings.js`)

A `brand=='local'` provider renders as a SPECIAL stg-provider-card:
- single textarea `data-local-endpoints` with one URL per line
- `_onLocalEndpoints(provIdx, value)` keeps `endpoints[]` and `base_url`
  in sync (dedupe + first-as-base_url)
- replaces "🔍 自动发现" button with "🔍 探测全部端点"
  → calls `_discoverLocalModels(provIdx)` which hits
  `POST /api/provider-probe-bulk` with the URL list, then merges the
  union of served models. Per-endpoint OK/fail rows render in the
  `#stgLocalStatus_<provIdx>` div on the same card.
- HIDES balance URL, models_path, extra_headers fields (irrelevant for
  OSS engines and clutters the UI).

The "🖥️ 本地部署模型" button (in `index.html`) calls `addLocalProvider()`,
which is idempotent: if a local provider already exists, it just expands
that card instead of creating a new one. There should normally be ONE
local provider in the system.

## Migration note

Earlier code created N separate `local_<host>` providers via a bulk-modal.
A migration in `data/config/server_config.json` consolidated them into a
single `id: 'local'` entry with `endpoints: [...]`. If any old `local_*`
entries remain in a fresh deployment, run the consolidation snippet from
the project history (look for `local_endpoints =` in earlier commits).

