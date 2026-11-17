"""Private loopback relay from the resident execution host to the desktop bridge owner.

Problem: in the managed split (``TOFU_PROCESS_ROLE=api`` + ``execution``)
desktop nodes long-poll the API process (``routes/desktop.py``), so the
process-local registry, command queue and stream frames of
``lib.desktop.bridge`` live there, while desktop tools, the environment device
listing, ``await_device``, desktop egress and the liveness judge run in the
execution process, whose registry was always empty: Settings showed a node
online while tools saw none.

Decision: the API process stays the single bridge owner, exactly like the
browser bridge (``lib/browser/bridge_relay.py``): it publishes
``<project>/data/desktop-bridge-relay.json`` (mode 0600, rotating bearer
token) over the shared transport (``lib/runtime/loopback_relay.py``), and the
public verbs of ``lib.desktop.bridge`` forward to it when this process selected
the relay. Owner-side poll verbs (register, take, resolve, revoke) are never
relayed: only the process that terminates polls calls them.

Alternatives considered: as for the browser — moving the registry into the
execution host would invert every poll; a durable shared registry would turn
transient liveness into stored state.

Entry points: ``configure_bridge_owner`` / ``stop_bridge_owner`` (process
services), ``selected_relay`` (bridge facade), ``RelayServer``, ``RelayClient``.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib.runtime import loopback_relay
from lib.runtime.loopback_relay import RelayUnavailable, is_cancelled

FORMAT = 'tofu.desktop-bridge-relay/v1'
RECORD_NAME = 'desktop-bridge-relay.json'
_NAME = 'desktop bridge'

__all__ = ['FORMAT', 'RECORD_NAME', 'RelayClient', 'RelayServer', 'RelayUnavailable',
           'configure_bridge_owner', 'is_cancelled', 'record_path', 'selected_relay',
           'stop_bridge_owner']


def record_path(project: str | os.PathLike) -> Path:
    return Path(project) / 'data' / RECORD_NAME


# ── Owner side (API process) ────────────────────────────────────────────

def _method_table():
    from lib.desktop import bridge

    def list_agents(params, cancel):
        owner = params.get('user_id')
        return bridge.list_agents(user_id=None if owner is None else str(owner))

    def online_agents(params, cancel):
        return bridge.online_agents()

    def online_connection(params, cancel):
        return bridge.online_connection(params['agent_id'], params['user_id'])

    def connected(params, cancel):
        return bridge.is_desktop_agent_connected()

    def command_active(params, cancel):
        return bridge.command_active(params['cmd_id'], float(params['now']))

    def send_command(params, cancel):
        result, error = bridge.send_desktop_command(
            params['cmd_type'], params.get('params') or {}, timeout=params.get('timeout'),
            target_agent_id=params.get('target_agent_id'), user_id=params['user_id'],
            cmd_id=params['cmd_id'], ttl=params.get('ttl'), cancellation=cancel)
        return [result, error]

    def enqueue_command(params, cancel):
        cmd_id, error = bridge.enqueue_desktop_command(
            params['cmd_type'], params.get('params') or {},
            target_agent_id=params.get('target_agent_id'), user_id=params['user_id'],
            cmd_id=params.get('cmd_id'), ttl=params.get('ttl'),
            stream_limits=params.get('stream_limits'),
            fire_and_forget=bool(params.get('fire_and_forget')))
        return [cmd_id, error]

    def consume_command(params, cancel):
        return bridge.consume_desktop_command(
            params['cmd_id'], target_agent_id=params['target_agent_id'],
            user_id=params['user_id'])

    def release_command(params, cancel):
        return bridge.release_desktop_command(
            params['cmd_id'], target_agent_id=params['target_agent_id'],
            user_id=params['user_id'])

    def frames(params, cancel):
        return bridge.get_frames(params['cmd_id'], int(params.get('since_seq') or 0))

    return {
        'command_active': command_active,
        'connected': connected,
        'consume_command': consume_command,
        'enqueue_command': enqueue_command,
        'frames': frames,
        'list_agents': list_agents,
        'online_agents': online_agents,
        'online_connection': online_connection,
        'release_command': release_command,
        'send_command': send_command,
    }


class RelayServer(loopback_relay.RelayServer):
    """Loopback listener owned by the API process for the desktop bridge."""

    def __init__(self, path: Path):
        super().__init__(path, format=FORMAT, methods=_method_table(), name=_NAME)


# ── Caller side (execution process) ─────────────────────────────────────

class RelayClient(loopback_relay.RelayClient):
    """Forwards one bridge verb per connection to the published bridge owner."""

    def __init__(self, path: Path):
        from lib.desktop.bridge import CANCEL_OBSERVATION_SECONDS
        super().__init__(path, format=FORMAT, name=_NAME,
                         cancel_observation_seconds=CANCEL_OBSERVATION_SECONDS)


_selection = loopback_relay.OwnerSelection(
    name=_NAME, record_path=record_path, make_server=RelayServer, make_client=RelayClient)


def selected_relay() -> RelayClient | None:
    """The remote bridge owner selected for this process, or None (local owner)."""
    return _selection.selected()


def configure_bridge_owner(*, process_role: str, environ) -> str:
    """Select this process's desktop bridge owner from its process role."""
    return _selection.configure(process_role=process_role, environ=environ)


def stop_bridge_owner(timeout: float = 2.0) -> bool:
    return _selection.stop(timeout)
