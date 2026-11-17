"""Tofu node control protocol v1: tiers, pairing links, attachment migration.

Responsibility: the one stdlib-only owner of ``contracts/desktop_node_v1.schema.json``
policy shared by the server bridge (``lib/desktop``), the HTTP routes and the
node runtime (``lib/desktop_agent``). It holds no credentials, sockets or state.

Entry points: :func:`required_tier`, :func:`granted_tiers`,
:func:`tier_refusal`, :func:`build_pairing_link`, :func:`parse_pairing_link`,
:func:`parse_join_input`, :func:`migrate_attachments`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


@lru_cache(maxsize=1)
def node_contract() -> dict:
    return json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/desktop_node_v1.schema.json').read_text())


def tier_order() -> tuple[str, ...]:
    return tuple(node_contract()['x-permission-tiers']['order'])


def default_tiers() -> dict:
    return dict(node_contract()['x-permission-tiers']['default'])


def required_tier(cmd_type: str, params=None) -> str | None:
    """The tier a command needs, ``'control'`` for cancellations, None if unknown."""
    contract = node_contract()
    rule = contract['x-command-param-tiers'].get(cmd_type)
    if rule and isinstance(params, dict):
        tier = rule['values'].get(str(params.get(rule['param']) or ''))
        if tier:
            return tier
    return contract['x-command-tiers'].get(cmd_type)


def granted_tiers(capabilities) -> dict:
    """Tiers a node declared in its poll frame; undeclared tiers stay denied.

    ``capabilities`` is the bridge frame object (``write``/``exec``/... truthy
    values). Read is the floor every node grants.
    """
    caps = capabilities if isinstance(capabilities, dict) else {}
    tiers = default_tiers()
    for tier in tier_order():
        if tier != 'read':
            tiers[tier] = bool(caps.get(tier))
    return tiers


def tiers_from_permissions(permissions) -> dict:
    """Node-local permission dict (``allow_*``) projected onto tier names."""
    perms = permissions if isinstance(permissions, dict) else {}
    keys = node_contract()['x-permission-tiers']['permission_keys']
    tiers = default_tiers()
    for tier, key in keys.items():
        tiers[tier] = bool(perms.get(key))
    return tiers


def tier_refusal(cmd_type: str, params, tiers: dict, *, node_label: str = 'this device') -> str | None:
    """Why a command may not run under ``tiers``; None when it may.

    Unknown commands are refused: a tier map entry is the admission ticket.
    """
    tier = required_tier(cmd_type, params)
    if tier is None:
        return f'Unknown command: {cmd_type}'
    if tier == 'control' or tiers.get(tier):
        return None
    return (f'{cmd_type} needs the {tier} permission, which {node_label} has not '
            f'granted. The owner can enable it in the Tofu desktop app under '
            f'Permissions.')


# ── Pairing links ────────────────────────────────────────────────────

class PairingInputError(ValueError):
    """Coded refusal of a join-dialog input; ``str()`` never contains a secret."""

    def __init__(self, code: str):
        self.code = code
        super().__init__('pairing_input:%s' % code)


@dataclass(frozen=True)
class PairingLink:
    server_url: str
    secret: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class LegacyConnectLine:
    server_url: str
    secret: str


def _http_base(value: str) -> str:
    """Normalize a server base: scheme+host+path, no query/fragment/trailing '/'."""
    try:
        parts = urlsplit((value or '').strip())
    except ValueError as exc:
        raise PairingInputError('bad_url') from exc
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise PairingInputError('bad_url')
    if parts.username is not None or parts.password is not None:
        raise PairingInputError('bad_url')
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip('/'), '', ''))


def build_pairing_link(server_url: str, secret: str, via=()) -> str:
    """``<base>/#tofu-pair=<secret>[&via=<base>]*`` for one minted secret."""
    pairing = node_contract()['x-pairing']
    base = _http_base(server_url)
    fragment = '%s=%s' % (pairing['fragment_key'], quote(secret, safe=''))
    for url in via:
        normalized = _http_base(url)
        if normalized != base:
            fragment += '&%s=%s' % (pairing['via_key'], quote(normalized, safe=''))
    return base + '/#' + fragment


