"""Execution shards reach the execution host's MCP servers over a private loopback relay.

Problem: an MCP bridge is process-local, and every execution shard
(``serverctl_pkg/execution_shards.py``) booted the whole application, so each
ran its own MCP auto-connect. On the live generation 9840324c (2026-11-17) 53
processes each started up to 7 servers; their concurrent ``npx`` launches
raced on the shared npm cache (``npm error code ENOENT ... syscall write``),
leaving github/12306-train missing in 16/14 processes, and any job placed in
one of those shards lacked those tools. MCP processes, memory and boot work
grew with the shard count.

Decision: the execution host (the owner of tool execution) owns the one MCP
bridge of the execution plane; a shard runs no MCP auto-connect and its
``get_bridge(owner)`` returns a :class:`RelayedMCPBridge`. Like the browser
and desktop bridges (``lib/runtime/loopback_relay.py``), the owner publishes
``<project>/data/mcp-bridge-relay.json`` (mode 0600, rotating bearer token)
and the shard forwards over it:

- ``catalog``: the owner's catalog identity (bridge incarnation + catalog
  generation) and live server names; the full index only when the shard's
  mirror is older. Every catalog read in a shard asks once, so a shard sees
  exactly the owner's connected servers, per-tool grants and generations.
- ``call_tool``: one call per connection. The shard's EOF (its job stopped or
  the shard died) cancels the owner's SDK request. Failures keep their
  category (cancelled, timeout, transport, invalid, tool error); a lost
  owner is a transport failure with an unknown outcome and is never replayed.
- ``list_servers``: the owner's live server rows.

Grant enforcement is unchanged: the shard's task gateway applies the task's
grants to the owner's catalog, and the owner's bridge refuses a tool the
owner disabled. Management (connect, disconnect, per-tool filter) stays with
the API host's routes and the execution host's reconcile worker
(docs/modules/tools_execution.md, "MCP management reaches the execution host").

Alternatives considered: a per-process npm cache would keep N copies of
every server and only hide the race; staggering shard launches adds an
interval knob and still runs N server sets; moving the API host's
management bridge here too would make the settings UI depend on the
execution host's availability (rejected in that same record).

Entry points: ``configure_mcp_owner`` / ``stop_mcp_owner`` (process
services), ``relayed_bridge`` (``lib.mcp.client.get_bridge``),
``RelayedMCPBridge``.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from lib.log import get_logger
from lib.runtime import loopback_relay
from lib.runtime.loopback_relay import RelayUnavailable, is_cancelled

logger = get_logger(__name__)

FORMAT = 'tofu.mcp-bridge-relay/v1'
RECORD_NAME = 'mcp-bridge-relay.json'
_NAME = 'MCP bridge'
OWNER_ROLE = 'execution'
RELAY_ROLES = ('execution_shard',)
# The cancel observation MCPBridge._run_async already uses for a local call.
CANCEL_OBSERVATION_SECONDS = 0.1

__all__ = ['FORMAT', 'RECORD_NAME', 'RelayedMCPBridge', 'configure_mcp_owner',
           'record_path', 'relayed_bridge', 'stop_mcp_owner']


def record_path(project: str | os.PathLike) -> Path:
    return Path(project) / 'data' / RECORD_NAME


# ── Owner side (execution host) ─────────────────────────────────────────

def _encode_result(result) -> dict:
    from lib.mcp.result_content import MCPToolResult
    text = str.__str__(result) if isinstance(result, str) else str(result)
    if not isinstance(result, MCPToolResult):
        return {'text': text}
    return {'text': text, 'tool_result': {
        'image_contents': list(result.image_contents),
        'structured_json': result.structured_json,
        'document_text': result.document_text,
        'document_error': result.document_error,
        'is_error': bool(result.is_error),
        'content_blocks': list(result.content_blocks),
        'retention_error': result.retention_error,
    }}


def _decode_result(value: dict):
    from lib.mcp.result_content import MCPToolResult
    fields = value.get('tool_result')
    if fields is None:
        return value['text']
    return MCPToolResult(value['text'], **fields)


def _error_kind(exc: BaseException) -> str:
    from lib.mcp.client._errors import _is_call_timeout_error, _is_transport_dead_error
    if isinstance(exc, CancelledError):
        return 'cancelled'
    if _is_call_timeout_error(exc):
        return 'timeout'
    if _is_transport_dead_error(exc):
        return 'transport'
    if isinstance(exc, ValueError):
        return 'invalid'
    return 'error'


def _raise_error(kind: str, message: str):
    if kind == 'cancelled':
        raise CancelledError(message)
    if kind == 'timeout':
        raise TimeoutError(message)
    if kind == 'transport':
        raise ConnectionError(message)
    if kind == 'invalid':
        raise ValueError(message)
    raise RuntimeError(message)


def _method_table():
    from lib.mcp.client._errors import _safe_text
    from lib.mcp.client._state import local_bridge as get_bridge

    def catalog(params, cancel):
        return get_bridge(params['owner_user_id']).catalog_mirror(str(params.get('known') or ''))

    def list_servers(params, cancel):
        return get_bridge(params['owner_user_id']).list_servers()

    def call_tool(params, cancel):
        bridge = get_bridge(params['owner_user_id'])
        try:
            result = bridge.call_tool(
                params['name'], params['arguments'],
                timeout_override=params.get('timeout_override'), cancel=cancel,
                retry_on_disconnect=params.get('retry_on_disconnect'), meta=params.get('meta'))
        except Exception as exc:
            # Relayed, not swallowed: the shard re-raises it as its own call's failure.
            logger.debug('[MCP:Relay] call_tool %s failed; relayed to the shard: %s',
                         params.get('name'), exc)
            return {'error': {'kind': _error_kind(exc), 'message': _safe_text(exc)}}
        return {'result': _encode_result(result)}

    return {'catalog': catalog, 'list_servers': list_servers, 'call_tool': call_tool}


class RelayServer(loopback_relay.RelayServer):
    """Loopback listener owned by the execution host for its MCP bridge."""

    def __init__(self, path: Path):
        super().__init__(path, format=FORMAT, methods=_method_table(), name=_NAME)


# ── Caller side (execution shard) ───────────────────────────────────────

class RelayClient(loopback_relay.RelayClient):
    def __init__(self, path: Path):
        super().__init__(path, format=FORMAT, name=_NAME,
                         cancel_observation_seconds=CANCEL_OBSERVATION_SECONDS,
                         owner_label='execution host')


class _Mirror:
    """One immutable copy of the owner's catalog at one identity."""

    def __init__(self, identity: str, generation: int, index: list, *,
                 connected: bool, servers: list[str]):
        self.identity = identity
        self.generation = generation
        self.connected = connected
        self.servers = tuple(servers)
        self.index = {ns: info for ns, info, _disabled in index}
        self.rows = tuple(info for _ns, info, disabled in index if not disabled)
        self.index_rows = tuple((ns, info, bool(disabled)) for ns, info, disabled in index)
        self.lock = threading.Lock()
        self.frozen_defs = None
        self.fingerprint = ''
        self.search_text = None

    def live(self, *, connected: bool, servers: list[str]) -> '_Mirror':
        if connected == self.connected and tuple(servers) == self.servers:
            return self
        return _Mirror(self.identity, self.generation, list(self.index_rows),
                       connected=connected, servers=servers)


