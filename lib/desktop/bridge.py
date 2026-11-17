"""Desktop-agent bridge — in-process command queue + result formatting.

The server queues commands here; the desktop agent long-polls
``POST /api/desktop/poll`` (in ``routes/desktop.py``) to pick them up and
return results. This module owns the queue state and the blocking
``send_desktop_command`` RPC so that lib-layer tool handlers can drive the
agent without importing the routes package.

The process that terminates polls owns this state. A resident execution host
selects that owner through ``lib.desktop.bridge_relay``: the presence and
command verbs below forward to it (``_relay()``), so tools, context and the
liveness judge read the one registry Settings shows. Poll-side verbs
(register, take, resolve, revoke) always run in the owner.
"""

import asyncio
from functools import lru_cache
import json
import math
from pathlib import Path
import threading
import time
import uuid

from lib.desktop_wire import contract, limit, protocol_version
from lib.log import get_logger

logger = get_logger(__name__)

# How often a blocked caller re-checks its cancellation signal (local wait and
# relayed wait alike).
CANCEL_OBSERVATION_SECONDS = 0.25


def _relay():
    """The remote bridge owner selected for this process, or None (local owner)."""
    from lib.desktop.bridge_relay import selected_relay
    return selected_relay()


def _relay_unavailable(verb, exc):
    logger.warning('[Desktop] bridge owner unreachable for %s: %s', verb, exc)

# ══════════════════════════════════════════════════════════
#  Command Queue (mirrors lib/browser.py pattern)
# ══════════════════════════════════════════════════════════

command_queue: dict = {}
command_queue_lock = threading.Lock()

# Async-waiter registry for the async def /api/desktop/poll route. Mirrors
# lib/browser/queue.py: the agent's long-poll awaits an asyncio.Event so the
# worker thread is released; the SYNC send_desktop_command enqueue path wakes
# it via loop.call_soon_threadsafe. Each waiter {'loop':, 'event':} removes
# ITSELF in a finally (timeout / success / disconnect) so nothing leaks.
_async_waiters: list = []
_async_waiters_lock = threading.Lock()


def _wake_async_waiters(command) -> None:
    """Wake desktop async poll waiters after a command was enqueued (sync)."""
    with _async_waiters_lock:
        waiters = list(_async_waiters)
    for w in waiters:
        if w.get('user_id') != command.get('user_id'):
            continue
        if command.get('target_agent_id') and w.get('agent_id') != command['target_agent_id']:
            continue
        loop, event = w.get('loop'), w.get('event')
        if loop is None or event is None:
            continue
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError as e:
            logger.debug('[Desktop] async waiter wake skipped (loop closed): %s', e)

# Wrapped in a single-element list so route modules and this module share
# one mutable cell — a bare module int can't be rebound across a
# ``from ... import`` alias.
_last_poll = [0.0]

# Agent registry. Every poll carries a stable agent identity frame; anonymous
# agents are rejected at the HTTP boundary.
_agents: dict = {}

# Stream frame store (RWA P2 §3.4): cmd_id -> {'chunks': {seq: (stream,
# data)}, 'done': bool, 'updated_at': float}. The agent may re-send frames
# after a connection error (outbox prefix redeliver), so reassembly
# DEDUPES by seq; entries expire with the command TTL.
_streams: dict = {}

# Connection window: the agent is "connected" if it polled within this many
# seconds (contracts/device_routing_v1.schema.json x-liveness-window-seconds).
_CONNECTED_WINDOW_S = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts/device_routing_v1.schema.json')
    .read_text(encoding='utf-8'))['x-liveness-window-seconds']
# Pending commands older than this are expired (agent never picked them up).
_COMMAND_TTL_S = limit('replay_retention_seconds')
# Long-poll wait window (seconds) for the async poll route. Env-overridable
# (tests set a small value) to match the browser bridge knob.
import os as _os
try:
    POLL_WAIT_TIMEOUT = float(_os.environ.get('TOFU_DESKTOP_POLL_WAIT', '8'))
except (ValueError, TypeError) as _e:
    logger.debug('[Desktop] bad TOFU_DESKTOP_POLL_WAIT %r (%s) — using 8.0s default',
                 _os.environ.get('TOFU_DESKTOP_POLL_WAIT'), _e)
    POLL_WAIT_TIMEOUT = 8.0


def last_poll_time() -> float:
    """Epoch seconds of the agent's most recent poll (0 if never)."""
    return _last_poll[0]


def record_poll() -> None:
    """Mark the agent as having just polled (called by the poll endpoint)."""
    _last_poll[0] = time.time()


def is_desktop_agent_connected() -> bool:
    """Check if the desktop agent has polled recently."""
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            return bool(relay.call('connected', {}))
        except RelayUnavailable as exc:
            _relay_unavailable('connected', exc)
            return False
    return time.time() - _last_poll[0] < _CONNECTED_WINDOW_S


def _sweep_streams_locked(now):
    # Per-command TTL: an egress stream (ttl=1800s) must survive a long
    # upstream silence (LLM thinking blocks produce NO frames for minutes);
    # Unsettled default commands retain accepted transport; completed/orphaned
    # transport has an idle replay-retention window.
    stale = []
    for cid, e in _streams.items():
        cmd = command_queue.get(cid)
        if cmd and not cmd['event'].is_set() and not cmd.get('ttl'):
            continue  # Unsettled accepted work owns its pending transport.
        ttl = (cmd or {}).get('ttl') or _COMMAND_TTL_S
        if now - e['updated_at'] > ttl and not _command_active_locked(cmd, now):
            stale.append(cid)
    for cid in stale:
        del _streams[cid]


def _command_active_locked(command, now):
    if not command:
        return False
    agent = _agents.get(command.get('claimed_agent_id')) or {}
    return (agent.get('user_id') == command.get('user_id')
            and now - agent.get('last_seen', 0) < _CONNECTED_WINDOW_S
            and command['id'] in (agent.get('active_command_ids') or set()))


def command_active(cmd_id, now) -> bool:
    """Whether a queued command is claimed by a device still connected (liveness judge)."""
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            return bool(relay.call('command_active', {'cmd_id': str(cmd_id), 'now': float(now)}))
        except RelayUnavailable as exc:
            _relay_unavailable('command_active', exc)
            return False
    with command_queue_lock:
        return _command_active_locked(command_queue.get(cmd_id), now)


