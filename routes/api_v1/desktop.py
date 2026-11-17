"""routes/api_v1/desktop.py — node status, pairing and distribution surface.

Every Tofu desktop install is a node (contracts/desktop_node_v1.schema.json,
docs/modules/remote_execution.md "Nodes and pairing"). The panel mints a
one-time pairing link; the desktop app redeems it through ``POST
/api/desktop/pair`` and from then on dials out to ``POST /api/desktop/poll``.
These routes list the caller's nodes, mint/revoke their credentials and serve
the single desktop installer.

The long-poll channel (``POST /api/desktop/poll``) stays in
:mod:`routes.desktop` because it is a credential-authenticated device
transport, not a JSON REST verb.
"""

from __future__ import annotations

import os
import threading
import time

from quart import Blueprint, current_app

from lib.api_v4 import raw_success_response
from lib.api_response import (
    api_bad_request, api_conflict, api_created, api_error, api_not_found, api_ok,
    api_payload,
)
from lib.log import LOG_DIR, audit_log, get_logger
from lib.logging_pkg.log_policy import stream_backup_count, stream_max_bytes
from lib.logging_pkg.log_record_values import text_value, sanitize_value
from lib.logging_pkg.log_retention import append_bytes_locked, copytruncate_if_oversize
from lib.openapi import media_responses, api_meta
from lib.request_parser import BadRequest, async_parse_body, optional_str
from lib.storage.errors import StorageError

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_desktop_bp = Blueprint('api_v1_desktop', __name__)


_DESKTOP_BUILD_OS_CHOICES = ('linux', 'windows')
_DESKTOP_BUILD_KIND_CHOICES = ('full',)


def _desktop_build_parameters(body: dict) -> tuple[str, str]:
    """Validate one expensive build request before it starts background work.

    Defaults are useful for an empty POST, but applying them to misspelled or
    malformed fields is unsafe: ``windwos`` previously launched a Linux build
    and an unknown ``kind`` launched the much larger full application.
    """
    os_key = optional_str(
        body, 'os', default='linux', max_len=16).lower() or 'linux'
    if os_key not in _DESKTOP_BUILD_OS_CHOICES:
        raise BadRequest(
            'os must be one of: ' + ', '.join(_DESKTOP_BUILD_OS_CHOICES),
            field='os',
        )
    if os_key == 'linux':
        return os_key, 'full'

    kind = optional_str(
        body, 'kind', default='full', max_len=16).lower() or 'full'
    if kind not in _DESKTOP_BUILD_KIND_CHOICES:
        raise BadRequest(
            'kind must be one of: ' + ', '.join(_DESKTOP_BUILD_KIND_CHOICES),
            field='kind',
        )
    return os_key, kind


def _setup_state(connected: bool) -> str:
    """The one next action the Local Control panel shows.

    ``connected`` — one of the caller's nodes polled within the window;
    ``pair`` — none did: show the pairing link. There is no per-network
    guess: a node always dials out to the server it joined (LAN, ssh tunnel
    or reverse proxy alike), so the server learns about it by observing its
    poll, never by classifying the browser's peer address (an ssh -L tunnel
    or same-host proxy makes every peer look like loopback).
    """
    return 'connected' if connected else 'pair'


# ── Platform/release knowledge: extracted to lib/desktop_dist/platforms ──
# (2026-07, ). The route previously OWNED these helpers;
# the background mirror (lib/desktop_dist/mirror.py) would have been a
# second copy of the same rules. Re-exported here so existing callers and
# guard suites that import them from the route see no drift.
from lib.desktop_dist.platforms import (  # noqa: F401
    _PLATFORM_ASSETS_CACHE,
    _RELEASE_ASSET_CACHE,
    _assets_from_checksums,
    _desktop_download_url,
    _detect_arch,
    _detect_os,
    _latest_release_assets,
    _match_platform_assets,
    _platform_assets,
    _update_repo,
)
from lib.desktop_dist import mirror as _dist_mirror
from lib.desktop_dist import store as _dist_store


