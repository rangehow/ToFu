"""Private loopback relay from the resident execution host to the browser bridge owner.

Problem: in the managed split (``TOFU_PROCESS_ROLE=api`` + ``execution``) the
extension long-polls the API process, so the process-local queue/registry in
``lib.browser.queue._state`` lives there, while task tools and context
composition run in the execution process, whose registry is always empty.
Every model saw "no usable extension" and every browser command would have
been enqueued where no extension ever polls.

Decision: the API process stays the single bridge owner (it terminates the
extension's HTTP polls). It publishes a private endpoint record beside the
manager's other process records (``<project>/data/browser-bridge-relay.json``,
mode 0600, rotating bearer token); the execution process selects that owner
and ``lib.browser.queue`` forwards its owner-scoped worker verbs over it.

Alternatives considered: moving the registry into the execution host would
need the API to forward every poll and every status/panel read the other way;
a durable shared registry would turn transient liveness into stored state.

Transport (``FORMAT``): the shared private loopback relay
(``lib/runtime/loopback_relay.py``); the owner treats the caller's EOF as its
cancellation and withdraws a queued command exactly like a local
``cancel_event``. The desktop bridge uses the same transport
(``lib/desktop/bridge_relay.py``).

Entry points: ``configure_bridge_owner`` / ``stop_bridge_owner`` (process
services), ``selected_relay`` (queue facade), ``RelayServer``, ``RelayClient``.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib.runtime import loopback_relay
from lib.runtime.loopback_relay import RelayUnavailable, is_cancelled

FORMAT = 'tofu.browser-bridge-relay/v1'
RECORD_NAME = 'browser-bridge-relay.json'
_NAME = 'browser bridge'
# Registry reads answered at once from the owner's memory; a Turn keeps one
# connection for them (lib/runtime/loopback_relay.py, "Session verbs").
SESSION_METHODS = frozenset({'client_protocol', 'connected_clients', 'extension_connected',
                             'client_owner'})

__all__ = ['FORMAT', 'RECORD_NAME', 'RelayClient', 'RelayServer', 'RelayUnavailable',
           'configure_bridge_owner', 'is_cancelled', 'record_path', 'selected_relay',
           'stop_bridge_owner']


def record_path(project: str | os.PathLike) -> Path:
    return Path(project) / 'data' / RECORD_NAME


# ── Owner side (API process) ────────────────────────────────────────────

def _method_table():
    from lib.browser.queue import _dispatch, _registry

    def connected_clients(params, cancel):
        owner = params.get('owner_user_id')
        return _registry.get_connected_clients(
            owner_user_id=None if owner is None else str(owner))

    def extension_connected(params, cancel):
        return _registry.is_extension_connected(
            params['client_id'], owner_user_id=params['owner_user_id'])

    def client_owner(params, cancel):
        return _registry.client_owner_user_id(params['client_id'])

    def credit_hub_quota(params, cancel):
        return _registry.get_credit_hub_quota(owner_user_id=params['owner_user_id'])

    def wait_connected(params, cancel):
        return _registry.wait_for_connected_clients(
            owner_user_id=params['owner_user_id'], client_id=params.get('client_id'),
            timeout=float(params['timeout']), cancellation=cancel)

    def send_command(params, cancel):
        result, error = _dispatch.send_browser_command(
            params['cmd_type'], params.get('params') or {}, params['timeout'],
            client_id=params['client_id'], owner_user_id=params['owner_user_id'],
            cancel_event=cancel)
        return [result, error]

    def client_protocol(params, cancel):
        from lib.browser.protocol import client_protocol as local_client_protocol
        return local_client_protocol(params['client_id'])

    return {
        'client_protocol': client_protocol,
        'connected_clients': connected_clients,
        'extension_connected': extension_connected,
        'client_owner': client_owner,
        'credit_hub_quota': credit_hub_quota,
        'wait_connected': wait_connected,
        'send_command': send_command,
    }


class RelayServer(loopback_relay.RelayServer):
    """Loopback listener owned by the API process for the browser queue."""

    def __init__(self, path: Path):
        super().__init__(path, format=FORMAT, methods=_method_table(), name=_NAME,
                         session_methods=SESSION_METHODS)


# ── Caller side (execution process) ─────────────────────────────────────

class RelayClient(loopback_relay.RelayClient):
    """Forwards one queue verb per connection to the published bridge owner."""

    def __init__(self, path: Path):
        from lib.browser.queue._dispatch import CANCEL_OBSERVATION_SECONDS
        super().__init__(path, format=FORMAT, name=_NAME, session_methods=SESSION_METHODS,
                         cancel_observation_seconds=CANCEL_OBSERVATION_SECONDS)


_selection = loopback_relay.OwnerSelection(
    name=_NAME, record_path=record_path, make_server=RelayServer, make_client=RelayClient)


def selected_relay() -> RelayClient | None:
    """The remote bridge owner selected for this process, or None (local owner)."""
    return _selection.selected()


def configure_bridge_owner(*, process_role: str, environ) -> str:
    """Select this process's browser bridge owner from its process role.

    ``api`` publishes the relay for the queue it owns; ``execution`` and its
    shards forward to it; ``all`` (and embedders) keep the local in-process owner.
    """
    return _selection.configure(process_role=process_role, environ=environ)


def stop_bridge_owner(timeout: float = 2.0) -> bool:
    return _selection.stop(timeout)