def _command_owned_by_poller(cmd, agent_id: str, user_id: str) -> bool:
    """Return whether one authenticated poller owns a queued command."""
    if not isinstance(cmd, dict):
        return False
    if (cmd.get('user_id') or '') != user_id:
        return False
    claimed_agent_id = cmd.get('claimed_agent_id') or ''
    return bool(agent_id) and claimed_agent_id == agent_id


def resolve_streams(frames, *, agent_id: str, user_id: str) -> int:
    """Ingest stream frames from a poll body. Returns new-chunk count.

    Frames are ``{cmd_id, seq, stream, data, done}``; re-sent frames are
    deduped by seq so an agent reconnect never double-counts output.
    """
    count = 0
    now = time.time()
    with command_queue_lock:
        _sweep_streams_locked(now)
        for f in frames or []:
            if not isinstance(f, dict):
                continue
            cmd_id = f.get('cmd_id', '')
            seq = f.get('seq')
            if not cmd_id or not isinstance(seq, int):
                continue
            cmd = command_queue.get(cmd_id)
            if not _command_owned_by_poller(cmd, agent_id, user_id):
                logger.warning(
                    '[Desktop] rejected stream frame for unowned command %s '
                    'from agent %s', cmd_id[:8], agent_id[:8])
                continue
            bounds = cmd.get('stream_limits')
            if bounds and cmd.get('stream_error'):
                continue
            entry = _streams.setdefault(
                cmd_id, {'chunks': {}, 'done': False, 'updated_at': now,
                         'consumed_seq': 0, 'bytes': 0})
            entry['updated_at'] = now
            if bounds:
                stream, data = f.get('stream'), f.get('data')
                invalid = (isinstance(seq, bool) or seq < 1
                           or not isinstance(stream, str) or len(stream) > 128
                           or not isinstance(data, str))
                try:
                    size = len(data.encode('utf-8')) if isinstance(data, str) else 0
                except UnicodeError:
                    invalid, size = True, 0
                if not invalid and (seq <= entry['consumed_seq'] or seq in entry['chunks']):
                    continue
                if (invalid or size > bounds['frame_bytes']
                        or entry['bytes'] + size > bounds['bytes']
                        or len(entry['chunks']) >= bounds['frames']):
                    cmd['stream_error'] = cmd['error'] = 'Desktop stream exceeds its transfer bounds'
                    cmd['event'].set()
                    continue
                entry['bytes'] += size
            if seq not in entry['chunks']:
                entry['chunks'][seq] = (
                    str(f.get('stream') or 'stdout'),
                    str(f.get('data') or ''),
                )
                count += 1
            if f.get('done'):
                entry['done'] = True
                entry['done_seq'] = seq
    return count


def get_frames(cmd_id, since_seq=0):
    """Ordered RAW frames for one streamed command (S3 desktop-egress).

    Returns ``(frames, done)`` where frames is ``[(seq, stream, data), …]``
    ascending by seq (only seq > ``since_seq``), or ``None`` when the entry
    is unknown/expired — callers MUST treat None-before-done as an aborted
    stream (design §4.3: never wait forever for a swept entry).
    ``get_command_stream`` keeps its stdout/stderr contract on top of this.
    """
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            got = relay.call('frames', {'cmd_id': str(cmd_id), 'since_seq': int(since_seq)})
        except RelayUnavailable as exc:
            _relay_unavailable('frames', exc)
            return None
        return None if got is None else ([tuple(frame) for frame in got[0]], got[1])
    now = time.time()
    with command_queue_lock:
        _sweep_streams_locked(now)
        entry = _streams.get(cmd_id)
        if entry is None:
            return None
        ordered = sorted((s, v) for s, v in entry['chunks'].items()
                         if s > since_seq)
        return ([(s, st, d) for s, (st, d) in ordered], entry['done'])


def get_command_stream(cmd_id, since_seq=0):
    """Reassembled stream for one command, or None when unknown/expired.

    Returns ``{'stdout', 'stderr', 'done', 'last_seq'}`` — pass
    ``since_seq=last_seq`` for an incremental read.
    """
    got = get_frames(cmd_id, since_seq)
    if got is None:
        return None
    frames, done = got
    text = {'stdout': [], 'stderr': []}
    last_seq = 0
    for seq, stream, data in frames:
        if stream in text:
            text[stream].append(data)
        last_seq = max(last_seq, seq)
    return {
        'stdout': ''.join(text['stdout']),
        'stderr': ''.join(text['stderr']),
        'done': done,
        'last_seq': last_seq,
    }


@lru_cache(maxsize=1)
def _system_facts_validator():
    from jsonschema import Draft202012Validator
    schema = contract()
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/SystemFacts'})


def _system_facts(value):
    """The declared fields of a valid agent ``system`` object, else None.

    The schema bounds every field; undeclared fields of a newer agent are not
    stored, and a malformed object counts as unreported.
    """
    if not _system_facts_validator().is_valid(value):
        return None
    shells = value['shells']
    if shells['default'] not in shells['available']:
        return None
    facts = {key: value[key] for key in ('os', 'release', 'version', 'arch') if key in value}
    facts['shells'] = {'available': list(shells['available']), 'default': shells['default']}
    return facts


