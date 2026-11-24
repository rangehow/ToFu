"""routes/api_v1/oauth.py — OAuth status / diagnostics surface.

Two routes:
  GET /api/v1/oauth/status — auth state for all providers (or one with
                              ``?provider=claude|codex``)
  GET /api/v1/oauth/test   — server-side reachability probe of OAuth
                              endpoints (admin-scoped)

The browser-redirect flows themselves stay at their legacy paths because
they mix GET form-redirects (geo-block fallback) and don't fit the v1
JSON contract:

  POST/GET /api/oauth/login    — kicks off PKCE + relay server
  POST/GET /api/oauth/callback — exchanges authorization code for tokens
  POST/GET /api/oauth/logout   — revokes stored tokens
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_forbidden, api_internal_error, api_ok, api_payload,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, async_parse_body, optional_str, parse_body, require_str

from .auth import human_ui_provenance, request_principal, require_auth, require_scope

logger = get_logger(__name__)

api_v1_oauth_bp = Blueprint('api_v1_oauth', __name__)


# provider → the API host its subscription traffic targets (egress probe key)
_PROVIDER_EGRESS_HOST = {
    'claude': 'api.anthropic.com',
    'codex': 'chatgpt.com',
}

# ``reconcile_oauth_providers`` is idempotent drift-repair that also runs at
# startup and on login/logout. Running it on EVERY status poll re-reads
# tokens, provisions/deprovisions, and builds a RouteCandidateCompiler per
# provider — on an IO/CPU-starved host that held the request thread 3-6 s
# (2026-09-24 SLOW log) for a read-only status UI. Throttle the reconcile to
# at most once per interval; the status READ itself stays fresh every call.
_OAUTH_RECONCILE_MIN_INTERVAL_S = 30.0
_oauth_reconcile_state = {'at': 0.0}


def _reconcile_oauth_throttled(owner_user_id: int) -> dict | None:
    """Return the post-repair provider statuses, or None when throttled."""
    import time as _time
    now = _time.monotonic()
    if now - _oauth_reconcile_state['at'] < _OAUTH_RECONCILE_MIN_INTERVAL_S:
        return None
    _oauth_reconcile_state['at'] = now
    from lib.oauth.outbound import reconcile_oauth_providers
    return reconcile_oauth_providers(owner_user_id=owner_user_id)


def _with_egress_state(status: dict, provider: str, user_id: str) -> dict:
    """Attach the desktop-egress state to one provider's status payload.

    NEVER probes inline (page-load path, design §6.2 A4) — egress_status
    reads the 300s probe cache only and fires a background warm-up.
    """
    host = _PROVIDER_EGRESS_HOST.get(provider)
    if not host:
        return status
    try:
        from lib.desktop.egress import egress_status
        status = dict(status)
        status['egress'] = egress_status(host, user_id=user_id)
    except Exception as e:
        logger.debug('[OAuth.v1] egress status failed for %s: %s', provider, e)
    return status


def _with_quota_state(status: dict, provider: str,
                      user_id: str = '') -> dict:
    """Attach passive quota plus the explicit earned-reset entitlement.

    This function never performs upstream I/O.  The reset owner returns its
    last account-scoped snapshot and starts one daemon refresh when stale —
    or when the quota snapshot is missing/stale, since the same wham ``/usage``
    GET also carries the structured ``rate_limit`` windows Codex ``/status``
    renders.
    """
    if provider != 'codex' or not status.get('authenticated'):
        return status
    status = dict(status)
    quota = None
    try:
        from lib.subscription_quota import latest_subscription_quota
        quota = latest_subscription_quota(
            provider, cache_key='oauth_codex')
        if quota:
            status['quota'] = quota
    except Exception as e:
        logger.debug('[OAuth.v1] quota status failed for %s: %s', provider, e)
    try:
        from lib.oauth.codex_usage import (
            CODEX_USAGE_QUOTA_FRESH_S, codex_usage_reset_status)
        quota_age = (quota or {}).get('age_seconds')
        status['reset_offer'] = codex_usage_reset_status(
            user_id=user_id,
            quota_refresh_due=(quota_age is None or int(quota_age)
                               > CODEX_USAGE_QUOTA_FRESH_S))
    except Exception as e:
        # Failure stays explicitly unknown; it must never be projected as zero.
        logger.debug('[OAuth.v1] reset-offer status failed for %s: %s',
                     provider, e)
        status['reset_offer'] = {
            'state': 'unknown', 'available_count': None,
            'source': 'codex_usage_api', 'captured_at': None,
            'stale': False, 'refreshing': False,
            'reason': 'status_unavailable',
        }
    return status


@api_v1_oauth_bp.route('/api/v1/oauth/status', methods=['GET'])
@require_auth
@api_meta(
    summary='OAuth login status (per provider)',
    description=(
        'Returns ``{<provider>: {logged_in, expires_at, ...}}`` for all '
        'providers, or just the requested one when ``?provider=`` is set. '
        '"Provider" here refers to the *upstream subscription provider* '
        '(Claude Pro, ChatGPT Codex, Kimi Code), NOT the v1 LLM provider '
        'config. '
        'Authenticated Codex status also carries a non-blocking '
        '``reset_offer`` state (available, none, or unknown); unknown is '
        'never inferred as zero.'
    ),
    tags=['capabilities'],
)
def oauth_status():
    owner_user_id = request_principal().require_owner(
        context='OAuth status')
    owner_scope = str(owner_user_id)
    try:
        from lib.oauth.manager import get_all_oauth_status, get_oauth_status
        from lib.oauth.token_store import token_snapshot
        # One credential read per provider serves reconciliation, the cards
        # and the reset offer of this response.
        with token_snapshot():
            reconciled = _reconcile_oauth_throttled(owner_user_id)
            provider = request.args.get('provider', '')
            if provider:
                from lib.oauth.outbound import OAUTH_PROVIDERS
                if provider not in OAUTH_PROVIDERS:
                    return api_bad_request('Invalid provider', field='provider')
                return api_ok(_with_quota_state(_with_egress_state(
                    get_oauth_status(
                        provider, owner_user_id=owner_user_id,
                        provider_status=(reconciled or {}).get(provider)),
                    provider, owner_scope),
                    provider, owner_scope))
            all_status = get_all_oauth_status(
                owner_user_id=owner_user_id, provider_statuses=reconciled)
            return api_ok({p: _with_quota_state(
                               _with_egress_state(s, p, owner_scope),
                               p, owner_scope)
                           for p, s in all_status.items()})
    except Exception as e:
        logger.error('[OAuth.v1] status check failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.status')


@api_v1_oauth_bp.route('/api/v1/oauth/test', methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='Test server-side OAuth endpoint reachability (admin)',
    description=(
        'Probes the four OAuth endpoints (``claude_token``, '
        '``claude_auth``, ``codex_token``, ``codex_auth``) from the '
        'server and returns reachability + geo-block detection. '
        'Mainly useful for diagnosing the China geo-block / corporate '
        'proxy situation. Admin-scoped because the response leaks '
        'partial response bodies.'
    ),
    tags=['capabilities'], scope='admin',
)
def oauth_test():
    import requests

    from lib.oauth.http import http_get

    endpoints = {
        'claude_token': 'https://platform.claude.com/v1/oauth/token',
        'claude_auth':  'https://claude.ai/',
        'codex_token':  'https://auth.openai.com/oauth/token',
        'codex_auth':   'https://auth.openai.com/',
    }

    results: dict[str, dict] = {}
    for name, url in endpoints.items():
        try:
            r = http_get(url, timeout=8,
                        allow_redirects=False)
            blocked = (
                (r.status_code == 302 and 'unavailable-in-region'
                 in (r.headers.get('Location', '')))
                or 'unsupported_country_region_territory' in r.text[:500]
            )
            results[name] = {
                'url': url,
                'status': r.status_code,
                'reachable': not blocked,
                'blocked': blocked,
                'detail': (r.headers.get('Location', '')[:200]
                           if r.status_code == 302 else r.text[:200]),
            }
        except requests.RequestException as e:
            logger.debug('[OAuth.v1] reachability probe %s (%s) failed: %s',
                         name, url, e)
            results[name] = {
                'url': url, 'status': 0, 'reachable': False,
                'blocked': True, 'detail': str(e)[:200],
            }
    return api_ok(results)


@api_v1_oauth_bp.route('/api/v1/oauth/device-login', methods=['POST', 'GET'])
@require_auth
@api_meta(
    summary='Start a device-authorization login flow (Codex, Kimi Code)',
    description=(
        'Mints a user code (OpenAI deviceauth for ``codex``; the RFC 8628 '
        'Kimi Code device grant for ``kimi``) and starts the '
        'server-side poll thread. This path never touches the localhost:1455 '
        'redirect: the user '
        'enters the displayed code at the verification URL in ANY browser. '
        'It requires one working server proxy/direct/desktop-agent route; a '
        'transport outage is returned as 503 with a machine-readable '
        '``reason`` (egress_blocked = every server route is policy/geo-rejected, '
        'egress_network / egress_recovery_pending = transient outage) plus the '
        'actionable ``error`` text, so the UI can show precise guidance and '
        'only fall back to the browser callback-copy flow when recovery is '
        'possible. '
        'Completion is observed via the regular /api/v1/oauth/status '
        'projection (device.user_code + status transitions).'
    ),
    tags=['capabilities'],
)
def oauth_device_login():
    if request.method == 'GET':
        if set(request.args) - {'provider'} or len(request.args.getlist('provider')) > 1:
            raise BadRequest('Unexpected or duplicate OAuth query parameter')
        provider = request.args.get('provider', '')
    else:
        if request.args:
            raise BadRequest('Send OAuth POST parameters in the JSON body')
        body = parse_body(force=True, strict=True, exact=('provider',))
        provider = optional_str(body, 'provider', default='', max_len=16)
    if provider not in ('codex', 'kimi'):
        return api_bad_request(
            'Device login is only available for provider=codex or kimi',
            field='provider')

    owner_user_id = request_principal().require_owner(
        context='OAuth device flow')
    try:
        from lib.oauth.manager import start_device_flow
        logger.info('[OAuth.v1] %s /api/v1/oauth/device-login from %s',
                    request.method, request.remote_addr)
        result = start_device_flow(
            provider, owner_user_id=owner_user_id)
        if 'error' in result:
            # A transport outage is not a malformed login request. Preserve
            # the upstream status_code=0 detail and use 503; the passthrough
            # ``reason`` lets clients classify the outage without string
            # matching (a persistent egress block must not auto-fall back to
            # the browser flow, whose token exchange rides the same routes).
            http_status = 503 if result.get('status_code') in (0, '0') else 400
            return api_payload(result, http_status)
        return api_ok(result)
    except Exception as e:
        logger.error('[OAuth.v1] device-login failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.device_login')

_CONSUME_ERROR_STATUS = {
    'not_authenticated': 401,
    'account_identity_unavailable': 401,
    'account_changed': 409,
    'none_available': 409,
    'nothing_to_reset': 409,
    'already_redeemed': 409,
    'upstream_rejected': 502,
    'upstream_unavailable': 502,
    'unresolved': 502,
}


@api_v1_oauth_bp.route('/api/v1/oauth/reset-offer/consume', methods=['POST'])
@require_auth
@api_meta(
    summary='Redeem one earned Codex usage-limit reset',
    description=(
        "Consumes one banked rate-limit reset credit for the caller's "
        'authenticated Codex account. This is the explicitly confirmed, '
        'idempotent command: the server selects the soonest-expiring '
        'available credit, persists its ``redeem_request_id`` before the '
        'upstream spend, and reuses that key on a retry of an ambiguous '
        'outcome so a credit can never be spent twice by accident. The '
        'response carries the force-refreshed ``reset_offer`` projection. '
        'Failure codes: ``none_available``/``nothing_to_reset``/'
        '``already_redeemed`` (409, nothing was spent), ``unresolved``/'
        '``upstream_*`` (502 — for ``unresolved`` the credit may have been '
        'spent; the refreshed projection is the reconciliation).'
    ),
    tags=['capabilities'],
)
def oauth_reset_offer_consume():
    parse_body()
    owner_user_id = request_principal().require_owner(
        context='Codex reset consume')
    try:
        from lib.oauth.codex_usage import consume_codex_usage_reset
        result = consume_codex_usage_reset(user_id=str(owner_user_id))
    except Exception as e:
        logger.error('[OAuth.v1] reset-offer consume failed: %s', e,
                     exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.reset_offer_consume')
    if result.get('ok'):
        from lib.log import audit_log
        audit_log(
            'oauth_codex_reset_consumed',
            owner_user_id=str(owner_user_id),
            windows_reset=result.get('windows_reset'),
        )
        return api_ok(result)
    error = str(result.get('error') or 'consume_failed')
    return api_payload(result, _CONSUME_ERROR_STATUS.get(error, 500))

# ── Egress agent pin selector (multi-agent deployments) ─────────────


@api_v1_oauth_bp.route('/api/v1/oauth/egress-agent', methods=['GET'])
@require_auth
@api_meta(
    summary='Egress agent pin state + online agents',
    description=(
        'Returns ``{pinned, agents}`` — the caller\'s pinned desktop egress '
        'agent and the online agents (with capabilities) eligible for '
        'subscription egress routing.'
    ),
    tags=['capabilities'],
)
def oauth_egress_agent_get():
    from lib.desktop.egress import egress_agent_selection

    owner_user_id = request_principal().require_owner(
        context='desktop egress selection')
    pinned, eligible_agents = egress_agent_selection(owner_user_id)
    agents = [
        {'agent_id': agent.get('agent_id'), 'name': agent.get('name'),
         'platform': agent.get('platform'),
         'capabilities': agent.get('capabilities') or {},
         'online': True}
        for agent in eligible_agents
    ]
    return api_ok({'pinned': pinned, 'agents': agents})


@api_v1_oauth_bp.route('/api/v1/oauth/egress-agent', methods=['POST'])
@require_auth
@api_meta(
    summary='Pin the desktop egress agent for this user',
    description=(
        'Body ``{agent_id}`` — pins the caller\'s subscription egress to one '
        'online egress-capable agent owned by the caller. Empty agent_id '
        'clears the pin. The owner-scoped preference is persisted by the '
        'Storage Sidecar.'
    ),
    tags=['capabilities'],
)
def oauth_egress_agent_set():
    # Parse before domain work. BadRequest is a client 400 at the shared HTTP
    # boundary and must never reach storage or clear an existing selection.
    body = parse_body(strict=True)
    agent_id = optional_str(
        body, 'agent_id', default='', max_len=128)
    from lib.desktop.egress import UnknownEgressAgent, pin_egress_agent
    from lib.log import audit_log

    owner_user_id = request_principal().require_owner(
        context='desktop egress selection')
    try:
        pinned = pin_egress_agent(owner_user_id, agent_id)
    except UnknownEgressAgent as exc:
        return api_bad_request(str(exc), field='agent_id')
    audit_log(
        'oauth_egress_agent_pinned',
        owner_user_id=owner_user_id,
        agent_id=pinned or '(cleared)',
    )
    return api_ok({'pinned': pinned})


_ANTIGRAVITY_PROMPT_MAX_CHARS = 16384  # Subscription Login text prompt contract.


@api_v1_oauth_bp.route('/api/v1/oauth/antigravity', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Check the local Antigravity CLI session', tags=['capabilities'], scope='admin',
          description='Checks ACP session readiness without sending a prompt or returning credentials.')
async def oauth_antigravity_status():
    from lib.oauth.antigravity import antigravity_for_owner

    return api_ok(await antigravity_for_owner(request_principal()))


@api_v1_oauth_bp.route('/api/v1/oauth/antigravity', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Send a text prompt to the local Antigravity agent', tags=['capabilities'], scope='admin',
    description='Private personal Tofu and same-origin UI session only. Google owns usage and credentials.',
    request_body={'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'additionalProperties': False, 'required': ['prompt'],
        'properties': {'prompt': {'type': 'string', 'minLength': 1,
                                  'maxLength': _ANTIGRAVITY_PROMPT_MAX_CHARS}},
    }}}},
)
async def oauth_antigravity_prompt():
    from lib.oauth.antigravity import antigravity_for_owner

    try:
        human_ui_provenance()
    except PermissionError:
        return api_forbidden('Send Antigravity prompts from the signed-in Tofu browser.')
    body = await async_parse_body(strict=True, exact=('prompt',))
    prompt = require_str(body, 'prompt', strip=False, max_len=_ANTIGRAVITY_PROMPT_MAX_CHARS)
    if not prompt.strip():
        return api_bad_request('prompt is required', field='prompt')
    return api_ok(await antigravity_for_owner(request_principal(), prompt=prompt))


@api_v1_oauth_bp.route('/api/v1/oauth/antigravity/login', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Start personal Google sign-in through the local Antigravity CLI',
    tags=['capabilities'], scope='admin', request_body=False,
    description='Signed-in same-origin Tofu UI only. The CLI opens its own browser and verifies readiness.',
)
async def oauth_antigravity_login():
    from lib.oauth.antigravity import antigravity_for_owner

    try:
        human_ui_provenance()
    except PermissionError:
        return api_forbidden('Start Antigravity sign-in from the signed-in Tofu browser.')
    return api_ok(await antigravity_for_owner(request_principal(), login=True))


@api_v1_oauth_bp.route('/api/v1/oauth/antigravity/install', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Install the official Antigravity CLI on the Tofu host',
    tags=['capabilities'], scope='admin', request_body=False,
    description=('Same-origin personal-owner UI only. Downloads the Google release manifest '
                 'and native binary through subscription network policy, verifies SHA512, '
                 'and atomically installs into ~/.local/bin without running a shell.'),
)
async def oauth_antigravity_install():
    from lib.oauth.subscription_cli_install import install_subscription_cli

    try:
        human_ui_provenance()
    except PermissionError:
        return api_forbidden('Install the Antigravity CLI from the signed-in Tofu browser.')
    if await request.get_data():
        return api_bad_request('This operation takes no request body.')
    return api_ok(await install_subscription_cli('antigravity', request_principal()))


from .oauth_accounts import register_accounts

register_accounts(api_v1_oauth_bp)

__all__ = ['api_v1_oauth_bp']
