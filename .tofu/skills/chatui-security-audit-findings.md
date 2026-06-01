---
name: chatui-security-audit-findings
description: Tofu security audit: tri-state auth_mode (open default 2026-05-27); shell=True + Feishu hardcoded secrets remain
enabled: true
tags: [security, audit, authentication, injection]
created: 2026-03-17T09:22:58Z
updated: 2026-05-27T03:57:39Z
---

# Tofu Security Audit — Key Findings & Status

## Resolved
1. **No real authentication** — RESOLVED (2026-05-26 unification). Single global
   `auth_before_request` gate in `routes/api_v1/auth.py` covers ALL non-public
   routes. Tokens accepted via `Authorization: Bearer`, `x-api-key`,
   `tofu_session` HttpOnly cookie, `?token=` query string, or legacy
   `TUNNEL_TOKEN` (deprecated). `lib/api_keys.bootstrap_personal_key()`
   persists plaintext at `data/config/.first_run_token` (0600).
2. **Auth-mode tri-state** (2026-05-27): `lib/auth_mode.py` adds
   `open` / `private` / `multi-user`, persisted at `data/config/auth.json`,
   env-lockable via `TOFU_AUTH_MODE`. `open` is now the DEFAULT for personal
   installs (paired with default bind `127.0.0.1`). `open` mode passes through
   with a synthetic local-admin AuthContext (`via_open_mode=True`); rate
   limits + idempotency bypass that synthetic principal. `private` is today's
   gate; `multi-user` is the same gate semantically reserved for relay
   deployments. UI mode switch lives in Settings → API Keys.
3. **`/api/me` hardcoded stub** — STILL stubbed in `routes/common.py:114`, but
   sits behind the unified gate so unauthenticated callers in `private`/
   `multi-user` get 401, never the stub.

## Still Open
- **3 shell=True injection points**: `lib/scheduler.py:328`,
  `lib/project_mod/tools.py:437`, `lib/desktop_agent.py:155` — gated by auth
  now, but still subprocess-via-shell. Refactor to `shlex.split` + list args.
- **Hardcoded Feishu APP_SECRET** in `lib/feishu_bot.py:43-44` with fallback
  values in source code. Move to env-only, fail-closed.
- **Flask secret_key** is `_load_or_create_flask_secret_key()` in `server.py`
  — generates per-install random key at `data/config/flask_secret_key`.
- **Browser/Desktop bridge endpoints**: still go through unified gate; verify
  the local browser extension still authenticates correctly under the new
  `open` default.
- **SVG upload allowed** in `routes/common.py:249` — XSS risk; serve with
  `Content-Disposition: attachment` or strip `<script>` on upload.

## Rate Limiting
- `routes/api_v1/auth.py` runs pre-flight bucket check (`lib/rate_limit_api.py`)
  for every authenticated API request. Standard `X-RateLimit-*` headers
  attached even on public paths when a Bearer key is present.
- Cookie-auth UI calls, tunnel calls, AND `via_open_mode` synthetic contexts
  bypass the bucket.

## Relay/Billing Status (2026-05-27)
- Foundations exist: `lib/api_keys.py`, `lib/rate_limit_api.py` (RPM+TPD),
  `lib/usage_tracker.py` (per-key daily counters), `lib/idempotency.py`,
  audit log, OpenAPI 3.1, OpenAI/Anthropic compat surfaces, Settings →
  API Keys UI.
- NOT YET BUILT (planned): `lib/billing/{pricing,cost,wallet,ledger}.py`,
  payments (Stripe/Alipay), users table, admin console at `/admin/*`,
  customer dashboard at `/dashboard/*`. Reference architecture for the
  build-out: `songquanpeng/one-api` (NewAPI). See discussion in
  conversation history for phased roll-out plan.

## Auth Test Coverage (2026-05-27)
- `tests/test_auth_mode.py` (8) — unit + open-mode E2E.
- `tests/test_e2e_headless_api.py` (36): private-mode auth + scopes.
- `tests/test_api_keys.py` (12), `tests/test_rate_limit_api.py` (9).
- `tests/conftest.py` pins `TOFU_AUTH_MODE=private` for the suite.

## Security Invariants (don't break)
1. In `private` / `multi-user` modes, every `/api/*`, `/v1/*`, `/metrics`
   request not in `_PUBLIC_EXACT` / `_PUBLIC_PREFIXES` returns 401 without
   a credential. Within those modes, NO env var changes this.
2. `open` mode is a deliberate, persisted policy choice (default for
   personal installs). It is NOT a hidden bypass — it is documented in
   `data/config/auth.json` and surfaced in the boot banner.
3. Bootstrap NEVER mints a key when `TUNNEL_TOKEN` is set or when the
   mode is `open`.
4. `?token=` is stripped + redirected before any route sees it (private/
   multi-user mode only).
5. The public allow-list (`_PUBLIC_EXACT` in `routes/api_v1/auth.py`) is
   short by design — every entry is a potential information leak. Adding
   one requires a justification comment.

## Good Practices Already Present
- Parameterized SQL queries throughout (no SQL injection)
- `DANGEROUS_PATTERNS` blocklist for project commands (though bypassable)
- File upload extension whitelist (still needs magic bytes validation)
- `MAX_CONTENT_LENGTH = 50MB` set globally (`server.py:447`)
- `audit.log` logs every auth event (`api_request_auth`, `api_forbidden`,
  `api_key_bootstrap`, `api_key_created/revoked/updated`,
  `auth_mode_changed`)

