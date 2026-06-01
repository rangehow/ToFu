---
name: oauth-china-geo-block-proxy-flow
description: OAuth for Chinese users: popup uses local browser (needs Clash), token exchange uses server (needs corporate proxy); geo-block diagnosis and workarounds
enabled: true
tags: [oauth, proxy, china, geo-block]
created: 2026-04-05T07:25:23Z
updated: 2026-04-05T07:25:23Z
---

# OAuth Flow for Chinese Users — Network Architecture

## Two Different Networks in Play

| Step | Where it runs | Network | Fix |
|---|---|---|---|
| **Popup** (`claude.ai/oauth/authorize`) | User's local browser | Local machine → needs Clash/VPN | User configures Clash rules |
| **Token exchange** (`console.anthropic.com/v1/oauth/token`) | Remote server | Server → corporate proxy | `proxies_for(url)` in requests calls |
| **API calls** (after login) | Remote server | Server → corporate proxy | Same |

## Key Findings (2026-04)

- `console.anthropic.com` — reachable from corporate proxy (Meituan `10.229.18.27:8412`)
- `claude.ai` — geo-blocked by Cloudflare (302 → app-unavailable-in-region)
- `auth.openai.com` — geo-blocked (403 unsupported_country_region_territory) from BOTH proxy and direct

## Mandatory: Use `proxies_for()` in OAuth HTTP Calls

All `requests.post()` in `lib/oauth/claude.py` and `lib/oauth/codex.py` MUST include:
```python
from lib.proxy import proxies_for
resp = requests.post(url, json=payload, proxies=proxies_for(url), timeout=30)
```

## UX for Chinese Users

- Show auth URL in a copyable input so user can open in a proxied browser on any device
- Show geo-block warning inline on the OAuth cards
- Don't auto-reset on popup close — user may be copying code manually
- `/api/oauth/test` endpoint for server-side connectivity diagnosis

## Common Issues

1. **Popup shows "unsupported region"** → User's local Clash not routing `claude.ai`/`auth.openai.com`
2. **Token exchange fails** → Server can't reach endpoint, check proxy config
3. **VSCode proxy 404** → OAuth fetch calls must use `apiUrl()` not raw `/api/` paths

