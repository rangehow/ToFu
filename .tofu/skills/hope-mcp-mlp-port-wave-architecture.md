---
name: hope-mcp-mlp-port-wave-architecture
description: hope-mcp v0.6.0+ ports @datafe/mlp-cli endpoints natively. Wave structure + auth/Token-Exchange specifics
enabled: true
tags: [hope-mcp, mlp, auth, wave-port]
created: 2026-05-07T02:25:41Z
updated: 2026-05-07T02:25:41Z
---

# hope-mcp v0.6.0+ — native port of @datafe/mlp-cli

## Why
mlp-cli (TypeScript, ~8K LoC, 23 command groups, 119 endpoints) is what
the mlp-skills bundle drives. We ported it natively into hope-mcp so
the LLM gets MLP capabilities without going through subprocess + skill
prompt indirection.

## Wave structure
Multi-turn migration. v0.6.0 ships **Wave 1** (28 tools); later waves
add image, graph, profiler, schedule, serving, feature, model, etc.

| Wave | Coverage | Status |
|------|----------|--------|
| 1 | auth, runs, jobs, logs, series, queue, project | ✅ shipped (53 total tools = 25 hope_* + 28 mlp_*) |
| 2 | image, model, sample, km, schedule | TODO |
| 3 | graph, profiler, profiler-tp | TODO |
| 4 | serving, feature, resource-analysis, resource-node | TODO |
| 5 | hope, codelab (parts), report, upgrade | TODO |

## Auth — the key trap

**Token Exchange** is `client_secret_jwt`, NOT `client_secret_post`.

* Endpoint: `https://ssosv.sankuai.com/sson/auth/oidc/v1/token`
  (NOT `/oauth2.0/token` — that returns 404).
* `client_assertion` = HS256 JWT signed with the client_secret;
  `aud` claim = the token URL itself, NOT the target audience.
* `client_assertion_type` =
  `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.
* `grant_type` =
  `urn:ietf:params:oauth:grant-type:token-exchange`.
* `audience` = e.g. `com.sankuai.data.ml.platform`, `com.sankuai.data.wanxiang.wanxiang`.
* `Content-Type: application/x-www-form-urlencoded` (not JSON).
* `subject_token` = the SSO `access_token` from `~/.mlp-cli-token.json`.

`hope_mcp.mlp_api._make_client_assertion_jwt` is a stdlib-only HS256
implementation — no PyJWT dependency.

## Hardcoded SSO credentials

mlp-cli ships `client_id=35c17c32da` / `client_secret=…` in its public
npm package. We use the same — overridable via
`MLP_SSO_CLIENT_ID` / `MLP_SSO_CLIENT_SECRET` env vars.

## Token file reuse

We read `~/.mlp-cli-token.json` written by the npm CLI's
`mlp login --ciba --mis <misId>`. This is the ONE thing we don't
reimplement in Python — OIDC PKCE + CIBA poll + MOA local-exchange
all live in the SDK. Strategy:

* `mlp_login` MCP tool shells out to `mlp login` for the initial
  token grab (CIBA blocks waiting for mobile-push approval).
* Everything else reads the token file directly + does Token Exchange
  in-process. ~3 h cache per audience.
* Refresh-token rotation is also left to the npm CLI for now (it's
  done lazily by `getAccessToken()` in sso-auth.ts:160-170).

## Request envelope

* Most paths: `{code, message, traceId, data}` — `code != 0` is failure.
* `/mlapi/kiln/*` paths: bare JSON, no envelope. Use Cookie auth
  (`com.sankuai.data.ml.platform_ssoid=<token>`) instead of the
  `access-token` header.
* `/mlapi/msp/*` and a few `/kub/` paths need the `ml.projectName`
  cookie. Tools that need it pass `with_project=True` to `mlp_request`.

## Endpoint catalogue lives in `mlp_api._ENDPOINTS`

For documentation; runtime requests can hit any path string.

## Live verification (2026-05-07)

* `mlp_run_list --with-me --size 3` → 10000 total, three real entries.
* `mlp_run_list --query mcp_test --with-me` found my three v0.4-era
  test runs (Killed/Killed/Failed) by exact name.
* `mlp_job_aggr_info` returned the full report including the
  user-supplied `comment` field (= our `annotation`).
* `mlp_job_attempts` returned per-pod info incl. `containerId`,
  `hostName`, and `diagnostics` — useful for failure RCA.
* `mlp_log_files` returned an empty list for a finished job
  (expected — logs reaped) but the proxy endpoint is alive.

## Test infrastructure

`tests/test_mlp.py` mirrors the hope-side `fake_hope_backend` pattern
but for MLP. Tests:

* Stub `mlp_api.requests.Session` with a `_FakeMlpBackend` that
  records calls + replies via URL substring.
* Stub `mlp_api._exchange_token_blocking` with a fake token so tests
  don't need a real `~/.mlp-cli-token.json`.
* Reset `mlp_tools._invalidate_mlp_login_cache()` between tests.

## Server.py changes for tool registration

* Added `from . import mlp_api` and `from .tools import mlp as mlp_tools`.
* Registered MLP tools as a single block at the end of `TOOLS` and
  `TOOL_HANDLERS`.
* Updated `tests/test_server.py::test_registers_full_tool_surface` to
  use `>= set` containment instead of `==` so further waves don't
  break it; added a Wave-1 explicit checklist.