def register_agent(agent_id, meta=None, user_id='', key_id='') -> dict | None:
    """Upsert a v2 agent in the registry and heartbeat it.

    ``meta`` is the agent frame from the poll body (name / platform /
    capabilities). ``user_id`` / ``key_id`` identify the bridge caller the
    poll authenticated as. Registration doubles as
    the liveness heartbeat: :func:`online_agents` only returns agents seen
    within the connection window, and a registered agent counts toward
    :func:`is_desktop_agent_connected`.

    Returns the new connection ``{agent_id, user_id, name, connection_epoch,
    connected_at}`` when this heartbeat brings the device online: first
    registration in this process, a heartbeat after the connection window
    lapsed, or a different owner. Heartbeats of a live connection return
    None, so an online transition is observed once per connection epoch.
    """
    meta = meta if isinstance(meta, dict) else {}
    connection = None
    with command_queue_lock:
        prev = _agents.get(agent_id) or {}
        now = time.time()
        owner = str(user_id or '')
        reconnected = (not prev or prev.get('user_id') != owner
                       or now - prev['last_seen'] >= _CONNECTED_WINDOW_S)
        epoch = (prev.get('connection_epoch') or 0) + 1 if reconnected else prev['connection_epoch']
        connected_at = now if reconnected else prev['connected_at']
        caps = meta.get('capabilities')
        _agents[agent_id] = {
            'agent_id': agent_id,
            'protocol_version': meta.get('protocol_version'),
            'execution_slots': meta.get('execution_slots', 0),
            'active_command_ids': (set(meta.get('active_command_ids') or [])
                                   if isinstance(meta.get('active_command_ids'), list)
                                   else set()),
            'name': str(meta.get('name') or prev.get('name') or ''),
            'platform': str(meta.get('platform') or prev.get('platform') or ''),
            # The drift signal (owner amendment ②): which build the agent
            # runs, so the server can flag a protocol-mismatched endpoint.
            # A frame without it (older agent) keeps the previous value.
            'version': str(meta.get('version') or prev.get('version') or ''),
            'capabilities': (dict(caps) if isinstance(caps, dict)
                             else prev.get('capabilities') or {}),
            'share_roots': (list(meta['share_roots'])
                            if isinstance(meta.get('share_roots'), list)
                            else prev.get('share_roots') or []),
            # Optional (older agents omit it): a frame without it keeps the
            # known facts; a malformed one clears them.
            'system': (_system_facts(meta['system']) if 'system' in meta
                       else prev.get('system')),
            # Optional NodeKind (contracts/desktop_node_v1); absent keeps the
            # previous value, routing reads absent as computer.
            'kind': (meta['kind'] if meta.get('kind') in ('computer', 'server')
                     else prev.get('kind')),
            'user_id': owner,
            'key_id': str(key_id or ''),
            'registered_at': prev.get('registered_at') or now,
            'last_seen': now,
            'connection_epoch': epoch,
            'connected_at': connected_at,
        }
        for command_id in _agents[agent_id]['active_command_ids']:
            command = command_queue.get(command_id)
            if (command and command.get('user_id') == owner
                    and command.get('claimed_agent_id') == agent_id):
                command['delivery_acknowledged'] = True
        _last_poll[0] = now
        if reconnected:
            connection = {'agent_id': agent_id, 'user_id': owner,
                          'name': _agents[agent_id]['name'],
                          'connection_epoch': epoch, 'connected_at': connected_at}
            _liveness_wake_locked()
    if connection is not None:
        _notify_devices_changed(owner)
    return connection


def _public_agent(agent):
    return {key: value for key, value in agent.items()
            if key not in ('active_command_ids', 'execution_slots', 'protocol_version',
                           'connection_epoch', 'connected_at', 'lost_epoch')}


def online_agents() -> list:
    """Registry agents whose heartbeat is inside the liveness window.

    An unreachable remote owner reads as no reachable device.
    """
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            return relay.call('online_agents', {})
        except RelayUnavailable as exc:
            _relay_unavailable('online_agents', exc)
            return []
    now = time.time()
    with command_queue_lock:
        return [_public_agent(a) for a in _agents.values()
                if now - a['last_seen'] < _CONNECTED_WINDOW_S]


def list_agents(user_id=None) -> list:
    """All known agents with an ``online`` flag (status endpoint).

    ``user_id`` (RWA P4a): when given, only agents registered by that
    bridge caller are returned — a tenant must never see another tenant's
    machines on a relay deployment. ``None`` = unfiltered (operator view).
    Raises ``RelayUnavailable`` when the selected remote owner is unreachable,
    so a caller can say so instead of reporting an empty fleet.
    """
    relay = _relay()
    if relay is not None:
        return relay.call('list_agents', {'user_id': None if user_id is None else str(user_id)})
    now = time.time()
    with command_queue_lock:
        out = [dict(_public_agent(a), online=(now - a['last_seen']) < _CONNECTED_WINDOW_S)
               for a in _agents.values()]
    if user_id is not None:
        out = [a for a in out if (a.get('user_id') or '') == (user_id or '')]
    return out


def online_connection(agent_id, user_id) -> dict | None:
    """The live connection of one owner's device, or None while it is offline.

    Same shape as the connection :func:`register_agent` returns at the online
    transition; another owner's device is never visible.
    """
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            return relay.call('online_connection', {'agent_id': str(agent_id),
                                                    'user_id': str(user_id or '')})
        except RelayUnavailable as exc:
            _relay_unavailable('online_connection', exc)
            return None
    now = time.time()
    with command_queue_lock:
        agent = _agents.get(agent_id)
        if (not agent or agent['user_id'] != str(user_id or '')
                or now - agent['last_seen'] >= _CONNECTED_WINDOW_S):
            return None
        return {'agent_id': agent_id, 'user_id': agent['user_id'], 'name': agent['name'],
                'connection_epoch': agent['connection_epoch'],
                'connected_at': agent['connected_at']}


def _online_ids_locked(user_id=None) -> set:
    """Online registry ids — optionally scoped to one bridge user.

    The single-agent fallback counts only the CALLER's own endpoints
    (RWA P4a): other tenants' agents must not make an unaddressed command
    look multi-target, nor make it look deliverable.
    """
    now = time.time()
    return {aid for aid, a in _agents.items()
            if now - a['last_seen'] < _CONNECTED_WINDOW_S
            and (user_id is None
                 or (a.get('user_id') or '') == (user_id or ''))}


def _deliverable(cmd, agent_id, online_ids, poller_user='') -> bool:
    """Route one owner-scoped command to its target or sole online agent."""
    if (cmd.get('user_id') or '') != (poller_user or ''):
        return False
    claimed = cmd.get('claimed_agent_id')
    if claimed:
        return claimed == agent_id
    target = cmd.get('target_agent_id')
    if target:
        return target == agent_id
    return len(online_ids) == 1 and agent_id in online_ids


def device_summary(agent) -> str:
    """One model-facing line: how to address a device and which shell it runs."""
    label = f"{agent.get('name') or agent['agent_id']} (agent {agent['agent_id']})"
    system = agent.get('system')
    if not system:
        return (f"{label}: platform {agent.get('platform') or 'unknown'}, "
                'system facts not reported (older agent)')
    parts = [' '.join(part for part in (system['os'], system.get('release')) if part)]
    if system.get('arch'):
        parts.append(system['arch'])
    shells = system['shells']
    parts.append(f"default shell {shells['default']} "
                 f"(available: {', '.join(shells['available'])})")
    return f"{label}: {', '.join(parts)}"


def _devices_listing(agents) -> str:
    if not agents:
        return 'None of your devices is online.'
    ordered = sorted(agents, key=lambda a: ((a.get('name') or '').casefold(), a['agent_id']))
    return 'Your online devices:\n' + '\n'.join(f' - {device_summary(a)}' for a in ordered)


