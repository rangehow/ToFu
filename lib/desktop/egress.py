"""lib/desktop/egress.py — 订阅流量的桌面代理出口路由层（S2）。

Routes OAuth token exchange/refresh (and, from S3, LLM streams) through
the user's desktop agent when the SERVER's own egress is geo-blocked or
network-dead — the failure chain measured 2026-07-31 (direct DNS dead,
corporate proxy 403 ``Request not allowed`` on all five endpoints).

Decision flow (:func:`route_request`):

  1. probe the target host WITHOUT auth (per-host 300s cache) —
     ``ok`` (401/400/404/405/200 = app layer reached) → caller goes direct;
  2. ``geo_blocked`` (403) or ``network_fail`` → pick an online
     egress-capable agent in the caller's tenant (legacy ``''`` user = any
     agent); none → :class:`EgressUnavailable` with actionable guidance.

Everything here is transport-agnostic: callers get an
:class:`EgressResponse` shaped like ``requests.Response`` so the OAuth
modules barely notice the detour. The domain whitelist is enforced BOTH
here (before enqueueing) and in the agent executor (before opening the
socket) — defense in depth, exact host match only.

Design: docs/DESKTOP_EGRESS_DESIGN.md §6.1.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

from lib.json_store import read_json
from lib.log import get_logger
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)

__all__ = [
    'ALLOWED_EGRESS_HOSTS',
    'EgressUnavailable',
    'EgressResponse',
    'host_allowed',
    'route_request',
    'egress_http',
]

#: Exact-match egress whitelist (§7.1). NO suffix matching —
#: ``api.anthropic.com.evil.com`` must never pass.
ALLOWED_EGRESS_HOSTS = frozenset({
    'api.anthropic.com',
    'console.anthropic.com',
    'claude.ai',
    'auth.openai.com',
    'auth0.openai.com',
    'chatgpt.com',
    'api.openai.com',
})

#: Per-host direct-reachability verdicts ('ok' / 'geo_blocked' /
#: 'network_fail'), 300s TTL — probing on every exchange would add seconds.
_probe_cache = TTLCache(ttl=300, max_size=32)

#: egress_http bridge-command TTL (design §4.3: token calls are short).
_EGRESS_HTTP_TTL_S = 120


class EgressUnavailable(RuntimeError):
    """No usable path to the provider: direct is blocked AND no suitable
    desktop agent is online. The message always carries user-actionable
    guidance (start an agent / pick one in Settings)."""


class EgressResponse:
    """``requests.Response``-shaped result for the egress path."""

    def __init__(self, status: int, headers: dict, content: bytes,
                 elapsed_ms: int = 0):
        self.status_code = status
        self.headers = headers or {}
        self.content = content or b''
        self.elapsed_ms = elapsed_ms

    @property
    def text(self) -> str:
        return self.content.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)


def host_allowed(url: str) -> bool:
    """Exact-host whitelist check (server side — the agent re-checks)."""
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception as e:
        logger.debug('[Egress] url parse failed for %r: %s', url, e)
        return False
    return host in ALLOWED_EGRESS_HOSTS


def _probe_host(url: str) -> str:
    """Classify direct reachability of the URL's host.

    POST the real endpoint WITHOUT auth: a 401/400/404/405 proves the
    application layer answered (network path OK — 'ok'); 403 is the
    geo-block signature; an exception is a network failure. Cached per host.
    """
    host = (urlparse(url).hostname or '').lower()
    cached = _probe_cache.get(host)
    if cached:
        return cached
    verdict = 'network_fail'
    try:
        from lib.http_client import http_post
        resp = http_post(url, json={}, timeout=5)
        verdict = 'geo_blocked' if resp.status_code == 403 else 'ok'
    except Exception as e:
        logger.debug('[Egress] direct probe of %s failed: %s', host, e)
    _probe_cache.set(host, verdict)
    logger.info('[Egress] probe %s → %s', host, verdict)
    return verdict


def _online_egress_agents(user_id: str = '') -> list:
    """Online agents advertising the egress capability, tenant-scoped.

    Legacy single-user deployments (``user_id=''``) see every egress agent
    (bridge v1/legacy semantics); a real tenant only sees its own.
    """
    from lib.desktop import online_agents
    out = []
    for a in online_agents():
        caps = a.get('capabilities') or {}
        if not caps.get('egress'):
            continue
        if user_id and (a.get('user_id') or '') != user_id:
            continue
        out.append(a)
    return out


def _pinned_agent(user_id: str) -> str:
    """The user's pinned egress agent id from
    ``data/config/oauth_egress_agents.json`` (Settings → OAuth, multi-agent
    deployments). '' when unset."""
    try:
        from lib.config_dir import config_path
        data = read_json(config_path('oauth_egress_agents.json'), default={}) or {}
        return str(data.get(user_id or '', '') or '')
    except Exception as e:
        logger.debug('[Egress] pinned-agent read failed: %s', e)
        return ''


def route_request(url: str, *, user_id: str = '') -> str:
    """Decide how a request to *url* should leave this server.

    Returns ``'direct'`` when the server's own egress reaches the provider,
    else the agent_id to route through.

    Raises:
        EgressUnavailable: direct is blocked AND no suitable agent is
            online (or several are and none is pinned).
    """
    if _probe_host(url) == 'ok':
        return 'direct'
    agents = _online_egress_agents(user_id)
    if not agents:
        raise EgressUnavailable(
            'server egress to this provider is blocked and no egress-capable '
            'desktop agent is online — start the desktop agent with '
            '--allow-egress on a machine whose network can reach the provider')
    if len(agents) == 1:
        return agents[0]['agent_id']
    pinned = _pinned_agent(user_id)
    if pinned and any(a['agent_id'] == pinned for a in agents):
        return pinned
    raise EgressUnavailable(
        f'{len(agents)} egress-capable agents are online; pin one in Settings '
        '(oauth_egress_agent_id) instead of letting the server guess')


def egress_http(url: str, *, method: str = 'POST', headers: dict = None,
                body: bytes = b'', timeout: float = 30,
                user_id: str = '') -> EgressResponse:
    """Execute one HTTP request through the caller's desktop agent.

    Whitelist → agent selection → bridge command (TTL 120s) → adapt the
    agent result into :class:`EgressResponse`.

    Raises:
        EgressUnavailable: host not whitelisted, no agent, agent offline /
            timed out, or the agent hit a network error.
    """
    if not host_allowed(url):
        raise EgressUnavailable(
            f'egress host not in whitelist: {urlparse(url).hostname or url!r}')
    from lib.desktop import send_desktop_command
    agent_id = route_request(url, user_id=user_id)  # raises when unavailable
    params = {
        'url': url,
        'method': (method or 'POST').upper(),
        'headers': dict(headers or {}),
        'body_b64': base64.b64encode(body or b'').decode('ascii'),
        'timeout_ms': int(min(max(timeout, 1), 60) * 1000),
        'proxy_mode': 'env',
    }
    logger.info('[Egress] routing %s %s via agent %s (user=%s)',
                params['method'], urlparse(url).hostname, agent_id[:8],
                user_id or '(legacy)')
    result, error = send_desktop_command(
        'egress_http', params,
        timeout=min(timeout + 15, _EGRESS_HTTP_TTL_S),
        target_agent_id=agent_id, user_id=user_id,
        ttl=_EGRESS_HTTP_TTL_S)
    if error or result is None:
        raise EgressUnavailable(
            f'desktop agent failed to execute egress request: {error or "no result"}')
    status = int(result.get('status') or 0)
    if status == 0:
        raise EgressUnavailable(
            f'agent network error: {result.get("error") or "unknown"}')
    try:
        content = base64.b64decode(result.get('body_b64') or '')
    except Exception as e:
        raise EgressUnavailable(f'agent returned undecodable body: {e}') from e
    return EgressResponse(
        status=status,
        headers=result.get('headers') or {},
        content=content,
        elapsed_ms=int(result.get('elapsed_ms') or 0),
    )