def _request_platform_downloads(arch_override: str = '',
                                kind: str = 'full') -> list[dict]:
    """Per-platform direct links for the CURRENT request's visitor.

    ── Zero network in the request path ──
    This used to resolve against ``api.github.com`` SYNCHRONOUSLY (TTL-cached,
    up to a 6 s timeout) inside an async route: every cache expiry stalled the
    event loop, which is the measured reason the Local Control modal's desktop
    row "always takes much longer". The answer now comes from the LOCAL
    artifact store (lib/desktop_dist): the background mirror keeps the
    published installers on this server's disk, so the client's download
    itself no longer depends on its route to the public GitHub network either.

    When the store cannot serve this platform yet (first boot, refresh in
    flight), the row is omitted and the mirror is kicked — the releases-page
    escape hatch stays, and the modal's 3 s poll pops the direct link in once
    the file lands. URLs are ABSOLUTE, built from the request's own host — an
    address the user demonstrably reaches (see _agent_server_url). Under a
    path-prefixed cloud-IDE proxy (…/proxy/<port>/) the host alone is NOT
    enough — the proxy strips the prefix before forwarding, so the backend
    structurally cannot see it, and the click dies on the gateway's default
    route without ever reaching Tofu. The client therefore re-bases the
    canonical ``/api/...`` tail onto its live ``BASE_PATH`` before rendering
    (local-control.js ``_lcResolveDlUrl`` — the same seam as pdf_viewer.js
    ``_resolvePaperPdfUrl``).

    ``arch_override`` is the architecture the CLIENT resolved for itself via
    ``navigator.userAgentData.getHighEntropyValues(['architecture'])`` — the
    only practical source on macOS, where the UA always says Intel.
    """
    from urllib.parse import quote

    from quart import request
    try:
        ua = request.user_agent.string or ''
    except Exception as e:
        logger.debug('[Desktop] user-agent parse failed: %s', e)
        ua = ''
    hint = (arch_override or '').strip() \
        or request.headers.get('Sec-CH-UA-Arch', '')
    os_key = _detect_os(ua)
    if not os_key:
        return []
    arch = _detect_arch(ua, hint)
    rows = _dist_store.find_for_platform(os_key, arch, kind=kind)
    # Kick the mirror whether or not the store served: an empty store needs
    # filling, a stale one needs refreshing. Non-blocking and single-flight;
    # a refresh this visitor starts fetches their installer first.
    _dist_mirror.ensure_fresh(prefer=(os_key, arch))
    import os as _os
    # Opt-in autobuild: a Linux visitor with no locally-BUILT artifact can
    # kick a native build (this server's own platform is the only one it can
    # truly build). Off by default — a build is minutes of CPU, so it happens
    # only where the operator asked for it, never implicitly for everyone.
    if (kind == 'full' and os_key == 'linux'
            and not any(e.get('source') == 'built' for e in rows)):
        if _os.environ.get('TOFU_DESKTOP_DIST_AUTOBUILD') == '1':
            from lib.desktop_dist import builder as _dist_builder
            if not _dist_builder.is_running():
                _dist_builder.start(reason='autobuild')
    # Same opt-in for Windows: no built installer → kick the Wine-toolchain
    # build (payload cached per (git_sha, deps), then the NSIS wrapper).
    # macOS never gets one — the documented permanent boundary.
    if (os_key == 'windows'
            and not any(e.get('source') == 'built' for e in rows)):
        if _os.environ.get('TOFU_DESKTOP_DIST_AUTOBUILD') == '1':
            from lib.desktop_dist import winbuilder as _win_builder
            if not _win_builder.is_running():
                _win_builder.start_installer(reason='autobuild',
                                             target=kind)
    base = (request.host_url or '').rstrip('/')
    # The URL is followed by the browser directly (an anchor href, not a
    # transport call), so it must land on a LIVE mount: a request that
    # arrived on the v4 projection gets a v4 download path — the v1 major
    # 426s once the release latch flips (see lib/api_v4).
    from lib.api_v4 import V1_PATH_PREFIX, V4_PATH_PREFIX, is_api_v4_path
    api_prefix = (
        V4_PATH_PREFIX if is_api_v4_path(request.path) else V1_PATH_PREFIX)
    out = []
    for e in rows:
        name = e.get('filename') or ''
        if not name:
            continue
        out.append({
            'os': e.get('os'),
            'arch': e.get('arch'),
            'label': e.get('label'),
            'filename': name,
            'url': base + api_prefix + '/desktop/download/' + quote(name),
            'hosted': 'server',
            'version': e.get('version') or '',
            'older_than_server': _dist_store.older_than_server(
                e.get('version')),
            'size': e.get('size') or 0,
            'source': e.get('source') or 'mirrored',
            'kind': e.get('kind') or 'full',
        })
    return out


def _with_drift(agents):
    """Flag agents whose build differs from this server's (owner amendment ②).

    The command protocol evolves WITH the server — a release-line agent
    against a HEAD server can silently mis-dispatch. ``outdated`` is True
    only when BOTH versions are known and differ: a legacy agent without
    the frame field is 'unknown', not 'outdated' (never cry wolf on the
    devices page).
    """
    try:
        from lib.version import __version__ as sv
        sv = (sv or '').strip()
    except Exception as e:
        logger.debug('[Desktop] server version read failed: %s', e)
        sv = ''
    out = []
    for a in agents or []:
        a = dict(a)
        av = str(a.get('version') or '').strip()
        a['outdated'] = bool(sv and av and av != sv)
        out.append(a)
    return out


def _agent_server_url() -> str:
    """The base URL a remote agent should point itself at.

    Taken from the REQUEST the browser just made, because that is by
    construction an address the user can actually reach this server on —
    a configured BIND_HOST would frequently be ``0.0.0.0`` (meaningless to
    type) and an internal hostname may not resolve from the user's machine.
    """
    from quart import request
    return (request.host_url or '').rstrip('/')


def _host_reachability(host: str) -> str:
    """Whether an AGENT can use the address this request arrived on.

    The installer attachment includes an address derived from the request —
    one the BROWSER demonstrably reaches. Under an SSO-fronted gateway
    (cloud-IDE preview proxies, corporate IdP) the browser sails through on cookies
    while the agent — which carries only a bridge token — is bounced at
    the edge and never reaches Tofu. Measured 2026-08-03 (owner live):
    the codelab preview proxy answered every /api/* with
    ``401 {"error":"Unauthorized"}`` while access.log showed ZERO agent
    polls — a proxy-derived URL made the agent poll a wall silently. The panel
    warns when the address it is about to embed is of that kind. 'public' is
    a heuristic (a public host CAN be fine when nothing intercepts it), so the
    panel warns without blocking the download.
    """
    import ipaddress
    h = (host or '').split(':')[0].strip().strip('[]').lower()
    if h in ('', 'localhost', 'localhost.localdomain'):
        return 'loopback'
    try:
        ip = ipaddress.ip_address(h)
    except ValueError as e:
        logger.debug('[Desktop] host %r is not an IP literal — treating '
                     'as public: %s', h, e)
        return 'public'
    if ip.is_loopback:
        return 'loopback'
    if ip.is_private:
        return 'private'
    return 'public'


def _caller_bridge_token_count(
    owner_user_id: int, tenant_id: str | None,
) -> int:
    """How many agents:bridge tokens the caller has minted (metadata only).

    Feeds the panel's waiting-diagnosis: tokens issued but zero agents
    arrived ⇒ the line was minted, so the failure is downstream of the
    copy — almost always the address half (a proxy URL the agent cannot
    use), which is exactly what server_url_reachability flags.
    """
    try:
        from lib.api_keys import list_keys
        return sum(
            1 for key in list_keys(
                owner_user_id=owner_user_id, tenant_id=tenant_id)
            if _BRIDGE_SCOPE in (key.get('scopes') or [])
        )
    except Exception as e:
        logger.debug('bridge token count unavailable: %s', e)
        return 0


_BRIDGE_SCOPE = 'agents:bridge'


def _local_kernel_pairing_refusal():
    """Devices dial the server they pair with; a local kernel is loopback-only.

    No machine other than the phone can reach it, so pairing codes are neither
    minted nor exchanged there.
    """
    if current_app.extensions.get('tofu_local_kernel') is None:
        return None
    return api_conflict(
        'This local kernel listens on loopback only, so no device can pair with it.',
        source='api_v1.desktop.local_kernel')