def match_devices(agents, selector: str) -> list:
    """Devices a selector names: the exact agent ID, else a case-insensitive name.

    ``agents`` must already be filtered to one owner. Shared by desktop tools
    and ``await_device`` so both resolve a selector identically.
    """
    return ([agent for agent in agents if agent['agent_id'] == selector]
            or [agent for agent in agents
                if (agent.get('name') or '').casefold() == selector.casefold()])


def resolve_device_target(selector: str, user_id) -> tuple[dict | None, str | None]:
    """Resolve a desktop tool's ``device`` among one owner's devices.

    Returns ``(agent, None)`` for one online match, else ``(agent_or_None,
    error)``; the agent accompanies the error only when it matched but is
    offline, so the caller can offer a wait for it. Another owner's devices are
    filtered out before matching and never appear in an error.
    """
    from lib.desktop.bridge_relay import RelayUnavailable
    try:
        agents = list_agents(user_id=str(user_id or ''))
    except RelayUnavailable as exc:
        return None, str(exc)
    online = [agent for agent in agents if agent['online']]
    matches = match_devices(agents, selector)
    if len(matches) > 1:
        names = ', '.join(f"{a.get('name') or a['agent_id']} (agent {a['agent_id']})"
                          for a in matches)
        return None, (f'Several of your devices match {selector!r}: {names}. '
                      'Retry with device set to one agent ID.')
    if not matches:
        return None, (f'None of your devices has the agent ID or name {selector!r}. '
                      + _devices_listing(online))
    agent = matches[0]
    if not agent['online']:
        return agent, (f"Device {agent.get('name') or agent['agent_id']} "
                       f"(agent {agent['agent_id']}) is offline. " + _devices_listing(online))
    return agent, None


def _addressing_enqueue_error(target_agent_id, user_id=''):
    """Validate a to-be-enqueued command against the online-agent set.

    Returns an error string when the command must NOT be queued, else None:
    addressed → the target agent must be online AND belong to the caller's
    bridge user; unaddressed with more than one of the caller's endpoints
    online is refused. Other owners' agents are invisible.
    """
    user_id = str(user_id or '')
    online = [a for a in online_agents()
              if (a.get('user_id') or '') == user_id]
    if target_agent_id:
        if not any(a['agent_id'] == target_agent_id for a in online):
            return (f'target desktop agent {target_agent_id!r} is not online '
                    f'for this bridge user ({len(online)} own agent(s) online)')
        return None
    n = len(online)
    if n > 1:
        return (f'{n} of your desktop devices are online and the command names none, '
                'so it was not sent. Retry with device set to one agent ID or name '
                '(target_agent_id for direct bridge callers). ' + _devices_listing(online))
    return None


def _tier_enqueue_error(cmd_type, params, target_agent_id, user_id=''):
    """The one Home permission boundary (contracts/device_routing_v1.schema.json).

    The command's tier must be in the target's effective grant: the owner's
    Home ceiling for that node (absent = read-only, default deny) AND the tiers
    the node declared in its latest poll frame. The node re-checks the same
    intersection from the command's ``home_tiers`` before executing.
    """
    user_id = str(user_id or '')
    online = [a for a in online_agents() if (a.get('user_id') or '') == user_id]
    agent = next((a for a in online if a['agent_id'] == target_agent_id), None) \
        if target_agent_id else (online[0] if len(online) == 1 else None)
    if agent is None:
        return None
    return _permission_refusal(cmd_type, params, agent, user_id)


def _permission_refusal(cmd_type, params, agent, user_id):
    from lib.desktop import device_policy
    from lib.desktop_wire.node_protocol import required_tier, tier_refusal
    label = f"device {agent.get('name') or agent['agent_id']} (agent {agent['agent_id']})"
    device = device_policy.device_tiers(agent.get('capabilities'))
    refusal = tier_refusal(cmd_type, params or {}, device, node_label=label)
    if refusal:
        return 'permission_denied: ' + refusal
    tier = required_tier(cmd_type, params or {})
    home = device_policy.home_tiers(user_id, agent['agent_id'])
    if tier not in (None, 'control') and not home.get(tier):
        return (f'permission_denied: {cmd_type} needs the {tier} permission, which this Home '
                f'does not allow on {label} (new devices start read-only). The owner can '
                'raise the Home ceiling on the Devices page '
                f"(PUT /api/v4/devices/{agent['agent_id']}/ceiling).")
    return None


def _home_tiers_for(cmd, agent_id):
    """The Home ceiling a claimed command carries to the node for its own re-check."""
    from lib.desktop import device_policy
    return device_policy.home_tiers(cmd.get('user_id') or '', agent_id)


def disconnect_credential(key_id, user_id) -> list:
    """Drop one owner's nodes polling with a revoked credential.

    Removes their registry entries and settles their queued or claimed
    commands with a typed failure, so a revoked node disappears at once
    instead of lingering until the liveness window lapses. Returns the
    removed agent IDs.
    """
    key_id, user_id = str(key_id or ''), str(user_id or '')
    if not key_id:
        return []
    with command_queue_lock:
        removed = [aid for aid, a in _agents.items()
                   if a.get('key_id') == key_id and a.get('user_id') == user_id]
        for aid in removed:
            del _agents[aid]
        for cmd in command_queue.values():
            target = cmd.get('claimed_agent_id') or cmd.get('target_agent_id')
            if target in removed and (cmd.get('user_id') or '') == user_id \
                    and not cmd['event'].is_set():
                cmd['error'] = 'device credential revoked; the device was disconnected'
                cmd['event'].set()
    if removed:
        logger.info('[Desktop] revoked credential %s disconnected %d device(s)',
                    key_id, len(removed))
    return removed


def settle_lost_claims(agent_id, user_id, active_command_ids) -> list:
    """Settle one node's claimed commands its new process cannot report.

    A node process that restarted (a new ``boot_id``, lib/desktop/
    node_registry.py) lost its predecessor's running commands and their
    unsent results with it. Commands the predecessor claimed and the new
    process does not list as active fail now with a typed, unconfirmed
    outcome instead of waiting forever. Never replayed. Returns their IDs.
    """
    agent_id, user_id = str(agent_id or ''), str(user_id or '')
    active = set(active_command_ids or ())
    settled = []
    with command_queue_lock:
        for cmd in command_queue.values():
            if (cmd.get('claimed_agent_id') == agent_id and (cmd.get('user_id') or '') == user_id
                    and cmd['id'] not in active and not cmd['event'].is_set()):
                cmd['error'] = ('the device restarted while running this command; its outcome '
                                'is unknown and it was not run again')
                cmd['event'].set()
                settled.append(cmd['id'])
    if settled:
        logger.info('[Desktop] device %s restarted: settled %d lost command(s)', agent_id, len(settled))
    return settled