def parse_pairing_link(text: str) -> PairingLink:
    """Parse a pasted/scanned pairing link.

    Tolerates surrounding whitespace, a missing ``/`` before ``#``, and an SPA
    route left in front of the pairing key (``#/chat&tofu-pair=…``). The query
    string and everything after ``#`` never become part of the server base.
    """
    pairing = node_contract()['x-pairing']
    raw = (text or '').strip()
    if '#' not in raw:
        raise PairingInputError('not_a_pairing_link')
    head, fragment = raw.split('#', 1)
    fields = parse_qsl(fragment.replace('?', '&').lstrip('/'), keep_blank_values=True)
    secrets_ = [v for k, v in fields if k.rsplit('/', 1)[-1] == pairing['fragment_key']]
    if len(secrets_) != 1:
        raise PairingInputError('not_a_pairing_link')
    secret = secrets_[0].strip()
    if not 6 <= len(secret) <= 64 or not all(
            c.isalnum() or c in '-_' for c in secret):
        raise PairingInputError('bad_secret')
    base = _http_base(head)
    candidates: list[str] = []
    for key, value in fields:
        if key == pairing['via_key']:
            url = _http_base(value)
            if url not in candidates:
                candidates.append(url)
    if base not in candidates:
        candidates.append(base)
    return PairingLink(server_url=base, secret=secret, candidates=tuple(candidates[:8]))


def fingerprint(node_id: str, credential_id: str) -> str:
    """The short code both ends show before the owner confirms a node.

    Derivation: contracts/desktop_node_registry_v1.schema.json x-identity.fingerprint.
    """
    import hashlib
    digest = hashlib.sha256(
        ('tofu-node-fingerprint/v1\0%s\0%s' % (node_id, credential_id)).encode('utf-8')
    ).hexdigest().upper()
    return '%s-%s' % (digest[:4], digest[4:8])


def headless_join_command(link: str) -> str:
    """The one-line headless join for a pairing link (x-pairing.headless_command)."""
    import shlex
    return 'python -m lib.desktop_agent --join %s' % shlex.quote(link)


def parse_join_input(text: str):
    """One join-dialog input.

    A pairing link; ``<url> <6-digit code>`` (x-pairing.code_join: the code
    form of a pairing, tried only at that URL); or a legacy ``<url> <token>``
    connect line.
    """
    raw = (text or '').strip()
    if '#' in raw and node_contract()['x-pairing']['fragment_key'] + '=' in raw:
        return parse_pairing_link(raw)
    parts = raw.split()
    if len(parts) == 2:
        base, secret = _http_base(parts[0]), parts[1].strip()
        digits = node_contract()['x-pairing']['short_code_digits']
        if len(secret) == digits and secret.isdigit():
            return PairingLink(server_url=base, secret=secret, candidates=(base,))
        return LegacyConnectLine(server_url=base, secret=secret)
    if len(parts) < 2:
        raise PairingInputError('missing_parts')
    raise PairingInputError('too_many_parts')


# ── Node-side attachment migration ───────────────────────────────────

def attachment_id(url: str) -> str:
    import hashlib
    return hashlib.sha256(_http_base(url).encode()).hexdigest()[:12]


def migrate_attachments(cfg: dict) -> bool:
    """Fold a legacy single ``remote_server`` into the ``attachments`` list.

    Idempotent; returns whether ``cfg`` changed. The legacy key is cleared
    after its credential moves, so there is exactly one authority afterwards.
    """
    keys = node_contract()['x-migration']
    legacy = cfg.get(keys['legacy_remote_server_key'])
    attachments = cfg.get(keys['attachments_key'])
    changed = False
    if not isinstance(attachments, list):
        attachments = []
        changed = True
    if isinstance(legacy, dict) and (legacy.get('url') or '').strip() and (legacy.get('secret') or '').strip():
        try:
            url = _http_base(legacy['url'])
        except PairingInputError:
            url = ''
        if url and not any(a.get('url') == url for a in attachments if isinstance(a, dict)):
            candidates = [c for c in (cfg.get('attach_candidates') or [])
                          if isinstance(c, str)]
            attachments.append({'id': attachment_id(url), 'url': url,
                                'secret': legacy['secret'].strip(),
                                'candidates': candidates, 'label': url,
                                'state': 'active'})
        cfg[keys['legacy_remote_server_key']] = {}
        changed = True
    if changed:
        cfg[keys['attachments_key']] = attachments
    return changed


__all__ = [
    'LegacyConnectLine', 'PairingInputError', 'PairingLink', 'attachment_id',
    'build_pairing_link', 'default_tiers', 'fingerprint', 'granted_tiers',
    'headless_join_command',
    'migrate_attachments',
    'node_contract', 'parse_join_input', 'parse_pairing_link', 'required_tier',
    'tier_order', 'tier_refusal', 'tiers_from_permissions',
]
