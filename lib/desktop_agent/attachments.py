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
    fingerprint,
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


def _pair_body(secret: str, node_id: str, name: str, kind: str = 'computer',
               previous_node_id: str = '') -> dict:
    body = {'code': secret, 'node_id': node_id, 'name': name[:80],
            'platform': sys.platform[:40], 'kind': kind}
    if previous_node_id and previous_node_id != node_id:
        body['previous_node_id'] = previous_node_id
    return body


def _pair_outcome(resp):
    """``(PairResponse, '')`` for a redeemed code, else ``(None, reason)``."""
    if resp.status_code == 201:
        try:
            payload = resp.json() or {}
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            payload = payload['data']
        token = str(payload.get('token') or '') if isinstance(payload, dict) else ''
        return (payload, '') if token else (None, 'not_tofu')
    if resp.status_code == 409:
        return None, 'invalid_code'
    if resp.status_code == 429:
        return None, 'rate_limited'
    return None, 'http_%d' % resp.status_code


def _redeem(url: str, body: dict, timeout: float):
    """One direct pair exchange. Returns the PairResponse, or a failure reason."""
    import requests
    from lib.http_transport.client import http_post
    path = node_contract()['x-transport']['pair_path']
    try:
        resp = http_post(url + path, timeout=timeout, allow_redirects=False,
                         auth=lambda request: request,
                         json=body)
    except requests.RequestException as exc:
        logger.debug('[Agent] pair exchange via %s failed: %s', url, exc)
        return None, 'unreachable'
    return _pair_outcome(resp)


def _redeem_via_browser(link, body: dict, log, stop=None):
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
                             body, {},
                             timeout=max(1.0, deadline - time.monotonic()))
        if resp is None:
            return None, 'browser_unavailable'
        return _pair_outcome(resp)
    finally:
        relay.close()


def join(text: str, *, node_id: str, name: str = '', timeout: float = 8.0,
         log=lambda _msg: None, via_browser: bool = True, stop=None,
         kind: str = 'computer') -> dict:
    """Join a server from one pasted input; returns the saved attachment.

    A pairing code is one-shot: a route that answered ``invalid_code`` was
    Tofu refusing the code itself, so later routes are not tried with it.
    The new node waits for the Home owner's confirmation; the attachment
    keeps the fingerprint both ends show. A host that replaced an earlier
    node (renamed, container recreated) names it, so the Home shows a
    re-pair of a known device.
    """
    from lib.desktop_agent.config import clear_predecessor, node_identity, save_attachment
    identity = node_identity()
    previous = identity['predecessor'] if identity['node_id'] == node_id else None
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
        body = _pair_body(parsed.secret, node_id, name, kind, previous or '')
        reasons, paired = [], {}
        for route in parsed.candidates:
            paired, reason = _redeem(route, body, timeout)
            if paired:
                url, secret = route, paired['token']
                break
            log('Pairing via %s failed: %s' % (route, reason))
            reasons.append(reason)
            if reason in ('invalid_code', 'rate_limited'):
                break
        if not url and via_browser and reasons[-1] not in ('invalid_code', 'rate_limited'):
            paired, reason = _redeem_via_browser(parsed, body, log, stop)
            if paired:
                url, secret = parsed.server_url, paired['token']
            else:
                reasons.append(reason)
        if not url:
            raise JoinError(reasons[-1] if reasons else 'unreachable')
        candidates = [c for c in parsed.candidates if c != url]
    record = {'id': attachment_id(url), 'url': url, 'secret': secret,
              'candidates': candidates, 'label': label,
              'paired_at': time.time(), 'state': 'active'}
    if not isinstance(parsed, LegacyConnectLine):
        record['fingerprint'] = fingerprint(node_id, str(paired.get('id') or ''))
        if paired.get('fingerprint') and paired['fingerprint'] != record['fingerprint']:
            raise JoinError('fingerprint_mismatch')
    save_attachment(record)
    if previous and not isinstance(parsed, LegacyConnectLine):
        clear_predecessor()
    if record.get('fingerprint'):
        log('Joined %s as node %s; waiting for the owner to confirm it on that '
            'Tofu (fingerprint %s)' % (label, name, record['fingerprint']))
    else:
        log('Joined %s as node %s' % (label, name))
    return record


__all__ = ['JoinError', 'join']
