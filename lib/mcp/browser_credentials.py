"""lib/mcp/browser_credentials.py — MCP credentials that copy the owner's browser session.

Problem: many MCP servers authenticate with a copy of the user's browser
session: one cookie's value (Overleaf ``overleaf_session2``, Bilibili
``SESSDATA``) or a Cookie header (``a1=…; web_session=…``). The copy expires,
or the site rotates the session, while the browser stays signed in; every
call then fails and agents ask the user to paste a fresh value from DevTools.

Decision: a *binding* records which browser cookies an env value copies:

* ``{'kind': 'cookie', 'name': N, 'url': U}`` — the value of the cookie N the
  browser sends to U;
* ``{'kind': 'cookie_header', 'names': [...], 'required': [...], 'url': U}`` —
  ``n1=v1; n2=v2`` composed, in the user's order, from the cookies the browser
  sends to U; a renewal needs every ``required`` (session) cookie.

Bindings have two sources. A curated catalog ``env_specs`` row may declare
``browser_cookie: {name, url, url_env}``; that works even when the stored copy
is already stale. Every other server *learns* its bindings: ``learn_bindings``
asks the owner's devices that declare ``cookie_match`` whether a stored
session-like value equals one of their cookies. The browser compares
HMAC-SHA-256 digests under a one-time key, so neither stored values nor
cookie values cross the bridge; only matching cookies' identity returns. A
learned binding is persisted in the row's ``browser_credentials`` together
with the device that answered and the value's fingerprint, and lapses when
the value is changed by any other path.

``refresh_from_browser`` reads the bound cookies from the binding's device
(else the owner's default device), saves changed values in one locked row
transaction, and audits ``mcp_credential_refreshed`` by key name only. The MCP
bridge decides when to call it (``lib/mcp/client/_bridge.py``). Values never
reach a model, a tool result or a log line.

Entry points: ``credential_bindings``, ``learn_bindings``,
``refresh_from_browser``, ``failure_notice``.
Dependencies: ``lib.mcp.registry`` (catalog), ``lib.mcp.config`` (row
transactions), ``lib.browser.queue`` and ``lib.browser.dispatch`` (owner device
routing; relayed from the execution host), ``lib.browser.access`` (the owner's
read-denied domains).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = ['RefreshOutcome', 'credential_bindings', 'failure_notice',
           'learn_bindings', 'refresh_from_browser']

# Same bound as the other server-side cookie readers
# (lib/browser/cookie_capture.py, lib.auth_sources.live_session_status).
_COOKIE_COMMAND_TIMEOUT_S = 10
# RFC 6265 §6.1: a user agent stores cookies of at least 4096 bytes.
_MAX_COOKIE_BYTES = 4096
# OWASP Session Management Cheat Sheet: a session identifier carries at least
# 64 bits of entropy. Shorter values (usernames, flags, regions) are never
# compared with the browser's cookies.
_MIN_SESSION_ENTROPY_BITS = 64
# RFC 6265 §4.1.1: a cookie value is printable US-ASCII except DQUOTE, comma,
# semicolon and backslash; a cookie name is an RFC 2616 token.
_COOKIE_OCTETS = frozenset(map(chr, range(0x21, 0x7f))) - set('",;\\')
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_UNSAFE_IN_ENV = ('\x00', '\r', '\n')


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of one ``refresh_from_browser`` call.

    ``reason``: ``unbound`` (no browser-held credential), ``no_browser`` (no
    device connected for the owner), ``no_session`` (the browser holds no
    usable session), ``current`` (the stored copy already equals it) or
    ``renewed`` (``changed`` keys now hold the browser's session).
    """
    row: dict[str, Any] | None
    changed: tuple[str, ...]
    reason: str


def _env(row) -> dict[str, Any]:
    env = row.get('env') if isinstance(row, dict) else None
    return env if isinstance(env, dict) else {}


