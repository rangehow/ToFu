---
name: hope-mcp-mlp-port-wave-architecture
description: hope-mcp MLP auth FULLY native+self-sustaining: CIBA login + Token Exchange + refresh-token rotation (single-use, write-back mandatory). NO npm CLI.
enabled: true
tags: [hope-mcp, mlp, auth, wave-port]
created: 2026-05-07T02:25:41Z
updated: 2026-07-01T09:16:14Z
---

# hope-mcp v0.6.0+ — native port of @datafe/mlp-cli

## Why
mlp-cli (TypeScript, ~8K LoC, 119 endpoints) is what the mlp-skills
bundle drives. Ported natively so the LLM gets MLP capabilities without
subprocess + skill-prompt indirection.

## Auth — FULLY NATIVE + SELF-SUSTAINING (v0.6.4, 2026-07-01): NO npm CLI

Three pieces, all in Python, all in `mlp_api.py`:

### 1. Token Exchange (RFC 8693) — the per-call platform token
**`client_secret_jwt`**, NOT `client_secret_post`.
* `POST https://ssosv.sankuai.com/sson/auth/oidc/v1/token`
  (NOT `/oauth2.0/token` → 404).
* `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`.
* `client_assertion` = HS256 JWT signed w/ client_secret; **`aud` = the
  token URL itself**, NOT the target audience (easy to get wrong).
* `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.
* `subject_token` = SSO access_token from `~/.mlp-cli-token.json`.
* `audience` = `com.sankuai.data.ml.platform` / `...wanxiang.wanxiang`.
* `_make_client_assertion_jwt` = stdlib-only HS256. ~3h per-audience cache.
* `_allow_refresh` param: on `invalid_token` (file token looks fresh but
  server rejects — revoked/skew) it force-refreshes then retries exchange ONCE.

### 2. CIBA mobile-push LOGIN — cold-start token grab
`ciba_login(mis, timeout_sec)`; spec from SSO OIDC discovery doc
`/sson/auth/oidc/v1/.well-known/openid-configuration`:
`backchannel_authentication_endpoint=/sson/auth/oidc/v1/bc-authorize`,
delivery `[poll,ping,push]`, `client_secret_jwt`.
* Step 1 `POST /bc-authorize` (form): `client_id`, `client_assertion(_type)`
  (SAME jwt, aud=token URL), `scope=openid profile`, `login_hint=<mis>` →
  `{auth_req_id, interval, expires_in}` + FIRES 大象 push.
* Step 2 poll `POST /token`: `grant_type=urn:openid:params:grant-type:ciba`,
  `auth_req_id`, client_assertion → 400 `authorization_pending` until
  approve → 200 `{access_token, refresh_token, expires_in}` (~259200s=3d).
  `slow_down`→backoff; `access_denied`/`expired_token`→terminal.
* `_write_token_file` writes CLI-schema `{access_token, refresh_token,
  expires_at (abs ms), modified_at, mis}`, chmod 600, clears exchange cache.
* `tools/mlp.py::mlp_login/mlp_whoami/mlp_logout` all native (no
  shutil/subprocess). `mlp_login` mis defaults `$MLP_MIS/$LLM_MIS/$USER`.

### 3. Refresh-token ROTATION — keeps session alive past 3-day expiry
`_refresh_sso_token_blocking(stale, force=False)`, called by
`_sso_token_from_file` when access_token is <60s from `expires_at`:
* `POST /token` `grant_type=refresh_token` + refresh_token + client_secret_jwt
  → 200 `{access_token, refresh_token, expires_in}`.
* **WRITES the fresh access + ROTATED refresh + expires_at back to disk**
  (mandatory — see finding below), clears exchange cache, returns new token.
* `threading.Lock` `_REFRESH_LOCK` serialises concurrent refreshes; a loser
  reuses a token another thread rotated (compares vs known-bad `stale`
  access_token; `force=True` bypasses the plain freshness check for the
  invalid_token path).
* refresh_token rejected (`invalid_grant`) or missing → `NotLoggedInError`
  → `login_required` → `experiment_login` (CIBA). This is the ONLY case
  that needs a new push now.

## ★ CRITICAL: refresh_tokens are SINGLE-USE / ROTATING
Each successful refresh INVALIDATES the old refresh_token and returns a
new one. A refresh WITHOUT write-back strands the file → next attempt gets
`invalid_grant`. So write-back is mandatory. (This bit a manual curl
experiment: it consumed the file's refresh_token, got a rotated one it
never saved → the on-disk copy is now the burned token, so live happy-path
rotation can only be re-proven after a fresh CIBA login mints an unused one.)

## Hardcoded SSO creds
`client_id=35c17c32da` / `client_secret=…` (public in npm pkg). Override:
`MLP_SSO_CLIENT_ID` / `MLP_SSO_CLIENT_SECRET`.

## Token file
`~/.mlp-cli-token.json` — we READ + WRITE it (CLI-written tokens still work).

## Request envelope
* Most: `{code,message,traceId,data}` — `code!=0` = failure.
* `/mlapi/kiln/*`: bare JSON, Cookie auth
  (`com.sankuai.data.ml.platform_ssoid=<token>`).
* `/mlapi/msp/*` + some `/kub/`: need `ml.projectName` cookie (`with_project=True`).

## Wave structure
v0.6.0 Wave 1 (28 mlp_* tools). Later: image, model, sample, km, schedule,
graph, profiler, serving, feature, resource.

## Tests
`tests/test_mlp.py`: stub `mlp_api.requests.Session` + `_exchange_token_blocking`;
reset `_invalidate_mlp_login_cache()`. CIBA: scripted `_FakeCibaSession`,
patch `mlp_api.time.sleep`, `MLP_TOKEN_FILE`→tmp. Refresh: `_FakeRefreshSession`
+ real tmp token file with forced-expired `expires_at`. Full suite 2026-07-01:
**167 passed, 2 skipped**.