def _device_wait_scope(cmd):
    """Publish a queued command to the owning task's liveness judge, when known."""
    from contextlib import nullcontext
    from lib.project_mod.tool_admission import cancellation as current
    control = current()
    task = control.task if control is not None else None
    if not isinstance(task, dict):
        return nullcontext()
    from lib.tasks_pkg.explained_wait import device_wait
    return device_wait(task, cmd)


def send_desktop_command(cmd_type, params=None, timeout=None, target_agent_id=None,
                         user_id='', cmd_id=None, ttl=None, cancellation=None):
    """Queue a command and wait for settlement or an explicit caller stop.

    ``target_agent_id`` routes the command to one registered agent; when
    omitted, the single-agent selection applies and with
    several agents online the command is REFUSED up front — never
    delivered to a lucky poller. ``user_id`` (RWA P4a) scopes the command
    to agents registered by the same bridge user; it stays INTERNAL (never
    projected onto the wire). ``ttl`` sets an explicit pickup deadline; omitted
    TTLs grant no queue-age refusal. Completed replay/transport retention is separate.
    """
    if timeout is not None and (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout < 0):
        raise ValueError('Desktop wait timeout must be nonnegative and finite or None')
    if cancellation is not None and cancellation.is_set():
        return None, 'Desktop command cancelled before dispatch'
    relay = _relay()
    if relay is not None:
        return _send_via_relay(relay, cmd_type, params, timeout, target_agent_id,
                               user_id, cmd_id, ttl, cancellation)
    attached = _attach_existing(cmd_id, cmd_type, target_agent_id, user_id)
    if isinstance(attached, str):
        return None, attached
    if attached is not None:
        cmd, event = attached, attached['event']
    else:
        err = (_addressing_enqueue_error(target_agent_id, user_id=user_id)
               or _tier_enqueue_error(cmd_type, params, target_agent_id, user_id=user_id))
        if err:
            logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
            return None, err
        cmd_id = cmd_id or str(uuid.uuid4())
        event = threading.Event()
        cmd = {
            'id': cmd_id,
            'type': cmd_type,
            'params': params or {},
            'created_at': time.time(),
            'event': event,
            'result': None,
            'error': None,
        }
        if target_agent_id:
            cmd['target_agent_id'] = target_agent_id
        cmd['user_id'] = str(user_id or '')
        if ttl:
            cmd['ttl'] = float(ttl)

        with command_queue_lock:
            command_queue[cmd_id] = cmd
        _wake_async_waiters(cmd)

    deadline = time.monotonic() + timeout if timeout is not None else None
    cancelled = False
    with _device_wait_scope(cmd):
        while not event.is_set():
            if cancellation is not None and cancellation.is_set():
                cancelled = True
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            # Poll only a supplied cancellation signal; otherwise block on settlement.
            interval = remaining
            if cancellation is not None:
                interval = (CANCEL_OBSERVATION_SECONDS if remaining is None
                            else min(CANCEL_OBSERVATION_SECONDS, remaining))
            event.wait(timeout=interval)

    with command_queue_lock:
        cmd = command_queue.get(cmd_id, cmd)
        if not event.is_set() and cmd.get('claimed_agent_id'):
            # The caller stopped waiting; remote work still owns settlement.
            cmd['caller_detached'] = True
        else:
            command_queue.pop(cmd_id, None)

    if not event.is_set():
        if cancelled:
            return None, 'Desktop command cancelled; remote completion is unconfirmed'
        return None, 'Desktop agent timeout — is the agent running?'

    return cmd.get('result'), cmd.get('error')


def _attach_existing(cmd_id, cmd_type, target_agent_id, user_id):
    """The queued command a repeated idempotency key names, else None.

    A tool call's command id is its idempotency key (``lib.desktop.routing``):
    a replayed call waits for the command already queued or running instead of
    sending it again. Returns an error string when the key belongs to another
    owner, device or command type.
    """
    if not cmd_id:
        return None
    with command_queue_lock:
        cmd = command_queue.get(cmd_id)
        if cmd is None:
            return None
        if (cmd.get('user_id') != str(user_id or '') or cmd['type'] != cmd_type
                or (target_agent_id or None) != cmd.get('target_agent_id')):
            return 'Desktop command id is already used by another command'
        cmd.pop('caller_detached', None)
        return cmd


# ── Liveness watcher (bridge owner) ───────────────────────────────────────
# One daemon thread wakes exactly when the earliest online device's window
# lapses (last_seen + _CONNECTED_WINDOW_S), so a lost device is noticed
# without a polling interval. It settles the lost device's unsettled commands
# with device_unavailable (never retried elsewhere) and publishes the
# devices.changed wake hint; register_agent publishes it on reconnect.
_liveness_condition = threading.Condition(command_queue_lock)
_liveness_thread = [None]


def _liveness_wake_locked() -> None:
    """Start the watcher on first use, or make it recompute its deadline."""
    thread = _liveness_thread[0]
    if thread is None or not thread.is_alive():
        thread = threading.Thread(target=_liveness_loop, name='desktop-liveness', daemon=True)
        _liveness_thread[0] = thread
        thread.start()
    _liveness_condition.notify_all()


def _expire_lost_agents_locked(now) -> list:
    """Settle commands of devices whose window lapsed this epoch; returns their owners."""
    owners = []
    for agent_id, agent in _agents.items():
        if now - agent['last_seen'] < _CONNECTED_WINDOW_S:
            continue
        if agent.get('lost_epoch') == agent.get('connection_epoch'):
            continue
        agent['lost_epoch'] = agent.get('connection_epoch')
        owners.append(agent['user_id'])
        label = f"{agent.get('name') or agent_id} (agent {agent_id})"
        for cmd_id, cmd in list(command_queue.items()):
            if cmd['event'].is_set() or cmd.get('user_id') != agent['user_id']:
                continue
            if cmd.get('claimed_agent_id') == agent_id:
                cmd['error'] = (
                    f'device_unavailable: device {label} stopped answering (no poll for '
                    f'{_CONNECTED_WINDOW_S} s) while it held this command. Its outcome is '
                    'unknown; it was not retried on any device. Check the device before '
                    'repeating the action.')
            elif not cmd.get('claimed_agent_id') and cmd.get('target_agent_id') == agent_id:
                cmd['error'] = (
                    f'device_unavailable: device {label} went offline before it took this '
                    'command; it was withdrawn and will not run.')
            else:
                continue
            cmd['device_lost'] = True
            cmd['event'].set()
            if cmd.get('fire_and_forget') or cmd.get('caller_detached'):
                command_queue.pop(cmd_id, None)
        logger.info('[Desktop] device %s lost; its unsettled commands were settled', agent_id[:8])
    return owners