# Routes a node can use without an SSO edge in between: a direct LAN address
# when the bind makes one reachable. Pairing links carry it as a ``via``
# candidate tried before the browser-visible base.

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _server_bind_class() -> str:
    """'all' | 'loopback' | 'specific' — how off-machine-reachable we are.

    Reads the bind the RUNNING server actually took (``_TOFU_RUNTIME_HOST``,
    recorded by server.py at boot). A loopback bind means NO desktop agent
    on another machine can ever reach this server directly — the panel
    surfaces that as an operator warning instead of letting the attach
    flow fail silently (owner incident 2026-08-05: a platform-injected
    BIND_HOST=127.0.0.1 quietly overrode the 0.0.0.0 default).
    """
    host = (os.environ.get('_TOFU_RUNTIME_HOST')
            or os.environ.get('BIND_HOST') or '0.0.0.0').strip().lower()
    if host in _LOOPBACK_HOSTS:
        return 'loopback'
    if host in ('', '0.0.0.0', '::'):
        return 'all'
    return 'specific'


def _direct_lan_candidate() -> str:
    """``http://<lan-ip>:<port>`` when the bind makes it reachable, else ''.

    Same honesty guard as the LAN discovery responder (lib/desktop/
    pairing.py): advertising an address the server cannot be reached at
    sends every discovering agent to a dead route.
    """
    if _server_bind_class() == 'loopback':
        return ''
    from lib.desktop.pairing import lan_ip
    ip = lan_ip()
    if not ip:
        return ''
    try:
        port = int(os.environ.get('_TOFU_RUNTIME_PORT') or '15000')
    except (TypeError, ValueError) as e:
        logger.debug('[Desktop] bad _TOFU_RUNTIME_PORT: %s', e)
        port = 15000
    return 'http://%s:%d' % (ip, port)


def _visitor_os() -> str:
    """The request visitor's OS key: 'windows' | 'macos' | 'linux' | ''.

    Picks the desktop installer row the panel offers first; one
    ``_detect_os`` owns the rule, the frontend only renders it.
    """
    from quart import request
    try:
        ua = request.user_agent.string or ''
    except Exception as e:
        logger.debug('[Desktop] user-agent parse failed: %s', e)
        ua = ''
    return _detect_os(ua)


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Node presence and setup status for the Local Control panel',
    description=(
        'Returns ``{connected, last_poll, pending_commands, setup_state, '
        'visitor_os, download_url, downloads, server_version, '
        'installers_refreshing, server_url, server_url_reachability, '
        'bridge_tokens_issued, server_bind, agents, nodes}``. '
        '``connected`` is true when one of the CALLER\'s nodes polled within '
        'the last 15 s. ``setup_state`` is ``connected`` or ``pair`` (show '
        'the pairing link). Each ``downloads`` row names its ``version`` and '
        'whether it is ``older_than_server``; ``installers_refreshing`` is '
        'true while the server is fetching published installers.'
    ),
    tags=['capabilities'],
)
def desktop_status():
    """SYNC on purpose: artifact-store and credential reads block on I/O."""
    from quart import request
    from lib.desktop import last_poll_time, list_agents, pending_commands_count
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop status')
    agents = _with_drift(list_agents(user_id=str(owner_user_id)))
    connected = any(a.get('online') for a in agents)
    _last = last_poll_time()
    _arch = (request.args.get('arch') or '').strip()[:16]
    # Computed first: it kicks the mirror that installers_refreshing reports.
    downloads = _request_platform_downloads(_arch)
    return api_ok({
        'connected': connected,
        'last_poll': _last,
        'visitor_os': _visitor_os(),
        'secondsAgo': (round(time.time() - _last, 1) if _last else None),
        'pending_commands': pending_commands_count(),
        'agents': agents,
        'nodes': _node_listing(owner_user_id, principal.tenant_id, agents),
        'setup_state': _setup_state(connected),
        'download_url': _desktop_download_url(),
        'downloads': downloads,
        'server_version': _dist_store.server_version(),
        'installers_refreshing': _dist_mirror.is_running(),
        'server_url': _agent_server_url(),
        'server_url_reachability': _host_reachability(request.host),
        'bridge_tokens_issued': _caller_bridge_token_count(
            owner_user_id, principal.tenant_id),
        # A loopback bind makes every off-machine node unreachable by
        # construction; the panel warns instead of letting pairing fail.
        'server_bind': _server_bind_class(),
    })


def _bridge_credentials(owner_user_id: int, tenant_id: str | None) -> list[dict]:
    from lib.api_keys import list_keys
    return [
        {'id': k.get('id'), 'name': k.get('name'),
         'created_at': k.get('created_at')}
        for k in list_keys(owner_user_id=owner_user_id, tenant_id=tenant_id)
        if _BRIDGE_SCOPE in (k.get('scopes') or [])
    ]


def _node_row(record, agent, credential, transport, predecessor=None, policy=None) -> dict:
    from lib.desktop.device_policy import permissions_view
    from lib.desktop_wire.node_protocol import granted_tiers
    agent = agent or {}
    node_id = record['nodeId'] if record else agent.get('agent_id')
    # Effective permissions: the Home ceiling AND the device's declared switch
    # (read-only until the node has polled this Home process).
    tiers = (permissions_view(policy['owner'], node_id, agent.get('capabilities'),
                              record=policy['record'], rev=policy['rev'],
                              known=bool(agent))['effective']
             if node_id else granted_tiers(None))
    state = 'unregistered'
    if record is not None:
        state = 'conflict' if record['state'] == 'active' and record.get('conflict') else record['state']
    online = bool(agent.get('online'))
    return {
        'node_id': record['nodeId'] if record else agent.get('agent_id'),
        'name': (record or {}).get('name') or agent.get('name') or agent.get('agent_id')
                or (credential or {}).get('name') or (credential or {}).get('id') or '',
        'state': state,
        'online': online,
        'last_seen': agent.get('last_seen'),
        'platform': agent.get('platform') or (record or {}).get('platform') or '',
        'version': agent.get('version') or '',
        'outdated': bool(agent.get('outdated')),
        'system': agent.get('system'),
        'share_roots': list(agent.get('share_roots') or []),
        'tiers': tiers,
        'credential': credential,
        'attachment': 'paired' if (record or credential or agent.get('key_id')) else 'process',
        'fingerprint': (record or {}).get('fingerprint'),
        'kind': (record or {}).get('kind'),
        'paired_at': (record or {}).get('pairedAt'),
        'confirmed_at': (record or {}).get('confirmedAt'),
        'route': transport['route'] if online else None,
        'rtt_ms': transport['rtt_ms'] if online else None,
        'running': transport['running'] if online else 0,
        'replacement_fingerprint': ((record or {}).get('replacement') or {}).get('fingerprint'),
        'conflict': (record or {}).get('conflict'),
        'predecessor': predecessor,
    }


