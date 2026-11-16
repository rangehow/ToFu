"""lib/mcp/browser_credentials.py — MCP credentials held by the owner's browser.

Problem: a stdio MCP server such as Overleaf authenticates with a copy of the
user's browser session cookie (``OVERLEAF_SESSION`` is ``overleaf_session2``).
The copy expires while the browser keeps a live session, and the only remedy
was a human pasting a fresh value from DevTools into Settings → MCP.

Decision: a curated catalog ``env_specs`` row may declare the browser cookie
that backs its value::

    'browser_cookie': {'name': 'overleaf_session2',
                       'url': 'https://www.overleaf.com',
                       'url_env': 'OVERLEAF_BASE_URL'}

``read_browser_env`` reads exactly that cookie, for the URL the server talks to
(the server's ``url_env`` value when set, else ``url``), from the owner's most
recently polling Tofu extension. The bridge asks only for a credential its
probe has proven expired and persists the answer into the owner's MCP
configuration (``MCPBridge._stored_server_config``). Values never reach a
model, a tool result or a log line; logs name servers, env keys and cookies.

Entry point: ``read_browser_env(name, srv_cfg, *, owner_user_id)``.
Dependencies: ``lib.mcp.registry`` (catalog), ``lib.browser.queue`` (owner
device routing; relayed from the execution host), ``lib.browser.access`` (the
owner's read-denied domains).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['read_browser_env']

# Same bound as the other server-side get_cookies readers
# (lib/browser/cookie_capture.py, lib.auth_sources.live_session_status).
_GET_COOKIES_TIMEOUT_S = 10
# RFC 6265 §6.1: a user agent stores cookies of at least 4096 bytes.
_MAX_COOKIE_BYTES = 4096
_UNSAFE_IN_ENV = ('\x00', '\r', '\n')


def _cookie_sources(name: str, srv_cfg: dict[str, Any]) -> list[dict[str, str]]:
    from lib.mcp.registry import get_catalog_entry
    env = srv_cfg.get('env') if isinstance(srv_cfg.get('env'), dict) else {}
    sources = []
    for spec in (get_catalog_entry(name) or {}).get('env_specs') or ():
        cookie = spec.get('browser_cookie')
        if not cookie:
            continue
        url_env = cookie.get('url_env')
        url = str((env.get(url_env) if url_env else '') or cookie['url']).strip()
        parsed = urlparse(url)
        if parsed.scheme in ('http', 'https') and parsed.hostname:
            sources.append({'env_key': spec['key'], 'url': url,
                            'cookie': cookie['name']})
    return sources


def _latest_client(owner: str) -> str:
    from lib.browser.bridge_relay import RelayUnavailable
    from lib.browser.queue import get_connected_clients
    try:
        clients = get_connected_clients(owner_user_id=owner)
    except RelayUnavailable as exc:
        logger.info('[MCP] Browser bridge unreachable for credential read: %s', exc)
        return ''
    if not clients:
        return ''
    return str(max(clients, key=lambda row: row.get('last_poll', 0))
               .get('client_id') or '')


def _usable(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)
            and len(value.encode('utf-8')) <= _MAX_COOKIE_BYTES
            and not any(ch in value for ch in _UNSAFE_IN_ENV))


def read_browser_env(name: str, srv_cfg: dict[str, Any], *,
                     owner_user_id) -> dict[str, str]:
    """Current browser values for ``name``'s cookie-backed env keys.

    Returns only keys whose cookie the owner's browser holds now. No source,
    no connected device, a read-denied domain, a failed command or an unusable
    value contributes nothing.
    """
    sources = _cookie_sources(name, srv_cfg)
    if not sources:
        return {}
    owner = str(owner_user_id)
    client_id = _latest_client(owner)
    if not client_id:
        return {}
    from lib.browser.access import is_read_allowed
    from lib.browser.queue import send_browser_command
    values: dict[str, str] = {}
    for source in sources:
        if not is_read_allowed(owner, source['url']):
            continue
        result, error = send_browser_command(
            'get_cookies', {'url': source['url'], 'name': source['cookie']},
            timeout=_GET_COOKIES_TIMEOUT_S, client_id=client_id,
            owner_user_id=owner)
        if error or not isinstance(result, list):
            logger.info('[MCP] %s: browser cookie %s unavailable: %s', name,
                        source['cookie'], str(error or 'non-list result')[:160])
            continue
        value = next((row.get('value') for row in result
                      if isinstance(row, dict)
                      and row.get('name') == source['cookie']
                      and _usable(row.get('value'))), None)
        if value is not None:
            values[source['env_key']] = value
    return values
