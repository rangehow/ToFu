---
name: oauth-china-geo-block-proxy-flow
description: OAuth geo-block: server egress 403-blocked AND browser fetch CORS-blocked (no ACAO on token endpoint); only working path is B2 curl-assisted: user terminal does the POST, paste token JSON to /api/oauth/store-token
enabled: true
tags: [oauth, proxy, china, geo-block]
created: 2026-04-05T07:25:23Z
updated: 2026-06-24T10:53:57Z
---

# OAuth Flow for Chinese Users — Network Architecture

## Three networks, and ALL THREE auto-paths can fail
| Step | Where | Network | Status under geo-block |
|---|---|---|---|
| Popup authorize (`claude.ai/oauth/authorize`) | local browser | VPN/Clash | ✅ works |
| Token exchange — SERVER (`/api/oauth/callback`) | remote server | corporate proxy | ❌ 403 geo-block |
| Token exchange — BROWSER fetch (B1) | local browser | VPN | ❌ CORS-blocked |
| Token exchange — TERMINAL curl (B2) | user terminal | VPN | ✅ works |
| API calls after login | remote server | corporate proxy | works (uses x-api-key) |

## The 403 + CORS double-block (2026-06)
- SERVER POST → `console.anthropic.com/v1/oauth/token` → **403
  `{"error":{"type":"forbidden","message":"Request not allowed"}}`**, `cf-ray:…-HKG`.
  Edge/geo block on the server egress IP. NOT a UA issue (tested 3 UAs).
  `auth.openai.com` similarly 403s `unsupported_country_region_territory`.
- BROWSER `fetch(token_url)` (B1) → **CORS preflight blocked**: the token
  endpoint serves the CLI and sends NO `Access-Control-Allow-Origin`. Cannot be
  worked around from JS. So B1 silently falls back to the geo-blocked server.
  Diagnostic tell: a working curl carrying `Referer: …vscode-….sankuai.com` proves
  the browser ran B1 but it threw → fell back → user still sees the server 403.

## Fix 1: surface the REAL reason
`lib/oauth/token_store.py::OAuthExchangeError(status_code, detail)`. exchange fns
RAISE it; `_explain_exchange_failure()` splits 403 geo-block / 400-401 invalid_grant
/ status-0 net. `manager.exchange_code` → `{error,status_code,detail}`.

## Fix 2: B2 curl-assisted manual exchange (the ONLY reliable path here)
`static/js/settings/oauth.js`:
- `_completeLogin` order: B1 browser fetch → server `/callback` → on BOTH failing,
  `_showCurlHelper(provider, code, state)`.
- `_buildCurlCommand` emits the exact curl (code + our PKCE `code_verifier` +
  client_id/redirect_uri; json for claude, form for codex) — the params come from
  the `exchange` block that `/api/oauth/login` now returns.
- User runs it in their VPN terminal, pastes the returned token JSON into the
  manual box. `_oauthManualSubmit` detects a `{`-leading paste with `access_token`
  → `_storeBrowserToken` → `POST /api/oauth/store-token` → `manager.store_token`
  → `*_store_token` (save_token + provision_oauth_provider). NO exchange (terminal
  already did it).
- `store_token` accepts Anthropic's real shape `{access_token, refresh_token,
  expires_in, account:{email_address}}`. KNOWN: email comes back blank (it's nested
  under account.email_address; `_extract_email_from_token` only checks top-level
  email/id_token) — cosmetic, token works.

## Mandatory / gotchas
- Server-side OAuth HTTP must keep `lib.http_client.http_post` (auto `proxies_for`).
- Patch `lib.oauth.claude.save_token` (module ns), NOT `token_store.save_token`.
- VSCode proxy: OAuth fetch must use `apiUrl()` not raw `/api/`.
- Don't auto-reset on popup close — user may be pasting code/curl JSON.