def _node_listing(owner_user_id: int, tenant_id: str | None, agents) -> list[dict]:
    """The caller's nodes (``Node`` in contracts/desktop_node_v1.schema.json).

    The Home's durable registry (lib/desktop/node_registry.py) is the list of
    paired nodes, online or not, pending or active; the live bridge registry
    adds liveness and the facts of the latest poll. A live node without a
    record is the packaged app's in-process node; a bridge credential that no
    record names and no node has polled with yet (minted before the registry,
    or by the owner directly) is listed ``unregistered`` until its first poll
    adopts it.
    """
    from lib.desktop.device_policy import policy as device_policy
    from lib.desktop.node_registry import NodeRegistry, transport_facts
    record, rev = device_policy(owner_user_id)
    policy = {'owner': owner_user_id, 'record': record, 'rev': rev}
    credentials = {c['id']: c for c in _bridge_credentials(owner_user_id, tenant_id)}
    live = {a['agent_id']: a for a in agents}
    online_ids = {a['agent_id'] for a in agents if a.get('online')}
    records = NodeRegistry(owner_user_id, tenant_id=tenant_id).list()
    by_id = {record['nodeId']: record for record in records}
    nodes, bound = [], set()
    for record in records:
        predecessor = by_id.get(record.get('predecessor') or '')
        if predecessor is not None:
            predecessor = {'node_id': predecessor['nodeId'], 'name': predecessor['name'],
                           'online': predecessor['nodeId'] in online_ids}
        agent = live.pop(record['nodeId'], None)
        if agent is not None and agent.get('key_id') != record['credentialId']:
            live[record['nodeId']] = agent
            agent = None
        bound.add(record['credentialId'])
        if record.get('replacement'):
            bound.add(record['replacement']['credentialId'])
        nodes.append(_node_row(record, agent, credentials.get(record['credentialId']),
                               transport_facts(owner_user_id, record['nodeId']), predecessor,
                               policy))
    for agent in live.values():
        credential = credentials.get(agent.get('key_id') or '')
        if credential:
            bound.add(credential['id'])
        nodes.append(_node_row(None, agent, credential,
                               transport_facts(owner_user_id, agent['agent_id']), policy=policy))
    for credential in credentials.values():
        if credential['id'] not in bound:
            nodes.append(_node_row(None, None, credential,
                                   {'route': None, 'rtt_ms': None, 'running': 0}, policy=policy))
    return nodes


@api_v1_desktop_bp.route('/api/v1/desktop/nodes', methods=['GET'])
@require_auth
@api_meta(
    summary='List the caller\'s nodes (this machine and other machines)',
    description=(
        'Shape: ``NodeListing`` in contracts/desktop_node_v1.schema.json. '
        'Paired nodes come from the Home\'s durable node registry '
        '(contracts/desktop_node_registry_v1.schema.json) with their trust '
        'state (pending / active / conflict) and fingerprint; liveness and '
        'poll facts come from the live bridge registry. Tools address an '
        'active node by ``node_id`` or name through their ``device`` field.'
    ),
    tags=['capabilities'],
)
def desktop_nodes():
    """SYNC on purpose: the registry and credential listings block on storage I/O."""
    from lib.desktop import list_agents
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop nodes')
    agents = _with_drift(list_agents(user_id=str(owner_user_id)))
    return api_ok({'nodes': _node_listing(owner_user_id, principal.tenant_id, agents)})


def _follow_policy(owner_user_id: int) -> None:
    """Registry transitions change device policy (removal, re-pair); the execution
    host drops its cached copy like any policy change (routes/api_v1/devices.py)."""
    from .devices import _follow_in_execution
    _follow_in_execution(owner_user_id)


def _registry_call(context: str, operation):
    """Run one owner transition of the node registry and map its typed failures."""
    from lib.desktop.node_registry import NodeNotFound, NodeRegistry, NodeStateError
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context=context)
    registry = NodeRegistry(owner_user_id, tenant_id=principal.tenant_id)
    try:
        outcome = operation(registry)
        _follow_policy(owner_user_id)
        return api_ok(outcome)
    except NodeNotFound:
        return api_not_found('not_found', message='node not found')
    except NodeStateError as error:
        return api_conflict(str(error), source='api_v1.desktop.' + context.replace(' ', '_'))


@api_v1_desktop_bp.route('/api/v1/desktop/nodes/<node_id>/confirm', methods=['POST'])
@require_auth
@api_meta(
    summary='Confirm a node that redeemed a pairing',
    description=(
        'Owner decision 5: a redeemed pairing waits for this confirmation '
        'before the node receives any command. Confirms a pending node, or '
        'a pending re-pairing of an active node ID (the previous credential '
        'is revoked), and clears a recorded identity conflict. Compare the '
        'fingerprint shown by the node first. Body: ``confirmRequest`` in '
        'contracts/desktop_node_registry_v1.schema.json; answer '
        '``nodeTransition``.'
    ),
    tags=['capabilities'],
)
async def desktop_node_confirm(node_id):
    import asyncio
    body = await async_parse_body(exact=('name',))
    name = optional_str(body, 'name', default='', max_len=80).strip() or None
    return await asyncio.to_thread(
        _registry_call, 'desktop node confirm',
        lambda registry: registry.confirm(node_id, name=name))


