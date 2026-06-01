---
name: centralized-proxy-bypass-module
description: Unified proxy config via lib/proxy.py: single 'bypass domains' list auto-syncs to both proxies_for() per-request bypass AND no_proxy env var — no separate no_proxy field; legacy proxy_config.no_proxy auto-migrated on startup
enabled: true
tags: [python, proxy, network, architecture, settings, unified, hot-reload, migration, no_proxy, proxy-bypass]
created: 2026-03-31T13:48:19Z
updated: 2026-04-01T03:34:59Z
---

# Unified Proxy Configuration — lib/proxy.py

## Architecture (post-unification)

Previously there were two redundant bypass mechanisms:
1. `no_proxy` env var (via `proxy_config.no_proxy` in Settings)
2. `proxy_bypass_domains` (separate Settings field → `proxies_for()`)

**Now unified into ONE setting**: "不代理域名" (Bypass Domains) in Settings UI.

Under the hood, `set_bypass_domains()` auto-syncs to **both**:
- Per-request bypass via `proxies_for(url)` (suffix match → `{'no_proxy': '*'}`)
- Global `no_proxy` env var via `_sync_no_proxy()` (for any code using `requests` directly)

## Key functions

- `proxies_for(url)` — returns `{'no_proxy': '*'}` if URL matches bypass domains, else `{}`
- `set_bypass_domains(domains)` — hot-reload from Settings, triggers `_rebuild()` + `_sync_no_proxy()`
- `get_bypass_domains()` — returns settings-only domains (for UI)
- `set_proxy_config(http_proxy, https_proxy)` — set proxy address (no_proxy param deprecated/ignored)
- `get_proxy_config()` — for UI display (no_proxy is read-only, auto-managed)

## Data flow

1. `PROXY_BYPASS_DOMAINS` env var → `_env_domains` (baseline, read once)
2. Settings UI textarea → `set_bypass_domains()` → `_settings_domains`
3. `_rebuild()` merges both → `_bypass_domains` tuple
4. `_sync_no_proxy()` rebuilds `no_proxy` env from: `_ALWAYS_BYPASS` + original env `NO_PROXY` + `_bypass_domains`

## Files

- `lib/proxy.py` — all proxy logic, auto-syncs no_proxy
- `server.py` — loads persisted config at startup, auto-migrates legacy `proxy_config.no_proxy`
- `routes/common.py` GET/POST — serves/saves `proxy_bypass_domains` (no `proxy_config.no_proxy`)
- `settings.js` — single bypass textarea, no separate no_proxy input
- `index.html` — unified "不代理域名" section

## Migration

On startup, `server.py` checks for legacy `proxy_config.no_proxy`:
- Splits by comma, deduplicates against existing `proxy_bypass_domains`
- Skips standard entries (localhost, 127.0.0.1, 0.0.0.0)
- Merges into `proxy_bypass_domains`, removes `no_proxy` key, saves back

## Config (server_config.json)

```json
{
  "proxy_config": {
    "http_proxy": "http://proxy:8080",
    "https_proxy": "http://proxy:8080"
  },
  "proxy_bypass_domains": [".sankuai.com", ".internal.example.com"]
}
```

Note: `proxy_config.no_proxy` is no longer stored (auto-managed).

