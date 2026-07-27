"""routes/api_v1/desktop.py — Desktop-agent status probe.

Single read-only route. Reports whether the desktop agent is currently
connected (last poll within 15 s) plus how many commands are pending in
the queue. Used by the in-app debug panel to render a presence dot.

The actual long-poll RPC channel (``POST /api/desktop/poll``) stays at
its original path under :mod:`routes.desktop` because it's a Bridge-Secret-
authenticated long-poll between server and agent, not a JSON REST verb.
"""

from __future__ import annotations

import sys
import time

from flask import Blueprint, jsonify

from lib.log import audit_log, get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_desktop_bp = Blueprint('api_v1_desktop', __name__)


def _setup_state(connected: bool) -> str:
    """Which ONE install instruction the UI should show.

    The setup surface must present a single next action, never a menu of
    every possible path — so the CHOICE is made here, where the facts
    actually live, rather than guessed in JS.

    * ``connected``  — an agent polled within the window; nothing to install.
    * ``tray``       — this server process is the packaged desktop app
      (``sys.frozen``, set by PyInstaller and re-exec'd by
      desktop/launcher.py with TOFU_RUN_SERVER=1). The agent runs
      IN-PROCESS via the tray's "Enable Computer Control" item, so the
      instruction is one click and no token is involved. This supersedes
      the old ``python -m lib.desktop_agent`` flow (launcher.py docstring:
      "Replaces the old 'install a second program and run python -m
      lib.desktop_agent' flow").
    * ``remote``     — anything else: the user's machine is not this
      machine, so they need the desktop app plus a bridge token.

    ``sys.frozen`` is the load-bearing signal, NOT the peer address:
    :func:`routes.api_v1.auth._remote_is_loopback` documents that a
    same-host reverse proxy makes every public request present as
    loopback, so it can never distinguish "the user is on this box" from
    "the user is behind nginx". A frozen process, by contrast, IS the
    tray app by construction. Loopback is consulted only to keep a
    source-run local dev server (frozen=False, peer=loopback) out of the
    ``remote`` bucket — it would otherwise be told to install a second
    copy of an app it is already running.
    """
    if connected:
        return 'connected'
    if getattr(sys, 'frozen', False):
        return 'tray'
    from .auth import _remote_is_loopback
    if _remote_is_loopback():
        return 'local_source'
    return 'remote'


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Desktop-agent connection status',
    description=(
        'Returns ``{connected, last_poll, pending_commands, setup_state}`` '
        'so the UI can render a presence indicator AND the single '
        'appropriate install instruction. Connection is defined as a '
        'poll within the last 15 s. ``setup_state`` is one of '
        '``connected`` / ``tray`` / ``local_source`` / ``remote``.'
    ),
    tags=['capabilities'],
)
async def desktop_status():
    from lib.desktop import (
        is_desktop_agent_connected,
        last_poll_time,
        list_agents,
        pending_commands_count,
    )
    from .auth import current_auth
    _auth = current_auth()
    _uid = (_auth.user_id
            if _auth and getattr(_auth, 'user_id', '') else None)
    connected = is_desktop_agent_connected()
    _last = last_poll_time()
    return jsonify({
        'connected': connected,
        'last_poll': _last,
        'secondsAgo': (round(time.time() - _last, 1) if _last else None),
        'pending_commands': pending_commands_count(),
        'agents': list_agents(user_id=_uid),
        'setup_state': _setup_state(connected),
    })


@api_v1_desktop_bp.route('/api/v1/desktop/streams/<cmd_id>', methods=['GET'])
@require_auth
async def desktop_stream(cmd_id):
    """Reassembled live output of one streamed command (RWA P2/P4b-2b).

    Debug / inspector surface — the chat UI consumes the same frames via
    the ``tool_progress`` SSE channel instead. ``cmd_id`` is an unguessable
    uuid minted per command; entries expire with the command TTL.
    """
    from lib.desktop import get_command_stream
    stream = get_command_stream(cmd_id)
    if stream is None:
        return jsonify({'error': 'not_found',
                        'message': 'unknown or expired command stream'}), 404
    return jsonify(stream)


# ── RWA P4b:Devices 页(拍板 5A)—— agents + bridge tokens 一屏 ──

_BRIDGE_SCOPE = 'agents:bridge'


@api_v1_desktop_bp.route('/api/v1/desktop/devices', methods=['GET'])
@require_auth
async def desktop_devices():
    """Devices page payload: the caller's agents + their bridge tokens.

    Tokens are listed METADATA-ONLY (id/name/created/scopes) — the secret
    is only ever returned once, by POST /api/v1/desktop/token.
    """
    from lib.api_keys import list_keys
    from lib.desktop import list_agents
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    tokens = [
        {'id': k.get('id'), 'name': k.get('name'),
         'created_at': k.get('created_at'),
         'scopes': sorted(k.get('scopes') or [])}
        for k in list_keys()
        if _BRIDGE_SCOPE in (k.get('scopes') or [])
        and (k.get('user_id') or '') == uid
    ]
    return jsonify({
        'agents': list_agents(user_id=uid),
        'tokens': tokens,
    })


@api_v1_desktop_bp.route('/api/v1/desktop/token', methods=['POST'])
@require_auth
async def desktop_token_mint():
    """Mint a per-user bridge token (scope agents:bridge).

    The raw secret is returned EXACTLY ONCE in this response; afterwards
    only metadata is listable. Bound to the caller's user_id so poll auth
    scopes every command to them (RWA P4a).
    """
    from lib.api_keys import create_key
    from lib.request_parser import async_parse_body, optional_str
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    body = await async_parse_body()
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'desktop-bridge'
    row, token = create_key(name, scopes=[_BRIDGE_SCOPE], user_id=uid)
    audit_log('desktop_bridge_token_minted', key_id=row.get('id'),
              name=name, user_id=uid)
    return jsonify({'id': row.get('id'), 'name': name, 'token': token,
                    'scopes': [_BRIDGE_SCOPE]}), 201


@api_v1_desktop_bp.route('/api/v1/desktop/token/<key_id>', methods=['DELETE'])
@require_auth
async def desktop_token_revoke(key_id):
    """Revoke one of the caller's OWN bridge tokens.

    Deliberately NOT the admin-scoped /api/v1/keys DELETE: a tenant may
    revoke only their own agents:bridge keys, nothing wider.
    """
    from lib.api_keys import get_key_by_id, revoke_key
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    row = get_key_by_id(key_id)
    if (not row or _BRIDGE_SCOPE not in (row.get('scopes') or [])
            or (row.get('user_id') or '') != uid):
        return jsonify({'error': 'not_found',
                        'message': 'bridge token not found'}), 404
    revoke_key(key_id)
    audit_log('desktop_bridge_token_revoked', key_id=key_id, user_id=uid)
    return jsonify({'revoked': key_id})


__all__ = ['api_v1_desktop_bp']