_EMPTY = _Mirror('', 0, [], connected=False, servers=[])


class RelayedMCPBridge:
    """An execution shard's view of the execution host's MCP bridge for one owner.

    It implements the bridge surface Turns use (catalog reads and calls);
    connection management belongs to the owner process.
    """

    def __init__(self, client: RelayClient, owner_user_id: int):
        self._client = client
        self._owner_user_id = owner_user_id
        self._lock = threading.Lock()
        self._mirror = _EMPTY
        self._generation = 0
        self._unreachable = False

    # catalog ---------------------------------------------------------------
    def _current(self) -> _Mirror:
        with self._lock:
            known = self._mirror
        try:
            answer = self._client.call('catalog', {
                'owner_user_id': self._owner_user_id, 'known': known.identity})
        except RelayUnavailable as exc:
            with self._lock:
                if not self._unreachable:
                    logger.warning('[MCP:Relay] the execution host owner is unreachable; '
                                   'this shard sees no MCP servers until it answers: %s', exc)
                self._unreachable = True
                if self._mirror.identity:
                    self._generation += 1
                    self._mirror = _Mirror('', self._generation, [], connected=False, servers=[])
                return self._mirror
        with self._lock:
            if self._unreachable:
                logger.info('[MCP:Relay] the execution host owner answers again')
                self._unreachable = False
            current = self._mirror
            if 'index' in answer and answer['identity'] != current.identity:
                self._generation += 1
                current = _Mirror(answer['identity'], self._generation, answer['index'],
                                  connected=bool(answer['connected']), servers=answer['servers'])
            else:
                current = current.live(connected=bool(answer['connected']),
                                       servers=answer['servers'])
            self._mirror = current
            return current

    @property
    def connected(self) -> bool:
        return self._current().connected

    @property
    def server_count(self) -> int:
        return len(self._current().servers)

    @property
    def tool_count(self) -> int:
        return len(self._current().index)

    def tool_catalog_generation(self) -> int:
        return self._current().generation

    def get_tool_info(self, namespaced_name: str):
        return self._current().index.get(namespaced_name)

    def is_mcp_tool(self, fn_name: str) -> bool:
        return fn_name in self._current().index

    def tool_index_rows(self):
        return list(self._current().index_rows)

    def get_tool_safety(self) -> dict[str, bool]:
        return {row['namespaced_name']: bool(row.get('read_only_hint'))
                for row in self._current().rows}

    def get_openai_tool_defs(self) -> list[dict[str, Any]]:
        return [row['openai_def'] for row in self._current().rows]

    def get_frozen_openai_tool_defs(self):
        mirror = self._current()
        with mirror.lock:
            if mirror.frozen_defs is None:
                mirror.frozen_defs = (mirror.generation,
                                      tuple(row['openai_def'] for row in mirror.rows))
            return mirror.frozen_defs

    def get_tool_catalog_snapshot(self) -> list[dict[str, Any]]:
        from lib.mcp.client._bridge import catalog_snapshot_row
        return [catalog_snapshot_row(row) for row in self._current().rows]

    def get_tool_catalog_projection(self):
        mirror = self._current()
        with mirror.lock:
            if not mirror.fingerprint:
                from lib.mcp.tool_search import catalog_snapshot_fingerprint
                mirror.fingerprint = catalog_snapshot_fingerprint(mirror.rows)
            return mirror.fingerprint, mirror.rows

    def get_tool_catalog_search_text_projection(self):
        mirror = self._current()
        with mirror.lock:
            if mirror.search_text is None:
                from lib.mcp.tool_search import catalog_search_text_by_name
                mirror.search_text = MappingProxyType(catalog_search_text_by_name(mirror.rows))
            return mirror.search_text

    def get_enabled_tool_summary(self) -> dict[str, Any]:
        mirror = self._current()
        counts = {name: 0 for name in mirror.servers}
        for row in mirror.rows:
            if row['server_name'] in counts:
                counts[row['server_name']] += 1
        servers = [{'name': name, 'count': counts[name]} for name in sorted(counts) if counts[name]]
        return {'servers': servers, 'total': sum(row['count'] for row in servers)}

    def list_servers(self) -> list[dict[str, Any]]:
        try:
            return self._client.call('list_servers', {'owner_user_id': self._owner_user_id})
        except RelayUnavailable as exc:
            logger.warning('[MCP:Relay] list_servers: %s', exc)
            return []

    # calls -----------------------------------------------------------------
    def call_tool(self, namespaced_name: str, arguments: dict[str, Any], *,
                  timeout_override: int | None = None, cancel=None,
                  retry_on_disconnect: bool | None = None,
                  meta: dict[str, Any] | None = None):
        if cancel is not None and cancel.is_set():
            raise CancelledError('MCP call cancelled')
        try:
            answer = self._client.call('call_tool', {
                'owner_user_id': self._owner_user_id, 'name': namespaced_name,
                'arguments': arguments, 'timeout_override': timeout_override,
                'retry_on_disconnect': retry_on_disconnect, 'meta': meta,
            }, wait_seconds=None, cancel_event=cancel)
        except RelayUnavailable as exc:
            # The owner may have run the call before it was lost: never replay.
            raise ConnectionError(
                f'connection closed: the MCP owner (execution host) did not answer; '
                f'the call outcome is unknown ({exc})') from exc
        if is_cancelled(answer):
            raise CancelledError('MCP call cancelled')
        if 'error' in answer:
            _raise_error(answer['error']['kind'], answer['error']['message'])
        return _decode_result(answer['result'])

    def call_tool_document(self, namespaced_name: str, arguments: dict[str, Any], *,
                           timeout: int | None = None, cancel=None) -> Any:
        from lib.mcp.documents import decode_document
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError('MCP document timeout must be a positive integer or None')
        return decode_document(self.call_tool(namespaced_name, arguments,
            timeout_override=timeout, cancel=cancel, retry_on_disconnect=False))

    # management belongs to the owner ---------------------------------------
    def _owned_elsewhere(self, *_args, **_kwargs):
        raise RuntimeError('MCP connections of an execution shard are owned by the execution host')

    connect_all = connect_server = set_disabled_tools = _disconnect_one = _owned_elsewhere

    def disconnect_all(self) -> None:
        """Nothing to stop: the owner's servers outlive this shard."""


