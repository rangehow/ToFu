---
name: chatui-security-audit-findings
description: Tofu security FIXED 2026-06-09: BYO SSRF egress guard, abs-path sandbox, open-mode loopback gate, const-time compare; test bugs (Quart default_config + http_get mock target) also fixed
enabled: true
tags: [security, audit, authentication, injection]
created: 2026-03-17T09:22:58Z
updated: 2026-06-09T03:04:28Z
---

## Resolved (verified 2026-06-09)
- Feishu APP_SECRET env-only fail-closed; no server-side shell=True; injection hunt clean (parameterized SQL); upload XSS safe (SVG blocked + Pillow re-encode); update_key admin-escalation guard sound.

### FIXED 2026-06-09 — the three criticals + 2 mediums
1. **BYO SSRF** → new `lib/byo_egress.py` (`validate_egress_url`/`is_egress_allowed`/`EgressDenied`). Always denies link-local (169.254/16 metadata), multicast, reserved, unspecified, bad scheme, DNS-failure. Loopback + RFC1918 allowed BY DEFAULT (self-hosted LLM case) — lock down with `TOFU_BYO_BLOCK_LOOPBACK=1` / `TOFU_BYO_BLOCK_PRIVATE=1`; bypass with `TOFU_BYO_ALLOW_HOSTS=h1,h2`. Resolves ALL IPs (getaddrinfo) to defeat DNS rebinding. Wired: registration `byo_providers._validate_base_url`; use-time `discovery.discover_models` + `_probe_balance_url`; **proxy choke point `llm_dispatch/ephemeral._normalise_base_url`** (covers native chat + agent/run + both compat adapters + inline blocks, at USE time → beats post-registration rebind). Tests: `tests/test_byo_egress.py` (10).
2. **Abs-path sandbox** → new `lib/project_mod/abs_path_guard.py`. ContextVar `_restrict_abs_paths` (default False = local/CLI/cookie unaffected). `enforce_abs_read` (read_tools._read_absolute_file) + `enforce_abs_write` (write_tools._resolve_write_path, BEFORE auto-register) require realpath-containment in a registered root (symlink escape denied). Activated per-task in `lib/tasks_pkg/handlers/project.py` via `set_restricted(task_is_remote(task))` in try/finally. `task_is_remote` = `_via_agent_run`/`_compat_openai`/`_compat_anthropic`/non-empty `_api_key_id`. Tests: `tests/test_abs_path_guard.py` (6).
3. **Open-mode loopback gate** → `routes/api_v1/auth.py`: synthetic local-admin only when `_remote_is_loopback()` (request.remote_addr, NOT spoofable XFF; Quart `<local>` sentinel = loopback) OR `TOFU_OPEN_MODE_ALLOW_REMOTE=1`. Non-loopback open-mode falls through to credential gate.
4. **Const-time compare**: `api_keys.validate_token` `hmac.compare_digest`; `auth._legacy_tunnel_token_passes` all 3 paths.

### Test failures — ALL FIXED 2026-06-09 (were real bugs, NOT environmental)
- **Quart `KeyError: PROVIDE_AUTOMATIC_OPTIONS`** in `test_api_v1_agent_run.py` + `test_api_v1_byo_surface_polish.py`: bare `Quart(__name__)` in setUpClass; this Quart's `default_config` lacks the key that Flask sansio `add_url_rule` reads. CANONICAL FIX (already in `server.py:478-480` + `test_api_response.py:_make_app_ctx`): `Quart.default_config = {**Quart.default_config, 'PROVIDE_AUTOMATIC_OPTIONS': True}` before construction. Added to each file's `_install_shim()`.
- **`test_image_fetch_ssrf.py` 4 failures**: tests mocked `requests.get`, but `_safe_image_fetch` does a function-local `from lib.http_client import http_get` → `requests.request`. Mock the real seam `patch('lib.http_client.http_get')`. (For function-local imports, patch the SOURCE module, not `routes.upload.http_get` which doesn't exist as an attr.)
- Guardrail: whole touched-area suite green — `pytest tests/test_image_fetch_ssrf.py tests/test_api_v1_agent_run.py tests/test_api_v1_byo_surface_polish.py tests/test_byo_egress.py tests/test_abs_path_guard.py tests/test_byo_providers.py tests/test_api_keys.py tests/test_auth_mode.py tests/test_e2e_headless_api.py tests/test_rate_limit_api.py` = 143 passed.

### Test-fixture note
`tests/test_byo_providers.py` uses `http://127.0.0.1:PORT` (not `http://h:PORT`) — egress guard denies unresolvable hosts (DNS-fail=deny). Use loopback/raw-IP in BYO fixtures, or set `TOFU_BYO_ALLOW_HOSTS`. Real private IPs (10.x, 127.0.0.1:1) are fine by default.

## Still OPEN (lower priority)
- `?token=` not stripped in open mode (URL/referer/log leak).
- `_PUBLIC_PREFIXES` startswith on un-normalized request.path (normalize + reject `..`//`//`).
- Cookie `secure=request.is_secure` → honor X-Forwarded-Proto behind TLS proxy.

