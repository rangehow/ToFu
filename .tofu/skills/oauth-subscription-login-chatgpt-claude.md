---
name: oauth-subscription-login-chatgpt-claude
description: OAuth PKCE login for ChatGPT Plus (Codex) and Claude Pro — console callback flow for Claude, localhost relay for Codex, JSON token exchange
enabled: true
tags: [oauth, chatgpt, claude, codex, authentication]
created: 2026-04-03T10:17:58Z
updated: 2026-04-05T04:43:49Z
---

# OAuth Subscription Login — ChatGPT Plus & Claude Pro

## Architecture

### Claude Pro: Console Callback Flow
```
User clicks "登录"
  → Frontend: fetch POST /api/oauth/login
  → Server: generate PKCE + auth_url (redirect_uri=console.anthropic.com/oauth/code/callback)
  → Frontend: window.open(auth_url) as popup
  
User authenticates in popup on claude.ai
  → Anthropic redirects to console.anthropic.com/oauth/code/callback
  → Console page shows code#state for user to copy
  
User copies code#state, pastes in Tofu input box
  → Frontend: fetch POST /api/oauth/callback {provider, code, state}
  → Server: PKCE code exchange via console.anthropic.com/v1/oauth/token (JSON body)
  → Frontend: update UI to "已登录"
```

### ChatGPT (Codex): Localhost Relay Flow
```
User clicks "登录"
  → Frontend: fetch POST /api/oauth/login
  → Server: generate PKCE + auth_url, start relay HTTP server on port 1455
  → Frontend: window.open(auth_url) as popup
  
User authenticates in popup
  → OAuth redirects to localhost:1455/auth/callback?code=XXX
  → Relay server serves HTML with postMessage()
  → Main window receives postMessage → auto-submits code
```

### Key Differences (2026-04 fix)
| | Claude | Codex |
|---|---|---|
| Redirect URI | `console.anthropic.com/oauth/code/callback` | `http://localhost:1455/auth/callback` |
| Auto-callback | NO — user must copy code#state | YES — postMessage relay |
| Token URL | `console.anthropic.com/v1/oauth/token` | `auth.openai.com/oauth/token` |
| Token Content-Type | `application/json` | `application/x-www-form-urlencoded` |
| Scope | `org:create_api_key user:profile user:inference` | `openid email profile offline_access` |
| Extra auth params | `code=true` | none |

### OAuth Parameters
| | Claude | Codex |
|---|---|---|
| Auth URL | `claude.ai/oauth/authorize` | `auth.openai.com/oauth/authorize` |
| Token URL | `console.anthropic.com/v1/oauth/token` | `auth.openai.com/oauth/token` |
| Client ID | `9d1c250a-...` | `app_EMoamEEZ73f0CkXaXp7hrann` |
| Redirect URI | `console.anthropic.com/oauth/code/callback` | `http://localhost:1455/auth/callback` |

### Files
- `lib/oauth/manager.py` — Flow management + relay server (Codex only) + `exchange_code()`
- `lib/oauth/claude.py` — Claude OAuth config + token exchange (JSON body)
- `lib/oauth/codex.py` — Codex OAuth config + Responses API translator
- `lib/oauth/pkce.py` — RFC 7636 PKCE generation
- `lib/oauth/token_store.py` — Persist to `data/config/oauth/`
- `routes/oauth.py` — 4 endpoints: login, callback, status, logout (all GET+POST)
- `static/js/settings.js` — postMessage listener + manual paste + POST→GET fallback

### Bug fix: 2026-04-05
**Root cause**: Claude OAuth config had wrong redirect_uri (`localhost:54545`), wrong token_url (`api.anthropic.com`), wrong scope, wrong Content-Type (form-urlencoded instead of JSON). These are the registered values for the Claude Code official OAuth client.
**Fix**: Aligned all OAuth parameters with the official Claude Code implementation. Changed flow to console callback (user copies code#state manually).

### Anthropic region block
`claude.ai/oauth/authorize` is geo-blocked in some regions (China/HK). Server-side curl shows 302 → `anthropic.com/app-unavailable-in-region`. Users must open the auth URL in a browser with VPN/proxy access.

### Proxy 405 Workaround
Some proxy layers reject POST on certain paths with HTTP 405. All endpoints accept both GET and POST with auto-retry fallback in frontend.