@api_v1_desktop_bp.route('/api/v1/desktop/nodes/<node_id>/reject', methods=['POST'])
@require_auth
@api_meta(
    summary='Reject a node that redeemed a pairing',
    description=(
        'Revokes the credential issued at redemption. A pending node is '
        'forgotten; a pending re-pairing of an active node is dropped and the '
        'active node keeps working. Answer ``nodeTransition``.'
    ),
    tags=['capabilities'],
)
def desktop_node_reject(node_id):
    """SYNC on purpose: revocation blocks on storage I/O."""
    return _registry_call('desktop node reject', lambda registry: registry.reject(node_id))


@api_v1_desktop_bp.route('/api/v1/desktop/nodes/<node_id>', methods=['PATCH'])
@require_auth
@api_meta(
    summary='Rename one of the caller\'s nodes',
    description='Body: ``renameRequest`` in contracts/desktop_node_registry_v1.schema.json.',
    tags=['capabilities'],
)
async def desktop_node_rename(node_id):
    import asyncio
    body = await async_parse_body(exact=('name',))
    name = optional_str(body, 'name', default='', max_len=80).strip()
    if not name:
        return api_bad_request('name is required', field='name')
    return await asyncio.to_thread(
        _registry_call, 'desktop node rename', lambda registry: registry.rename(node_id, name))


@api_v1_desktop_bp.route('/api/v1/desktop/nodes/<node_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Remove one of the caller\'s nodes',
    description=(
        'Revokes every credential the Home issued to the node, disconnects '
        'it at once (its registry entry is dropped and its pending commands '
        'fail) and forgets its durable record, online or not. The node sees '
        'its next poll refused and stops that attachment. The in-process node '
        'of the packaged app has no credential (409).'
    ),
    tags=['capabilities'],
)
def desktop_node_remove(node_id):
    """SYNC on purpose: credential revocation blocks on storage I/O."""
    from lib.desktop import list_agents
    from lib.desktop.node_registry import NodeRegistry
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop node remove')
    registry = NodeRegistry(owner_user_id, tenant_id=principal.tenant_id)
    if registry.get(node_id) is not None:
        outcome = registry.forget(node_id)
        _follow_policy(owner_user_id)
        return api_ok({'removed': node_id, 'credential': outcome['revoked_credentials'][0],
                       'disconnected': outcome['disconnected']})
    agent = next((a for a in list_agents(user_id=str(owner_user_id))
                  if a['agent_id'] == node_id), None)
    if agent is None:
        return api_not_found('not_found', message='node not found')
    return api_conflict(
        'This node is the desktop app\'s own in-process node; turn off '
        'Computer Control in that app instead.',
        source='api_v1.desktop.node_remove')


@api_v1_desktop_bp.route('/api/v1/desktop/build', methods=['GET', 'POST'])
@require_auth
@api_meta(
    summary='Inspect (GET) or kick (POST) an on-server desktop build',
    description=(
        'POST starts a single-flight background build of the desktop app '
        'from the COMMITTED tree (git archive HEAD → PyInstaller → boot '
        'smoke), recorded in the artifact store with ``source == "built"``. '
        'The default (or ``{"os": "linux"}``) builds the server\'s own '
        'platform natively. ``{"os": "windows"}`` drives the userspace Wine '
        'toolchain (lib/desktop_dist/winbuilder.py — payload cached per '
        '(git_sha, deps), then the NSIS wrapper). Build selectors are '
        'validated rather than defaulted on typos. '
        'macOS cannot be built on Linux (documented permanent boundary — the '
        'mirror serves it). GET returns both builders\' persisted states.'
    ),
    tags=['capabilities'],
)
async def desktop_build():
    from quart import request
    from lib.desktop_dist import builder as _dist_builder
    if request.method == 'POST':
        body = await async_parse_body(strict=True)
        os_key, kind = _desktop_build_parameters(body)
        if os_key == 'windows':
            from lib.desktop_dist import winbuilder as _win_builder
            st = _win_builder.start_installer(reason='api', target=kind)
            audit_log('desktop_build_kicked', os='windows', kind=kind,
                      state=st.get('state'), version=st.get('version'))
            return api_payload(st, 202)
        st = _dist_builder.start(reason='api')
        audit_log('desktop_build_kicked', state=st.get('state'),
                  version=st.get('version'))
        return api_payload(st, 202)
    from lib.desktop_dist import winbuilder as _win_builder
    return api_ok({'linux': _dist_builder.state(),
                   'windows': _win_builder.state()})


@api_v1_desktop_bp.route('/api/v1/desktop/download/<path:filename>',
                         methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('*/*', conditional=True),

    summary='Download a server-hosted desktop installer',
    description=(
        'Serves an installer from the local artifact store '
        '(lib/desktop_dist) as an attachment, with Range support '
        '(``conditional=True``) so a 100+ MB download is resumable. '
        '``filename`` must exactly match a manifest entry — no path material '
        'is accepted, so traversal is structurally impossible.'
    ),
    tags=['capabilities'],
)
@raw_success_response
def desktop_download(filename):
    """SYNC on purpose: file serving crosses the explicit Quart boundary in
    the executor (same carve-out as serve_motion_file), so a 135 MB stream
    never sits on the event loop."""
    path = _dist_store.resolve_file(filename)
    if path is None:
        return api_not_found('not_found',
                             message='no such artifact')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, as_attachment=True,
                                 attachment_filename=filename)


# ── Client-diagnostics inbox (owner ask 2026-08-06) ──────────────────
# A controlled machine that cannot reach this server cannot push its own
# logs anywhere — so the agent's tray / role-window carries a「复制诊断
# 信息」button (desktop/agent_launcher._diag_report), and the user pastes
# the bundle HERE. The server appends it to a JSONL file the operator (or
# the assistant) reads straight from disk; the GET lets the panel confirm
# the paste landed. The agent redacts first and the server recursively redacts
# again as mandatory defense in depth; text is capped at _DIAG_MAX_CHARS.
# The file uses the shared
# stream policy rather than growing once per authenticated paste forever.
_DIAG_LOG = os.path.join(LOG_DIR, 'desktop_client_diag.log')
_DIAG_MAX_CHARS = 200_000
_DIAG_LIST_LIMIT = 20
_DIAG_WRITE_LOCK = threading.Lock()