def _stored_entries(row) -> dict[str, dict]:
    stored = row.get('browser_credentials') if isinstance(row, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {key: entry for key, entry in stored.items() if isinstance(entry, dict)}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _entropy_bits(value: str) -> float:
    alphabet = 0
    if any('0' <= ch <= '9' for ch in value):
        alphabet += 10
    if any('a' <= ch <= 'z' for ch in value):
        alphabet += 26
    if any('A' <= ch <= 'Z' for ch in value):
        alphabet += 26
    if any(not (ch.isascii() and ch.isalnum()) for ch in value):
        alphabet += 32  # printable ASCII punctuation
    return len(value) * math.log2(alphabet) if alphabet > 1 else 0.0


def _session_like(value) -> bool:
    """Could ``value`` be a session identifier stored in a cookie?"""
    return (isinstance(value, str)
            and 0 < len(value.encode('utf-8')) <= _MAX_COOKIE_BYTES
            and set(value) <= _COOKIE_OCTETS
            and '://' not in value and not value.startswith(('/', '~'))
            and _entropy_bits(value) >= _MIN_SESSION_ENTROPY_BITS)


def _header_pairs(value) -> list[tuple[str, str]]:
    """``[(name, value), ...]`` when ``value`` is a Cookie header, else []."""
    if not isinstance(value, str) or '=' not in value:
        return []
    pairs = []
    for part in value.split(';'):
        part = part.strip()
        if not part:
            continue
        name, sep, cookie_value = part.partition('=')
        name = name.strip()
        if not sep or not _COOKIE_NAME.fullmatch(name):
            return []
        pairs.append((name, cookie_value.strip()))
    return pairs


def _usable(value) -> bool:
    return (isinstance(value, str) and bool(value)
            and len(value.encode('utf-8')) <= _MAX_COOKIE_BYTES
            and not any(ch in value for ch in _UNSAFE_IN_ENV))


def _host(url: str) -> str:
    return (urlparse(url).hostname or '').lower()


def _same_site(left: str, right: str) -> bool:
    left, right = left.lstrip('.').lower(), right.lstrip('.').lower()
    return (bool(left) and bool(right) and (
        left == right or left.endswith('.' + right) or right.endswith('.' + left)))


def _normalized(entry: dict) -> dict | None:
    url = str(entry.get('url') or '').strip()
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    binding: dict[str, Any] = {'kind': entry.get('kind'), 'url': url}
    if entry.get('kind') == 'cookie':
        if not _COOKIE_NAME.fullmatch(str(entry.get('name') or '')):
            return None
        binding['name'] = entry['name']
    elif entry.get('kind') == 'cookie_header':
        names = [n for n in entry.get('names') or ()
                 if isinstance(n, str) and _COOKIE_NAME.fullmatch(n)]
        required = [n for n in entry.get('required') or () if n in names]
        if not required:
            return None
        binding.update(names=names, required=required)
    else:
        return None
    if isinstance(entry.get('client_id'), str) and entry['client_id']:
        binding['client_id'] = entry['client_id']
    return binding


def _declared(name: str, row) -> dict[str, dict]:
    from lib.mcp.registry import get_catalog_entry
    env = _env(row)
    declared = {}
    for spec in (get_catalog_entry(name) or {}).get('env_specs') or ():
        cookie = spec.get('browser_cookie')
        if not cookie:
            continue
        url_env = cookie.get('url_env')
        url = str((env.get(url_env) if url_env else '') or cookie['url']).strip()
        binding = _normalized({'kind': 'cookie', 'name': cookie['name'], 'url': url})
        if binding is not None:
            declared[spec['key']] = binding
    return declared


def _non_secret_keys(name: str) -> set[str]:
    from lib.mcp.registry import get_catalog_entry
    return {spec['key'] for spec in (get_catalog_entry(name) or {}).get('env_specs') or ()
            if spec.get('secret') is False or spec.get('type') == 'select'}


def credential_bindings(name: str, row) -> dict[str, dict]:
    """Env key → binding for every value of ``row`` that copies browser cookies.

    A catalog declaration wins over a learned binding; a learned binding
    counts only while its fingerprint still matches the stored value.
    """
    env = _env(row)
    stored = _stored_entries(row)
    bindings = {}
    for key, entry in stored.items():
        value = env.get(key)
        if isinstance(value, str) and entry.get('fingerprint') == _fingerprint(value):
            binding = _normalized(entry)
            if binding is not None:
                bindings[key] = binding
    for key, binding in _declared(name, row).items():
        device = stored.get(key, {}).get('client_id')
        bindings[key] = {**binding, **({'client_id': device} if device else {})}
    return bindings


def _devices(owner: str) -> list[dict]:
    from lib.browser.bridge_relay import RelayUnavailable
    from lib.browser.queue import get_connected_clients
    try:
        return list(get_connected_clients(owner_user_id=owner))
    except RelayUnavailable as exc:
        logger.info('[MCP] Browser bridge unreachable for credentials: %s', exc)
        return []


def _command(cmd_type: str, params: dict, *, owner: str, client_id: str):
    from lib.browser.queue import send_browser_command
    result, error = send_browser_command(
        cmd_type, params, timeout=_COOKIE_COMMAND_TIMEOUT_S,
        client_id=client_id, owner_user_id=owner)
    if error:
        logger.info('[MCP] Browser %s on %s failed: %s', cmd_type,
                    client_id[:12], str(error)[:160])
        return None
    return result


# ── Learning which values copy browser cookies ───────────

def _learning_candidates(name: str, row, skip: frozenset[str]) -> dict[str, str]:
    bound = credential_bindings(name, row)
    non_secret = _non_secret_keys(name)
    candidates = {}
    for key, value in _env(row).items():
        if (key in bound or key in non_secret or not isinstance(value, str)
                or _fingerprint(value) in skip):
            continue
        if _session_like(value) or any(
                _session_like(v) for _, v in _header_pairs(value)):
            candidates[key] = value
    return candidates


def _utf16_length(value: str) -> int:
    # The browser compares JavaScript string lengths (UTF-16 code units).
    return len(value.encode('utf-16-le')) // 2


def _match_request(candidates: dict[str, str]) -> tuple[dict, dict]:
    """The ``match_cookies`` params and candidate id → (env key, pair index)."""
    key = os.urandom(32)
    entries, layout = [], {}

    def _add(env_key, index, cookie_name, value):
        candidate_id = f'c{len(layout)}'
        layout[candidate_id] = (env_key, index)
        entry = {'id': candidate_id, 'length': _utf16_length(value),
                 'mac': hmac.new(key, value.encode('utf-8'), hashlib.sha256).hexdigest()}
        if cookie_name:
            entry['name'] = cookie_name
        entries.append(entry)

    for env_key, value in candidates.items():
        if _session_like(value):
            _add(env_key, None, '', value)
        for index, (cookie_name, cookie_value) in enumerate(_header_pairs(value)):
            if cookie_value:
                _add(env_key, index, cookie_name, cookie_value)
    return ({'key': base64.b64encode(key).decode('ascii'), 'candidates': entries},
            layout)


def _path_match(request_path: str, cookie_path: str) -> bool:
    # RFC 6265 §5.1.4.
    return (request_path == cookie_path or (
        request_path.startswith(cookie_path)
        and (cookie_path.endswith('/') or request_path[len(cookie_path)] == '/')))


def _sending_url(cookies: list[tuple]) -> str | None:
    """One URL to which the browser sends every cookie in ``cookies``."""
    host = max((domain.lstrip('.') for _, domain, _, _, _ in cookies), key=len)
    path = max((path for _, _, path, _, _ in cookies), key=len)
    for _, domain, cookie_path, host_only, _ in cookies:
        domain = domain.lstrip('.')
        if host_only and host != domain:
            return None
        if not (host == domain or host.endswith('.' + domain)):
            return None
        if not _path_match(path, cookie_path):
            return None
    return f'https://{host}{path if path.startswith("/") else "/"}'


def _bindings_from_matches(candidates, layout, matches, anchors) -> dict[str, dict]:
    """Bindings the browser's ``matches`` prove for ``candidates``.

    A value binds only to exactly one cookie. A header binds when at least
    one session-like pair matched and every matched cookie is sent to one
    URL. A cookie that scripts may read (no HttpOnly) binds only beside an
    HttpOnly session cookie of the same site, as a site's CSRF companion
    does; a lone script-readable match (a username or locale) is ignored.
    """
    found: dict[str, set] = {}
    for match in matches if isinstance(matches, list) else ():
        if not isinstance(match, dict) or match.get('id') not in layout:
            continue
        cookie = (str(match.get('name') or ''), str(match.get('domain') or ''),
                  str(match.get('path') or '/'), bool(match.get('hostOnly')),
                  bool(match.get('httpOnly')))
        if cookie[0] and cookie[1]:
            found.setdefault(match['id'], set()).add(cookie)
    unique = {cid: next(iter(cookies)) for cid, cookies in found.items()
              if len(cookies) == 1}
    learned = {}
    for env_key, value in candidates.items():
        pairs = _header_pairs(value)
        matched = [(index, unique[cid]) for cid, (key, index) in layout.items()
                   if key == env_key and index is not None and cid in unique]
        required = [pairs[index][0] for index, _ in matched
                    if _session_like(pairs[index][1])]
        url = _sending_url([cookie for _, cookie in matched]) if required else None
        if url is not None:
            learned[env_key] = {
                'kind': 'cookie_header', 'url': url,
                'names': list(dict.fromkeys(cookie_name for cookie_name, _ in pairs)),
                'required': list(dict.fromkeys(required))}
    singles = {key: unique[cid] for cid, (key, index) in layout.items()
               if index is None and cid in unique and key not in learned}
    anchors = list(anchors) + [_host(binding['url']) for binding in learned.values()]
    anchors += [domain for _, domain, _, _, http_only in singles.values() if http_only]
    for env_key, (cookie_name, domain, path, _, http_only) in singles.items():
        if http_only or any(_same_site(domain, anchor) for anchor in anchors):
            learned[env_key] = {'kind': 'cookie', 'name': cookie_name,
                                'url': f'https://{domain.lstrip(".")}{path or "/"}'}
    return learned


def learn_bindings(name: str, *, owner_user_id,
                   skip: frozenset[str] = frozenset()) -> frozenset[str]:
    """Learn and persist which stored values of ``name`` copy browser cookies.

    Values whose fingerprint is in ``skip`` are not asked about again.
    Returns the fingerprints a device answered for (empty when no device
    with ``cookie_match`` is connected, so the caller asks again later).
    """
    from lib.browser.dispatch import select_owner_client
    from lib.browser.protocol import BrowserCapability
    from lib.mcp.config import load_mcp_config, update_server
    row = load_mcp_config(user_id=owner_user_id).get(name)
    if not isinstance(row, dict) or not row.get('enabled', True):
        return frozenset()
    candidates = _learning_candidates(name, row, skip)
    if not candidates:
        return frozenset()
    owner = str(owner_user_id)
    connected = _devices(owner)
    capable = [str(device.get('client_id') or '') for device in connected
               if BrowserCapability.COOKIE_MATCH.value in (device.get('capabilities') or ())]
    if not capable:
        return frozenset()
    # Select among all devices, as browser tools do: selecting among the
    # capable ones only would move the owner's sticky device.
    first = select_owner_client(owner, connected)
    devices = sorted(capable, key=lambda client_id: client_id != first)
    anchors = [_host(binding['url']) for binding in credential_bindings(name, row).values()]
    request, layout = _match_request(candidates)
    learned, answered = {}, False
    for client_id in devices:
        result = _command('match_cookies', request, owner=owner, client_id=client_id)
        if not isinstance(result, dict):
            continue
        answered = True
        for env_key, binding in _bindings_from_matches(
                candidates, layout, result.get('matches'), anchors).items():
            learned.setdefault(env_key, {
                **binding, 'client_id': client_id,
                'fingerprint': _fingerprint(candidates[env_key])})
        if len(learned) == len(candidates):
            break
    if learned:
        def _bind(current):
            env, stored = _env(current), _stored_entries(current)
            fresh = {key: entry for key, entry in learned.items()
                     if isinstance(env.get(key), str)
                     and _fingerprint(env[key]) == entry['fingerprint']}
            if not fresh:
                return None
            current['browser_credentials'] = {**stored, **fresh}
            return current

        update_server(name, _bind, user_id=owner_user_id)
        logger.info('[MCP] %s: %s copy browser cookies (%s)', name,
                    ', '.join(sorted(learned)),
                    ', '.join(sorted({_host(b['url']) for b in learned.values()})))
    if not answered:
        return frozenset()
    return frozenset(_fingerprint(value) for value in candidates.values())


# ── Renewing from the browser ────────────────────────────

def _browser_value(binding: dict, cookies: list) -> str | None:
    if binding['kind'] == 'cookie':
        return next((row.get('value') for row in cookies
                     if row.get('name') == binding['name']
                     and _usable(row.get('value'))), None)
    first: dict[str, str] = {}
    for row in cookies:
        cookie_name = row.get('name')
        # Chrome lists the cookies for one URL in Cookie-header order.
        if cookie_name in binding['names'] and cookie_name not in first:
            first[cookie_name] = row.get('value')
    if not all(_usable(first.get(n)) for n in binding['required']):
        return None
    return '; '.join(f'{n}={first[n]}' for n in binding['names']
                     if _usable(first.get(n)))


def refresh_from_browser(name: str, *, owner_user_id) -> RefreshOutcome:
    """Copy the owner's current browser session into ``name``'s stored row."""
    from lib.browser.access import is_read_allowed
    from lib.browser.dispatch import select_owner_client
    from lib.mcp.config import load_mcp_config, update_server
    row = load_mcp_config(user_id=owner_user_id).get(name)
    if not isinstance(row, dict):
        return RefreshOutcome(None, (), 'unbound')
    bindings = credential_bindings(name, row)
    if not bindings:
        return RefreshOutcome(row, (), 'unbound')
    owner = str(owner_user_id)
    devices = _devices(owner)
    if not devices:
        return RefreshOutcome(row, (), 'no_browser')
    fresh, read_from = {}, {}
    for key, binding in bindings.items():
        if not is_read_allowed(owner, binding['url']):
            continue
        client_id = select_owner_client(owner, devices,
                                        preferred=binding.get('client_id', ''))
        params = {'url': binding['url']}
        if binding['kind'] == 'cookie':
            params['name'] = binding['name']
        cookies = _command('get_cookies', params, owner=owner, client_id=client_id)
        value = (_browser_value(binding, [c for c in cookies if isinstance(c, dict)])
                 if isinstance(cookies, list) else None)
        if value is not None:
            fresh[key], read_from[key] = value, client_id
    env, stored_before = _env(row), _stored_entries(row)
    changed = sorted(key for key, value in fresh.items() if env.get(key) != value)
    if not changed:
        return RefreshOutcome(row, (), 'current' if fresh else 'no_session')
    learned = {key for key in changed
               if 'fingerprint' in stored_before.get(key, {})}
    expected = {key: env.get(key) for key in changed}
    applied: list[str] = []

    def _renew(current):
        current_env, stored = _env(current), _stored_entries(current)
        # A key edited since it was read keeps the newer edit.
        keys = [key for key in changed if current_env.get(key) == expected[key]]
        if not keys:
            return None
        current['env'] = {**current_env, **{key: fresh[key] for key in keys}}
        for key in keys:
            entry = {**stored.get(key, {}), 'client_id': read_from[key]}
            if key in learned:
                entry['fingerprint'] = _fingerprint(fresh[key])
            stored[key] = entry
        current['browser_credentials'] = stored
        applied[:] = keys
        return current

    row = update_server(name, _renew, user_id=owner_user_id)
    if row is None:
        return RefreshOutcome(None, (), 'unbound')
    if not applied:
        return RefreshOutcome(row, (), 'current')
    audit_log('mcp_credential_refreshed', server=name, env_keys=applied,
              source='browser_cookie')
    logger.info('[MCP] Saved %s for %s from the owner\'s browser',
                ', '.join(applied), name)
    return RefreshOutcome(row, tuple(applied), 'renewed')


def failure_notice(name: str, row, *, renewed: bool, reason: str) -> str:
    """Model-facing guidance after ``name`` failed on a browser-held credential."""
    sites = sorted({_host(b['url']) for b in credential_bindings(name, row).values()})
    site = ', '.join(sites) or 'its site'
    if renewed:
        return (f"[Tofu] The {name} MCP server's credential copies the user's "
                f'browser session on {site}. Tofu replaced the expired copy with '
                "the browser's current session and reconnected; repeat the call.")
    if reason == 'no_browser':
        action = f'connect the Tofu browser extension and sign in to {site} there'
    else:
        action = f'sign in to {site} in the browser connected to Tofu'
    return (f"[Tofu] The {name} MCP server's credential copies the user's "
            f'browser session on {site}, and Tofu found no newer session there. '
            f'Ask the user to {action}; Tofu then copies the session itself. '
            'Do not ask the user for cookie values.')