def _liveness_loop() -> None:
    while True:
        with command_queue_lock:
            owners = _expire_lost_agents_locked(time.time())
        for owner in dict.fromkeys(owners):
            _notify_devices_changed(owner)
        with command_queue_lock:
            now = time.time()
            deadlines = [agent['last_seen'] + _CONNECTED_WINDOW_S for agent in _agents.values()
                         if agent.get('lost_epoch') != agent.get('connection_epoch')]
            if not deadlines:
                _liveness_condition.wait()
            else:
                remaining = min(deadlines) - now
                if remaining > 0:
                    _liveness_condition.wait(timeout=remaining)


def _notify_devices_changed(owner) -> None:
    from lib.desktop.device_policy import notify_devices_changed
    notify_devices_changed(owner)


def _send_via_relay(relay, cmd_type, params, timeout, target_agent_id, user_id, cmd_id,
                    ttl, cancellation):
    """Forward one blocking command to the remote owner, keeping the local device wait.

    The owner enforces addressing, tiers and settlement; this process keeps
    the task's device-wait publication so the liveness judge (which asks the
    owner through :func:`command_active`) still sees whose silence it is.
    """
    from lib.desktop.bridge_relay import RelayUnavailable, is_cancelled
    cmd_id = cmd_id or str(uuid.uuid4())
    try:
        with _device_wait_scope({'id': cmd_id, 'type': cmd_type}):
            outcome = relay.call('send_command', {
                'cmd_type': cmd_type, 'params': params or {}, 'timeout': timeout,
                'target_agent_id': target_agent_id, 'user_id': str(user_id or ''),
                'cmd_id': cmd_id, 'ttl': ttl,
            }, wait_seconds=timeout, cancel_event=cancellation)
    except RelayUnavailable as exc:
        return None, str(exc)
    if is_cancelled(outcome):
        return None, 'Desktop command cancelled; remote completion is unconfirmed'
    return outcome[0], outcome[1]


def enqueue_desktop_command(cmd_type, params=None, target_agent_id=None,
                            user_id='', cmd_id=None, ttl=None, stream_limits=None,
                            fire_and_forget=False):
    """Non-blocking enqueue: queue a command and return its id IMMEDIATELY.

    ``send_desktop_command`` blocks for the final result; streamed commands
    (egress_http_stream) and fire-and-forget cancels (egress_cancel) need
    the command on the wire NOW, with the caller consuming stream frames /
    ignoring the result instead of waiting. Returns ``(cmd_id, error)`` —
    error is the addressing refusal (same rules as send_desktop_command).
    """
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            got = relay.call('enqueue_command', {
                'cmd_type': cmd_type, 'params': params or {},
                'target_agent_id': target_agent_id, 'user_id': str(user_id or ''),
                'cmd_id': cmd_id, 'ttl': ttl, 'stream_limits': stream_limits,
                'fire_and_forget': bool(fire_and_forget)})
        except RelayUnavailable as exc:
            return None, str(exc)
        return got[0], got[1]
    err = (_addressing_enqueue_error(target_agent_id, user_id=user_id)
           or _tier_enqueue_error(cmd_type, params, target_agent_id, user_id=user_id))
    if err:
        logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
        return None, err
    if stream_limits is not None:
        ceilings = {'bytes': 16 * 1024 * 1024, 'frames': 4096, 'frame_bytes': 1024 * 1024}
        if not isinstance(stream_limits, dict) or set(stream_limits) != set(ceilings) or any(
                type(stream_limits[key]) is not int or not 1 <= stream_limits[key] <= maximum
                for key, maximum in ceilings.items()):
            return None, 'Invalid desktop stream limits'
    cmd_id = cmd_id or str(uuid.uuid4())
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': params or {},
        'created_at': time.time(),
        'event': threading.Event(),
        'result': None,
        'error': None,
    }
    if target_agent_id:
        cmd['target_agent_id'] = target_agent_id
    cmd['user_id'] = str(user_id or '')
    if ttl:
        cmd['ttl'] = float(ttl)
    if stream_limits is not None:
        cmd['stream_limits'] = dict(stream_limits)
    if fire_and_forget:
        cmd['fire_and_forget'] = True
    with command_queue_lock:
        command_queue[cmd_id] = cmd
    _wake_async_waiters(cmd)
    return cmd_id, None


def consume_desktop_command(cmd_id, *, target_agent_id, user_id):
    """Read an addressed command and drain only its contiguous bounded frames.

    The consumed watermark deduplicates retransmitted frames without retaining
    artifact bytes. Out-of-order frames wait inside the declared byte/count
    window. Existing replay readers retain their non-consuming behavior.
    """
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            got = relay.call('consume_command', {'cmd_id': str(cmd_id),
                                                 'target_agent_id': target_agent_id,
                                                 'user_id': str(user_id)})
        except RelayUnavailable as exc:
            _relay_unavailable('consume_command', exc)
            return None
        if got is not None:
            got['frames'] = [tuple(frame) for frame in got['frames']]
        return got
    with command_queue_lock:
        cmd = command_queue.get(cmd_id)
        if (not cmd or cmd.get('target_agent_id') != target_agent_id
                or cmd.get('user_id') != str(user_id) or not cmd.get('stream_limits')):
            return None
        frames = []
        stream = _streams.get(cmd_id)
        done = False
        if stream is not None:
            sequence = stream['consumed_seq'] + 1
            while sequence in stream['chunks']:
                kind, data = stream['chunks'].pop(sequence)
                stream['bytes'] -= len(data.encode('utf-8'))
                frames.append((sequence, kind, data))
                stream['consumed_seq'] = sequence
                sequence += 1
            done = stream.get('done_seq') == stream['consumed_seq']
        return {'frames': frames, 'stream_done': done,
                'settled': cmd['event'].is_set(), 'result': cmd.get('result'),
                'error': cmd.get('stream_error') or cmd.get('error')}


