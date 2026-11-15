"""Node-side joining: redeem a pairing link and persist the attachment.

Responsibility: turn one join-dialog input (a pairing link, or a legacy
``<url> <token>`` connect line) into an ``Attachment``
(contracts/desktop_node_v1.schema.json) in the agent config. The link's
routes are tried in order (``via`` routes first, the browser-visible base
last); the first route whose ``POST /api/desktop/pair`` succeeds becomes the
attachment URL and the others are kept as repair candidates. When no route
answers directly (an SSO edge that only the owner's signed-in browser passes),
the node opens its loopback browser relay for the link's origin and the open
Local Control page redeems the secret on its behalf (``relay_pair`` in the
frontend desktop endpoint registry); later polls ride the same relay.

Entry points: :func:`join`, :class:`JoinError`.
Dependencies: lib.desktop_wire.node_protocol, lib.desktop_agent.config,
lib.http_transport.client (shared network authority).
"""
from __future__ import annotations

import socket
import sys
import time

from lib.desktop_wire.node_protocol import (
    LegacyConnectLine,
    PairingInputError,
    attachment_id,
    node_contract,
    parse_join_input,
)
from lib.log import get_logger

logger = get_logger(__name__)


class JoinError(Exception):
    """Coded join failure; ``code`` maps to UI prose, ``str()`` holds no secret."""

    def __init__(self, code: str, detail: str = ''):
        self.code = code
        self.detail = detail
        super().__init__('join:%s%s' % (code, (':' + detail) if detail else ''))


def _pair_body(secret: str, node_id: str, name: str) -> dict:
    return {'code': secret, 'node_id': node_id, 'name': name[:80],
            'platform': sys.platform[:40]}


def _pair_outcome(resp):
    """``(token, '')`` for a redeemed code, else ``(None, reason)``."""
    if resp.status_code == 201:
        try:
            token = str((resp.json() or {}).get('token') or '')
        except ValueError:
            token = ''
        return (token, '') if token else (None, 'not_tofu')
    if resp.status_code == 409:
        return None, 'invalid_code'
    if resp.status_code == 429:
        return None, 'rate_limited'
    return None, 'http_%d' % resp.status_code


def _redeem(url: str, secret: str, node_id: str, name: str, timeout: float):
    """One direct pair exchange. Returns the credential, or a failure reason."""
    import requests
    from lib.http_transport.client import http_post
    path = node_contract()['x-transport']['pair_path']
    try:
        resp = http_post(url + path, timeout=timeout, allow_redirects=False,
                         auth=lambda request: request,
                         json=_pair_body(secret, node_id, name))
    except requests.RequestException as exc:
        logger.debug('[Agent] pair exchange via %s failed: %s', url, exc)
        return None, 'unreachable'
    return _pair_outcome(resp)


def _redeem_via_browser(link, node_id: str, name: str, log, stop=None):
    """Redeem through the signed-in page's relay; waits while the link lives.

    The wait is the pairing lifetime (``x-pairing.ttl_seconds``): after it the
    secret is dead anyway. ``stop`` (an Event) ends the wait early.
    """
    from lib.desktop_agent._browser_relay import BrowserRelay
    relay = BrowserRelay(lambda: list(link.candidates), log=log)
    if not relay.start():
        return None, 'unreachable'
    deadline = time.monotonic() + node_contract()['x-pairing']['ttl_seconds']
    log('Waiting for the open Tofu page to carry the pairing (relay port %s)'
        % relay.port)
    try:
        while not relay.browser_available():
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                return None, 'browser_unavailable'
            time.sleep(0.5)
        resp = relay.request(link.server_url + node_contract()['x-transport']['pair_path'],
                             _pair_body(link.secret, node_id, name), {},
                             timeout=max(1.0, deadline - time.monotonic()))
        if resp is None:
            return None, 'browser_unavailable'
        return _pair_outcome(resp)
    finally:
        relay.close()


def join(text: str, *, node_id: str, name: str = '', timeout: float = 8.0,
         log=lambda _msg: None, via_browser: bool = True, stop=None) -> dict:
    """Join a server from one pasted input; returns the saved attachment.

    A pairing code is one-shot: a route that answered ``invalid_code`` was
    Tofu refusing the code itself, so later routes are not tried with it.
    """
    from lib.desktop_agent.config import save_attachment
    try:
        parsed = parse_join_input(text)
    except PairingInputError as exc:
        raise JoinError(exc.code) from None
    name = (name or socket.gethostname() or 'desktop').strip()
    if isinstance(parsed, LegacyConnectLine):
        url, secret, candidates = parsed.server_url, parsed.secret, []
        label = url
    else:
        url, secret, label = '', '', parsed.server_url
        reasons = []
        for route in parsed.candidates:
            token, reason = _redeem(route, parsed.secret, node_id, name, timeout)
            if token:
                url, secret = route, token
                break
            log('Pairing via %s failed: %s' % (route, reason))
            reasons.append(reason)
            if reason in ('invalid_code', 'rate_limited'):
                break
        if not url and via_browser and reasons[-1] not in ('invalid_code', 'rate_limited'):
            token, reason = _redeem_via_browser(parsed, node_id, name, log, stop)
            if token:
                url, secret = parsed.server_url, token
            else:
                reasons.append(reason)
        if not url:
            raise JoinError(reasons[-1] if reasons else 'unreachable')
        candidates = [c for c in parsed.candidates if c != url]
    record = {'id': attachment_id(url), 'url': url, 'secret': secret,
              'candidates': candidates, 'label': label,
              'paired_at': time.time(), 'state': 'active'}
    save_attachment(record)
    log('Joined %s as node %s' % (label, name))
    return record


__all__ = ['JoinError', 'join']
