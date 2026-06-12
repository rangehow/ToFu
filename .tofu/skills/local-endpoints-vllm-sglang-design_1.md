---
name: local-endpoints-vllm-sglang-design
description: Local self-hosted LLM endpoints: ONE provider with endpoints:[...], multi-URL slot fan-out, mandatory no-proxy registration via should_bypass_proxy (raw-IP incl. 33.x), NOT just is_local_endpoint
enabled: true
tags: [llm-dispatch, local-endpoints, vllm, sglang, proxy, convention]
created: 2026-05-13T04:28:54Z
updated: 2026-06-06T02:29:00Z
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
  "id": "local", "name": "本地部署模型", "brand": "local", "enabled": true,
  "endpoints": ["http://10.0.0.5:8000/v1", "http://10.0.0.6:8000/v1"],
  "base_url": "http://10.0.0.5:8000/v1",  // first endpoint, kept for legacy callers
  "api_keys": [""],            // empty string OK — most OSS engines have no auth
  "models": [...], "thinking_format": "..."
}
```
Backwards-compat: if `endpoints` is absent/empty, the dispatcher falls back to `[base_url]`.

## Proxy bypass (CRITICAL — caused a real outage, and a SECOND one via ephemeral slots)

Self-hosted endpoints often live on private OR pseudo-private (corporate
public-but-internal) IPs that the corporate `https_proxy` can't reach.
RFC1918-only detection is NOT enough: e.g. `33.x.x.x` is publicly-routable
but only routable from inside the corp network. Symptom when missed:
`ProxyError('Unable to connect to proxy', ConnectionResetError(104))` —
the LLM IP appears in the urllib3 MaxRetryError but `host=` is the corp proxy.

### Two predicates in lib/llm_dispatch/discovery.py
- `is_local_endpoint(url)` — loopback / RFC1918 / link-local / `.local|.internal|.lan|.intranet` / `TOFU_LOCAL_CIDRS`. Used for branding + health-poll inclusion + UI grouping.
- `should_bypass_proxy(url)` = `is_local_endpoint(url) OR is_raw_ip_host(url)` — **this is the correct gate for proxy-bypass registration.** A bare IPv4/IPv6 literal base URL is in practice ALWAYS self-hosted/internal (commercial APIs use domains), so it must bypass even when not RFC1918 (covers 33.x without needing TOFU_LOCAL_CIDRS). Added 2026-06.

### Canonical fix: `lib/proxy.py:register_no_proxy_host(host)`
- adds literal host to `_registered_hosts` (matched in `proxies_for`)
- appends to `no_proxy` env var so third-party `requests` w/o our `proxies=` wrapper still bypasses

### Call sites that auto-register (all must use should_bypass_proxy, NOT is_local_endpoint):
1. `dispatcher.py:_build_slots_from_providers` — for every endpoint of every provider where `brand=='local' OR should_bypass_proxy(url)`. Server boot.
2. `discovery.py:probe_provider` when `is_local`. (probe also force-registers when is_local)
3. `health_local.py:_check_endpoint` — registers UNCONDITIONALLY every poll (cheap, idempotent).
4. `ephemeral.py:mint_ephemeral_slot` — gates on `should_bypass_proxy(base_url)`. **This was the 2026-06 bug**: it gated on `is_local_endpoint`, so ephemeral slots (e.g. `/api/v1/agent/run` BYO `glm5.1-FP8` at `http://33.236.243.109:8080`) sent the FIRST chat request through the corp proxy → connection reset.

If a future code path probes a local endpoint without going through one of these, it MUST call `register_no_proxy_url(url)` first (or check `should_bypass_proxy`).

## Health checker (`lib/llm_dispatch/health_local.py`)
Per-endpoint, not per-provider. Probes each `endpoints` URL; cools down only slots whose `base_url` matches the dead endpoint; on recovery clears cooldowns + unions served-model sets and re-discovers on drift / every RESYNC_EVERY. Filters `discover_models` output to the union so a transient outage doesn't drop a private model.

## Frontend (`static/js/settings.js`)
`brand=='local'` renders a special card: single textarea `data-local-endpoints` (one URL/line), `_onLocalEndpoints` syncs `endpoints[]`+`base_url`, "🔍 探测全部端点" → `POST /api/provider-probe-bulk`, per-endpoint OK/fail rows in `#stgLocalStatus_<idx>`. Balance/models_path/extra_headers hidden. `addLocalProvider()` is idempotent (one local provider total).