def release_desktop_command(cmd_id, *, target_agent_id, user_id):
    """Retire one owner's command and reconstructible stream on every exit."""
    relay = _relay()
    if relay is not None:
        from lib.desktop.bridge_relay import RelayUnavailable
        try:
            return bool(relay.call('release_command', {'cmd_id': str(cmd_id),
                                                       'target_agent_id': target_agent_id,
                                                       'user_id': str(user_id)}))
        except RelayUnavailable as exc:
            _relay_unavailable('release_command', exc)
            return False
    with command_queue_lock:
        cmd = command_queue.get(cmd_id)
        if (not cmd or cmd.get('target_agent_id') != target_agent_id
                or cmd.get('user_id') != str(user_id)):
            return False
        command_queue.pop(cmd_id, None)
        _streams.pop(cmd_id, None)
        return bool(cmd.get('claimed_agent_id'))


def resolve_results(results, *, agent_id: str, user_id: str) -> int:
    """Resolve agent-returned command results into the queue. Returns count."""
    resolved = 0
    for r in results or []:
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        with command_queue_lock:
            cmd = command_queue.get(cmd_id)
            if _command_owned_by_poller(cmd, agent_id, user_id):
                if cmd['event'].is_set():
                    continue  # A repeated delivery cannot replace first settlement.
                cmd['result'] = r.get('result')
                cmd['error'] = r.get('error')
                cmd['event'].set()
                if cmd.get('caller_detached'):
                    command_queue.pop(cmd_id, None)
                resolved += 1
            elif cmd is not None:
                logger.warning(
                    '[Desktop] rejected result for unowned command %s from '
                    'agent %s', cmd_id[:8], agent_id[:8])
    return resolved


def take_pending_commands(*, agent_id: str, user_id: str) -> list:
    """Collect commands awaiting THIS poller, expiring stale ones.

    ``agent_id`` and ``user_id`` identify the authenticated poller. Commands
    are claimed before projection so only that agent may settle them.
    """
    from lib.desktop import device_policy
    from lib.desktop_wire.node_protocol import required_tier
    pending = []
    # Read before the queue lock: a policy cache miss reads storage.
    home = device_policy.home_tiers(user_id, agent_id)
    now = time.time()
    with command_queue_lock:
        online_ids = _online_ids_locked(user_id)
        agent = _agents.get(agent_id) or {}
        negotiated = agent.get('protocol_version') == protocol_version()
        active = agent.get('active_command_ids') or set()
        ordered = sorted(command_queue.items(),
                         key=lambda entry: entry[1]['type'] not in ('egress_cancel', 'paper_build_cancel'))
        for cmd_id, cmd in ordered:
            if cmd['event'].is_set():
                continue  # already resolved
            if (cmd.get('ttl') and now - cmd['created_at'] > cmd['ttl']
                    and not _command_active_locked(cmd, now)):
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
                if cmd.get('fire_and_forget') or cmd.get('caller_detached'):
                    command_queue.pop(cmd_id, None)
                continue
            if not _deliverable(cmd, agent_id, online_ids, user_id):
                continue
            control = cmd['type'] in ('egress_cancel', 'paper_build_cancel')
            if len(pending) >= limit('command_batch'):
                continue
            if negotiated and not control:
                if cmd_id in active or cmd.get('delivery_acknowledged'):
                    continue
            tier = required_tier(cmd['type'], cmd['params'])
            if not cmd.get('claimed_agent_id') and tier not in (None, 'control') \
                    and not home.get(tier):
                # The Home ceiling was lowered after this command was queued:
                # it never reaches the node. (The node re-checks its own
                # switch and the home_tiers it receives.)
                cmd['error'] = (f"permission_denied: {cmd['type']} needs the {tier} "
                                'permission, which the Home no longer allows on this '
                                'device; it was withdrawn before the device received it.')
                cmd['event'].set()
                if cmd.get('fire_and_forget') or cmd.get('caller_detached'):
                    command_queue.pop(cmd_id, None)
                continue
            cmd['claimed_agent_id'] = agent_id
            if cmd.get('fire_and_forget'):
                command_queue.pop(cmd_id, None)
            wire = {
                'id': cmd_id,
                'type': cmd['type'],
                'params': cmd['params'],
                'home_tiers': dict(home),
            }
            if cmd.get('target_agent_id'):
                wire['target_agent_id'] = cmd['target_agent_id']
            if negotiated:
                wire['target_agent_id'] = agent_id
                if cmd.get('ttl'):
                    wire['expires_at'] = cmd['created_at'] + cmd['ttl']
                    wire['deadline_enforced'] = True
                else:
                    wire['expires_at'] = cmd.setdefault('replay_expires_at', now + _COMMAND_TTL_S)
                    wire['deadline_enforced'] = False
            pending.append(wire)
    return pending


async def take_pending_commands_async(
    *, agent_id: str, user_id: str, timeout: float | None = None,
) -> list:
    """Async long-poll variant of take_pending_commands for the async route.

    Awaits an asyncio.Event (woken cross-thread by send_desktop_command)
    instead of returning immediately, so the agent picks up a command the
    instant it is queued — without pinning the worker thread for the wait.
    Poller identity is threaded through every re-check.
    """
    if timeout is None:
        timeout = POLL_WAIT_TIMEOUT
    pending = take_pending_commands(agent_id=agent_id, user_id=user_id)
    if pending:
        return pending

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    waiter = {'loop': loop, 'event': event, 'agent_id': agent_id, 'user_id': user_id}
    with _async_waiters_lock:
        _async_waiters.append(waiter)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event.clear()
            pending = take_pending_commands(
                agent_id=agent_id, user_id=user_id)
            if pending:
                return pending
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError as e:
                logger.debug('[Desktop] async poll slice elapsed, re-checking queue: %s', e)
                pass
        return take_pending_commands(agent_id=agent_id, user_id=user_id)
    finally:
        with _async_waiters_lock:
            try:
                _async_waiters.remove(waiter)
            except ValueError as e:
                logger.debug('[Desktop] async waiter already deregistered: %s', e)


def pending_commands_count() -> int:
    """Number of queued commands not yet resolved."""
    with command_queue_lock:
        return sum(1 for c in command_queue.values() if not c['event'].is_set())