def _append_client_diag_entry(entry: dict) -> None:
    """Append one redacted JSONL record under the shared stream ceiling."""
    import json as _json

    safe_entry = sanitize_value(
        dict(entry), max_items=12,
        max_string_chars=_DIAG_MAX_CHARS)
    safe_entry = safe_entry if isinstance(safe_entry, dict) else {}
    safe_entry['text'] = text_value(
        safe_entry.get('text') or '', max_chars=_DIAG_MAX_CHARS)
    line = (_json.dumps(safe_entry, ensure_ascii=False) + '\n').encode('utf-8')
    os.makedirs(os.path.dirname(_DIAG_LOG), exist_ok=True)
    with _DIAG_WRITE_LOCK:
        ceiling = stream_max_bytes('desktop_client_diag')
        copytruncate_if_oversize(
            _DIAG_LOG,
            max_bytes=ceiling,
            trigger_bytes=max(1, ceiling - len(line)),
            backup_count=stream_backup_count('desktop_client_diag'))
        append_bytes_locked(_DIAG_LOG, line)


@api_v1_desktop_bp.route('/api/v1/desktop/client-diag', methods=['POST'])
@require_auth
@api_meta(
    summary='Store a pasted agent-diagnostics bundle',
    description=(
        'The counterpart of the agent\'s copy-diagnostics button: the user '
        'pastes the clipboard bundle into the Local Control panel and it '
        'lands in logs/desktop_client_diag.log as one JSON line '
        '(ts/user_id/text). 400 on an empty paste; the text is capped at '
        '200k chars.'
    ),
    tags=['capabilities'],
)
async def desktop_client_diag_submit():
    from lib.request_parser import async_parse_body
    from .auth import request_principal
    owner_user_id = request_principal().require_owner(
        context='desktop diagnostics')
    body = await async_parse_body(exact=('text',))
    # Manual validation (not optional_str): the refusal must ride the
    # api_error envelope, not the global BadRequest handler.
    text = body.get('text')
    if not isinstance(text, str):
        return api_error('bad_diag', status=400,
                         message='text must be a string')
    text = text.strip()
    if len(text) > _DIAG_MAX_CHARS:
        return api_error('diag_too_large', status=400,
                         message='diagnostics too large (max %d chars)'
                                 % _DIAG_MAX_CHARS)
    if not text:
        return api_error('empty_diag', status=400,
                         message='nothing to store — paste the copied '
                                 'diagnostics first')
    entry = {'ts': time.time(), 'owner_user_id': owner_user_id, 'text': text}
    try:
        _append_client_diag_entry(entry)
    except OSError as e:
        logger.error('[Desktop] client-diag store failed: %s', e,
                     exc_info=True)
        return api_error('diag_store_failed', status=500,
                         message='could not store the diagnostics — '
                                 'see logs/error.log')
    logger.info('[Desktop] client diagnostics received (%d chars, user=%s)',
                len(text), owner_user_id)
    return api_ok({'received': len(text)})