_selection = loopback_relay.OwnerSelection(
    name=_NAME, record_path=record_path, make_server=RelayServer, make_client=RelayClient,
    owner_role=OWNER_ROLE, relay_roles=RELAY_ROLES)
_relayed: dict[int, RelayedMCPBridge] = {}
_relayed_lock = threading.Lock()


def configure_mcp_owner(*, process_role: str, environ) -> str:
    """``execution`` publishes the relay; its shards forward; every other role is local."""
    return _selection.configure(process_role=process_role, environ=environ)


def relays_mcp(process_role: str) -> bool:
    """True for a role whose MCP servers run in another process."""
    return process_role in RELAY_ROLES


def relayed_bridge(owner_user_id: int) -> RelayedMCPBridge | None:
    """This shard's relayed bridge for ``owner_user_id``, or None (local owner)."""
    client = _selection.selected()
    if client is None:
        return None
    with _relayed_lock:
        bridge = _relayed.get(owner_user_id)
        if bridge is None or bridge._client is not client:
            bridge = _relayed[owner_user_id] = RelayedMCPBridge(client, owner_user_id)
        return bridge


def stop_mcp_owner(timeout: float = 2.0) -> bool:
    with _relayed_lock:
        _relayed.clear()
    return _selection.stop(timeout)