def format_desktop_result(cmd_type, result):
    """Format a desktop agent result for the LLM tool response."""
    if result is None:
        return '(no output)'
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get('file_receipts_version') == 1 and 'output' in result:
            return format_desktop_result(cmd_type, result['output'])
        if result.get('error'):
            return f"Error: {result['error']}"
        if cmd_type == 'project_find_files' \
                and isinstance(result.get('files'), str):
            return result['files']
        if cmd_type == 'project_list_dir' \
                and isinstance(result.get('entries'), list):
            lines = [f"Directory: {result.get('path') or '.'}"]
            output_chars = len(lines[0])
            shown = 0
            for entry in result['entries']:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '')
                if not name:
                    continue
                entry_type = entry.get('type')
                is_dir = entry_type == 'dir'
                size = entry.get('size')
                rendered = f'  {name}/' if is_dir else f'  {name}'
                if entry_type in {'symlink', 'special'}:
                    rendered += f' [{entry_type}]'
                elif not is_dir and isinstance(size, int):
                    rendered += f' ({size} bytes)'
                if output_chars + len(rendered) + 1 > 64_000:
                    break
                lines.append(rendered)
                output_chars += len(rendered) + 1
                shown += 1
            if result.get('truncated') or shown < len(result['entries']):
                lines.append(
                    f'… [listing truncated after {shown} entries; '
                    'use a narrower relative path]')
            return '\n'.join(lines)
        # Screenshot results ride the multimodal __screenshot__ lane (the same
        # contract as browser_screenshot / inspect_image) so a vision model
        # actually receives the pixels; inlining the base64 as text would cost
        # tens of KB the model cannot decode. The agent emits ``base64``;
        # ``image_base64`` is accepted for forward/backward compatibility.
        if cmd_type == 'desktop_screenshot' and (
                isinstance(result.get('base64'), str)
                or isinstance(result.get('image_base64'), str)):
            b64 = result.get('base64') or result.get('image_base64')
            fmt = str(result.get('format') or 'jpeg')
            view_w, view_h = result.get('width', '?'), result.get('height', '?')
            real_w = result.get('real_width') or view_w
            real_h = result.get('real_height') or view_h
            size_bytes = int(result.get('size_bytes') or 0)
            scaled_note = (
                f', screen {real_w}×{real_h}'
                if (real_w, real_h) != (view_w, view_h) else '')
            return {
                '__screenshot__': True,
                'dataUrl': f'data:image/{fmt};base64,{b64}',
                'format': fmt,
                'originalSize': size_bytes,
                'compressedSize': size_bytes,
                'compressionApplied': False,
                '_text_fallback': (
                    f'Desktop screenshot captured (view {view_w}×{view_h}'
                    f'{scaled_note}). The image is displayed above — analyze '
                    'it visually; desktop_gui_action coordinates are mapped '
                    'back to real screen pixels automatically.'
                ),
            }
        if cmd_type == 'desktop_list_files' \
                and isinstance(result.get('entries'), list):
            # Dedicated table: the generic dict formatter below would collapse
            # >20 entries into "entries: [N items]" — a read tool returning no
            # data — or dump Python repr noise for smaller listings.
            path_label = result.get('path') or '.'
            entries = [e for e in result['entries'] if isinstance(e, dict)]
            lines = [f'Directory: {path_label} ({len(entries)} entries)']
            output_chars = len(lines[0])
            shown = 0
            for entry in entries:
                name = str(entry.get('name') or '')
                if not name:
                    continue
                is_dir = entry.get('type') == 'dir'
                size = entry.get('size')
                rendered = f'  {name}/' if is_dir else f'  {name}'
                if not is_dir and isinstance(size, int):
                    rendered += f' ({size:,} bytes)'
                if output_chars + len(rendered) + 1 > 64_000:
                    break
                lines.append(rendered)
                output_chars += len(rendered) + 1
                shown += 1
            total = result.get('total')
            if shown < len(entries) or (isinstance(total, int)
                                        and total > shown):
                lines.append(
                    f'… [listing truncated: showing {shown}'
                    + (f' of {total}' if isinstance(total, int) else '')
                    + '; narrow the path]')
            return '\n'.join(lines)
        if cmd_type == 'desktop_run_command' and 'exit_code' in result:
            # Dedicated render so agent-reported caps become visible markers
            # instead of silent data loss.
            parts = []
            stdout = str(result.get('stdout') or '')
            stderr = str(result.get('stderr') or '')
            if stdout:
                parts.append(f'stdout: {stdout}')
            if result.get('stdout_truncated'):
                parts.append(
                    f'[stdout truncated: {int(result["stdout_truncated"]):,} '
                    'chars total, showing first 100,000]')
            if stderr:
                parts.append(f'stderr: {stderr}')
            if result.get('stderr_truncated'):
                parts.append(
                    f'[stderr truncated: {int(result["stderr_truncated"]):,} '
                    'chars total, showing first 20,000]')
            parts.append(f'exit_code: {result["exit_code"]}')
            return '\n'.join(parts)
        # System info, process list, etc.
        parts = []
        for k, v in result.items():
            if isinstance(v, list) and len(v) > 20:
                parts.append(f'{k}: [{len(v)} items]')
            else:
                parts.append(f'{k}: {v}')
        return '\n'.join(parts)
    if isinstance(result, list):
        if len(result) == 0:
            return '(empty list)'
        # File listings
        lines = []
        for item in result[:200]:
            if isinstance(item, dict):
                name = item.get('name', str(item))
                is_dir = item.get('is_dir', False)
                size = item.get('size', '')
                prefix = '[DIR] ' if is_dir else '[FILE] '
                suffix = f'  ({size} bytes)' if size and not is_dir else ''
                lines.append(f'{prefix}{name}{suffix}')
            else:
                lines.append(str(item))
        if len(result) > 200:
            lines.append(f'... and {len(result) - 200} more items')
        return '\n'.join(lines)
    return str(result)


__all__ = [
    'command_active',
    'command_queue',
    'command_queue_lock',
    'consume_desktop_command',
    'device_summary',
    'match_devices',
    'resolve_device_target',
    'release_desktop_command',
    'format_desktop_result',
    'is_desktop_agent_connected',
    'last_poll_time',
    'get_command_stream',
    'list_agents',
    'online_agents',
    'pending_commands_count',
    'record_poll',
    'register_agent',
    'resolve_results',
    'resolve_streams',
    'send_desktop_command',
    'take_pending_commands',
    'take_pending_commands_async',
]