@api_v1_desktop_bp.route('/api/v1/desktop/client-diag', methods=['GET'])
@require_auth
@api_meta(
    summary='List recent pasted agent-diagnostics bundles',
    description=(
        'Newest-first metadata + preview of the last 20 submissions '
        '(the full text stays on disk in logs/desktop_client_diag.log). '
        'Lets the panel confirm a paste landed.'
    ),
    tags=['capabilities'],
)
async def desktop_client_diag_list():
    import json as _json
    from .auth import current_auth

    entries = []
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(_DIAG_LOG, flags)
        with os.fdopen(descriptor, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            if size > 2_000_000:
                f.seek(size - 1_000_000)
                f.readline()  # drop the partial line the seek landed in
                lines = f.readlines()
            else:
                lines = f.readlines()
        auth = current_auth()
        requesting_owner_user_id = str(auth.owner_user_id or '')
        include_all_users = bool(auth and auth.has_scope('admin'))
        for raw_line in reversed(lines):
            try:
                e = _json.loads(raw_line.decode('utf-8', errors='replace'))
            except ValueError as e2:
                logger.debug('[Desktop] diag line undecodable: %s', e2)
                continue
            if not isinstance(e, dict):
                continue
            entry_owner_user_id = str(e.get('owner_user_id') or '')
            if (not include_all_users
                    and entry_owner_user_id != requesting_owner_user_id):
                continue
            entries.append({'ts': e.get('ts'),
                            'owner_user_id': e.get('owner_user_id'),
                            'chars': len(e.get('text') or ''),
                            'preview': (e.get('text') or '')[:200]})
            if len(entries) >= _DIAG_LIST_LIMIT:
                break
    except FileNotFoundError:
        # Only absence is an empty inbox; any other read fault is a server
        # error, never "nothing was pasted".
        logger.debug('[Desktop] client-diag file absent — nothing to list')
    return api_ok({'entries': entries})


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
        return api_not_found(
            'not_found', message='unknown or expired command stream')
    return api_ok(stream)


@api_v1_desktop_bp.route('/api/v1/desktop/devices', methods=['GET'])
@require_auth
@api_meta(
    summary='Devices page: the caller\'s agents + their bridge tokens',
    description=(
        'Tokens are listed METADATA-ONLY (id/name/created/scopes) — the '
        'secret is only ever returned once, by POST /api/v1/desktop/token.'
    ),
    tags=['capabilities'],
)
async def desktop_devices():
    """Devices page payload: the caller's agents + their bridge tokens."""
    from lib.api_keys import list_keys
    from lib.desktop import list_agents
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop devices')
    tokens = [
        {'id': k.get('id'), 'name': k.get('name'),
         'created_at': k.get('created_at'),
         'scopes': sorted(k.get('scopes') or [])}
        for k in list_keys(
            owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
        if _BRIDGE_SCOPE in (k.get('scopes') or [])
    ]
    return api_ok({
        'agents': _with_drift(list_agents(user_id=str(owner_user_id))),
        'tokens': tokens,
    })


def _device_wait_failure(error: Exception):
    from lib.storage import http_status_for_storage_error
    if isinstance(error, StorageError):
        return api_error(error, status=http_status_for_storage_error(error))
    return api_bad_request(str(error))


def _device_wait_parameters():
    from lib.desktop.device_waits import query_parameters
    return query_parameters('waitListQuery')


@api_v1_desktop_bp.route('/api/v1/desktop/device-waits', methods=['GET'])
@require_scope('conversations')
@api_meta(
    summary='Conversations waiting for one of the caller\'s devices to reconnect',
    description=(
        'Owner-scoped await_device registrations: the pending ones by device, '
        'or with includeClosed=true every registration in registration order. '
        'Shapes: contracts/device_waits_v1.schema.json (waitPage).'
    ),
    tags=['capabilities'], scope='conversations',
    parameters=_device_wait_parameters(),
)
def desktop_device_waits():
    """SYNC on purpose: owner-scoped record reads block on storage I/O."""
    from quart import request
    from lib.desktop.device_waits import DeviceWaits, validate
    from .auth import request_principal
    owner_user_id = request_principal().require_owner(context='device waits')
    try:
        query = {}
        for name in request.args:
            if len(request.args.getlist(name)) != 1:
                raise ValueError('Repeated query field: ' + name)
            query[name] = request.args[name]
        validate(query, 'waitListQuery')
        return api_ok(DeviceWaits(owner_user_id).page(
            cursor=query.get('cursor'), include_closed=query.get('includeClosed') == 'true'))
    except (StorageError, ValueError) as error:
        return _device_wait_failure(error)


@api_v1_desktop_bp.route('/api/v1/desktop/device-waits/<wait_id>/cancel', methods=['POST'])
@require_scope('conversations')
@api_meta(
    summary='Cancel one of the caller\'s device waits',
    description=(
        'Idempotent: a delivered, failed or already cancelled wait is '
        'returned unchanged. Another owner\'s wait is not found.'
    ),
    tags=['capabilities'], scope='conversations',
)
def desktop_device_wait_cancel(wait_id):
    """SYNC on purpose: the CAS transition blocks on storage I/O."""
    from lib.desktop.device_waits import DeviceWaitNotFound, DeviceWaits
    from .auth import request_principal
    owner_user_id = request_principal().require_owner(context='device wait cancel')
    try:
        return api_ok(DeviceWaits(owner_user_id).cancel(wait_id))
    except DeviceWaitNotFound:
        return api_not_found('not_found', message='device wait not found')
    except (StorageError, ValueError) as error:
        return _device_wait_failure(error)


@api_v1_desktop_bp.route('/api/v1/desktop/pair-code', methods=['POST'])
@require_auth
@api_meta(
    summary='Mint a one-time pairing link (and its 6-digit code)',
    description=(
        'Mints one durable pairing (valid 5 minutes across Home restarts, '
        'one-shot, per-IP failure budget) '
        'bound to the caller: ``link`` is what the owner pastes into, or '
        'scans with, the Tofu desktop app (``PairingLink`` in '
        'contracts/desktop_node_v1.schema.json); ``code`` is the same '
        'pairing as 6 typeable digits. ``?base=`` (the page\'s live origin '
        '+ path prefix, pinned to the request host) becomes the link base; '
        'a reachable direct LAN address is added as a ``via`` route. '
        '``command`` is the one-line headless join. The node redeems either '
        'form through POST /api/desktop/pair and then waits for the owner to '
        'confirm it (POST /api/v1/desktop/nodes/<node_id>/confirm).'
    ),
    tags=['capabilities'],
)
async def desktop_pair_code_mint():
    """Mint a one-time pairing code.

    The code is the ONLY credential the agent needs to attach itself
    to this user's account — no bearer, no bridge secret. The panel
    renders the code big with a copy button and a 5-minute countdown.
    """
    refusal = _local_kernel_pairing_refusal()
    if refusal is not None:
        return refusal
    from lib.desktop.pairing import (
        _CODE_TTL_S, PairingIdentity, mint_pairing, pending_codes,
    )
    from lib.desktop_wire.node_protocol import build_pairing_link, headless_join_command
    from routes.browser import _external_base_url
    from .auth import current_auth, request_principal
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop pairing')
    identity = PairingIdentity(
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    import asyncio
    code, link_secret, expires_at = await asyncio.to_thread(mint_pairing, identity)
    direct = _direct_lan_candidate()
    link = build_pairing_link(_external_base_url(), link_secret,
                              via=[direct] if direct else [])
    audit_log('desktop_pair_code_minted', owner_user_id=owner_user_id)
    return api_created({
        'code': code,
        'link': link,
        'command': headless_join_command(link),
        'expires_at': expires_at,
        'ttl': _CODE_TTL_S,
        'pending': await asyncio.to_thread(pending_codes, owner_user_id),
    })


@api_v1_desktop_bp.route('/api/desktop/pair', methods=['POST'])
@api_meta(
    summary='Exchange a pairing code for a bridge token',
    description=(
        'The AGENT calls this (NO bearer — the code IS the credential) '
        'to consume a one-time code and receive an agents:bridge token for a '
        '*pending* node: it polls but receives no command until the owner '
        'confirms it (``fingerprint`` is shown on both ends). '
        'One-shot: a code that is missing, expired, over-attempted, or '
        'already used fails. Does NOT require authentication: this is '
        'the onboarding of a fresh agent that has no token yet.'
    ),
    tags=['capabilities'],
)
async def desktop_pair():
    """Exchange a pairing code for an agents:bridge token.

    Not authenticated by design: the agent pairing itself in has no
    token. The 6-digit one-time code is the sole credential; it is
    consumed exactly once. On success the agent gets a bridge token
    bound to the code's minting user, saves it as its remote
    attachment, and starts polling.
    """
    refusal = _local_kernel_pairing_refusal()
    if refusal is not None:
        return refusal
    from quart import request
    from lib.api_keys import create_key
    from lib.desktop.pairing import (consume_code,
                                     ip_fail_budget_exceeded,
                                     record_pair_failure,
                                     record_pair_success)
    # Per-IP global failure budget (owner 2026-08-04): an attacker who
    # keeps guessing NEW codes gets a fresh per-code budget each time, so
    # per-code lockout alone leaves 1e6 space brute-forceable. A blocked
    # IP gets 429 BEFORE its code is even looked up — the rate bound is
    # the real boundary, not the per-code retry count.
    client_ip = request.remote_addr or '<unknown>'
    if ip_fail_budget_exceeded(client_ip):
        audit_log('desktop_pair_rate_limited', ip=client_ip)
        return api_error('pair_rate_limited', status=429,
                         message='Too many failed pairing attempts from '
                                 'this address. Wait a few minutes and '
                                 'try again with a fresh code.')
    import asyncio
    from lib.desktop.node_registry import CREDENTIAL_ORIGIN, NodeRegistry, valid_node_id
    from lib.desktop_wire.node_protocol import node_contract
    body = await async_parse_body(exact=('code', 'name', 'platform', 'node_id', 'kind',
                                         'previous_node_id'))
    code = optional_str(body, 'code', default='',
                        max_len=64).strip()
    node_id = optional_str(body, 'node_id', default='', max_len=128).strip()
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'paired-agent'
    platform = optional_str(body, 'platform', default='',
                            max_len=40).strip() or 'unknown'
    kind = optional_str(body, 'kind', default='computer', max_len=16).strip()
    previous_node_id = optional_str(body, 'previous_node_id', default='', max_len=128).strip()
    if previous_node_id and not valid_node_id(previous_node_id):
        return api_bad_request('invalid previous_node_id', field='previous_node_id')
    if not valid_node_id(node_id):
        return api_bad_request('desktop_node_identity_required', field='node_id',
                               message='Update this node: pairing needs its node ID.')
    if kind not in node_contract()['$defs']['NodeKind']['enum']:
        return api_bad_request('invalid kind', field='kind')
    identity = await asyncio.to_thread(consume_code, code, node_id=node_id)
    if identity is None:
        record_pair_failure(client_ip)
        audit_log('desktop_pair_failed', code=code[:2] + '****',
                  reason='invalid_code')
        return api_conflict('invalid_code',
                            message='This pairing code is invalid, expired, '
                                    'or already used. Generate a new one '
                                    'in the panel.')
    record_pair_success(client_ip)
    row, token = await asyncio.to_thread(
        create_key,
        name,
        scopes=[_BRIDGE_SCOPE],
        owner_user_id=identity.owner_user_id,
        account_user_id=identity.account_user_id,
        tenant_id=identity.tenant_id,
        metadata={'origin': CREDENTIAL_ORIGIN, 'node_id': node_id},
    )
    registry = NodeRegistry(identity.owner_user_id, tenant_id=identity.tenant_id)
    try:
        record = await asyncio.to_thread(
            registry.record_pairing, node_id=node_id, credential_id=row['id'],
            name=name, platform=platform, kind=kind, previous_node_id=previous_node_id)
    except Exception:
        # Never leave a live credential the registry does not name.
        from lib.api_keys import revoke_key
        await asyncio.to_thread(revoke_key, row['id'], owner_user_id=identity.owner_user_id,
                                tenant_id=identity.tenant_id)
        raise
    fingerprint = (record.get('replacement') or record)['fingerprint']
    audit_log('desktop_pair_succeeded', key_id=row.get('id'),
              owner_user_id=identity.owner_user_id, platform=platform,
              node_id=node_id)
    return api_created({
        'id': row.get('id'),
        'name': name,
        'token': token,
        'scopes': [_BRIDGE_SCOPE],
        'owner_id': identity.owner_user_id,
        'node_id': node_id,
        'kind': kind,
        'fingerprint': fingerprint,
        'node_state': 'pending_confirmation',
    })


@api_v1_desktop_bp.route('/api/v1/desktop/token', methods=['POST'])
@require_auth
@api_meta(
    summary='Mint a per-user bridge token (scope agents:bridge)',
    description=(
        'The raw secret is returned EXACTLY ONCE in this response; '
        'afterwards only metadata is listable. Bound to the caller\'s '
        'user_id so poll auth scopes every command to them (RWA P4a).'
    ),
    tags=['capabilities'],
)
async def desktop_token_mint():
    """Mint a per-user bridge token (scope agents:bridge)."""
    from lib.api_keys import create_key
    from lib.request_parser import async_parse_body, optional_str
    from .auth import current_auth, request_principal
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop token mint')
    body = await async_parse_body(exact=('name',))
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'desktop-bridge'
    row, token = create_key(
        name,
        scopes=[_BRIDGE_SCOPE],
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    audit_log('desktop_bridge_token_minted', key_id=row.get('id'),
              name=name, owner_user_id=owner_user_id)
    return api_created({'id': row.get('id'), 'name': name,
                        'token': token, 'scopes': [_BRIDGE_SCOPE]})


@api_v1_desktop_bp.route('/api/v1/desktop/token/<key_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Revoke one of the caller\'s OWN bridge tokens',
    description=(
        'Deliberately NOT the admin-scoped /api/v1/keys DELETE: a tenant '
        'may revoke only their own agents:bridge keys, nothing wider. '
        'Nodes polling with the key are disconnected at once.'
    ),
    tags=['capabilities'],
)
async def desktop_token_revoke(key_id):
    """Revoke one of the caller's OWN bridge tokens."""
    from lib.api_keys import get_key_by_id, revoke_key
    from lib.desktop import disconnect_credential
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop token revoke')
    row = get_key_by_id(
        key_id, owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
    if not row or _BRIDGE_SCOPE not in (row.get('scopes') or []):
        return api_not_found('not_found',
                             message='bridge token not found')
    from lib.desktop.node_registry import NodeRegistry
    forgotten = NodeRegistry(owner_user_id, tenant_id=principal.tenant_id) \
        .forget_credential(key_id)
    if forgotten:
        _follow_policy(owner_user_id)
    revoke_key(
        key_id, owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
    disconnected = disconnect_credential(key_id, str(owner_user_id))
    audit_log('desktop_bridge_token_revoked', key_id=key_id,
              owner_user_id=owner_user_id)
    return api_ok({'revoked': key_id, 'disconnected': sorted(set(
        disconnected + ((forgotten or {}).get('disconnected') or [])))})


__all__ = ['api_v1_desktop_bp']
