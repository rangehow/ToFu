"""Python binding of the Rust MCP client (``packages/tofu-mcp``, ``tofu-mcp-host``).

Responsibility: spawn one ``tofu-mcp-host`` child and speak its JSON-lines
host protocol (``tofu_mcp::host``): one request object per line, answered by
id; ``audit``/``log`` notifications are kept in order for the caller.
The host may also call back (``tofu_mcp::callback``): ``{"call", "method",
"params"}`` is answered with ``{"reply", "result"|"error"}`` from the
``callbacks`` the binding was given, each on its own thread so a slow answer
(a network route probe) never stalls replies; a method without a handler is
answered kind ``unavailable``. ``RustMCPBridge`` presents the bridge surface Turns use (``call_tool``,
``call_tool_document``, catalog reads) over it, decoding results and failure
categories exactly like the execution-shard relay (``lib/mcp/bridge_relay``).

This is the parity seam of the Rust rewrite (docs/RUST_REWRITE_EVIDENCE.md,
Phase 5 "MCP (rmcp)"): nothing in the product selects it yet; tests drive both
implementations through it. Entry points: ``RustMCPHost``, ``RustMCPBridge``,
``host_binary``. Dependencies: ``lib.mcp.bridge_relay`` (result/error codec).
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
from concurrent.futures import Future
from collections.abc import Callable
from typing import Any

BINARY_ENV = 'TOFU_MCP_HOST_BIN'


class RustHostError(RuntimeError):
    """A host-protocol failure carrying the relay's failure category."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def host_binary() -> str | None:
    """The configured ``tofu-mcp-host`` executable, or None."""
    path = os.environ.get(BINARY_ENV, '')
    return path if path and os.access(path, os.X_OK) else None


def _network_route_candidates(params: dict) -> list[dict]:
    """``network.route_candidates``: the shared authority's plan (docs/NETWORK.md)."""
    from lib import proxy
    return [{'route_id': route.route_id, 'label': route.label, 'mode': route.mode,
             'priority': route.priority, 'proxy_url': route.async_proxy_url(),
             'pool_id': route.pool_id}
            for route in proxy.network_route_candidates(params['url'], scope=params.get('scope'))]


def _network_report(params: dict) -> None:
    """``network.report``: one attempt's outcome on the exact route it used."""
    from lib import proxy
    from lib.network_routes import Route
    raw = params.get('route') or {}
    route = Route(params['route_id'], raw.get('label') or '', raw.get('mode') or 'direct',
                  raw.get('priority') or 0, raw.get('proxy_url') or '', raw.get('pool_id') or '')
    proxy.report_network_route(params['url'], route, bool(params['ok']), params.get('latency_ms'))


def _mcp_config_rows(params: dict) -> dict:
    """``mcp.config_rows``: the owner's stored server rows (``lib.mcp.config``)."""
    from lib.mcp.config import load_mcp_config
    return load_mcp_config(user_id=params.get('owner_user_id'))


def _mcp_patch_server(params: dict):
    """``mcp.patch_server``: patch one stored row; None when it is absent."""
    from lib.mcp.config import patch_server
    return patch_server(params['name'], params['changes'], user_id=params.get('owner_user_id'))


class _Unavailable(RuntimeError):
    """An owner that cannot answer now; the host treats it as absent."""
    kind = 'unavailable'


def _mcp_update_row(params: dict) -> dict:
    """``mcp.update_row``: replace one stored row in its locked transaction only
    while it (and the named vault entries) still hold what the host read;
    the vault writes happen inside that transaction."""
    from lib.credentials_vault import get_entry, set_entry
    from lib.mcp.config import update_server
    applied = []

    def _mutate(current):
        if current != params['expect'] or any(
                get_entry(entry) != value for entry, value in params['vault_expect'].items()):
            return None
        for entry, value in params['vault_set'].items():
            set_entry(entry, value)
        applied.append(True)
        return params['row']

    row = update_server(params['name'], _mutate, user_id=params.get('owner_user_id'))
    return {'row': row, 'applied': bool(applied)}


def _vault_get_entry(params: dict):
    """``vault.get_entry``: one credential vault value, or None."""
    from lib.credentials_vault import get_entry
    return get_entry(params['entry'])


def _browser_connected_clients(params: dict) -> list:
    """``browser.connected_clients``: the owner's devices (``lib.browser.queue``)."""
    from lib.browser.bridge_relay import RelayUnavailable
    from lib.browser.queue import get_connected_clients
    try:
        return list(get_connected_clients(owner_user_id=params['owner']))
    except RelayUnavailable as exc:
        raise _Unavailable(str(exc)) from exc


def _browser_select_client(params: dict) -> str:
    """``browser.select_client``: ``lib.browser.dispatch.select_owner_client``."""
    from lib.browser.dispatch import select_owner_client
    return select_owner_client(params['owner'], params['owned'],
                               preferred=params.get('preferred') or '')


def _browser_command(params: dict) -> dict:
    """``browser.command``: one ``send_browser_command`` as ``{result, error}``."""
    from lib.browser.queue import send_browser_command
    result, error = send_browser_command(
        params['type'], params['params'], timeout=params['timeout'],
        client_id=params['client_id'], owner_user_id=params['owner'])
    return {'result': result, 'error': str(error) if error else None}


def _browser_read_allowed(params: dict) -> bool:
    """``browser.read_allowed``: ``lib.browser.access.is_read_allowed``."""
    from lib.browser.access import is_read_allowed
    return bool(is_read_allowed(params['owner'], params['url']))


def _tool_search_index_capacity(params: dict) -> int:
    """``tool_search.index_capacity``: the catalog index LRU retention budget."""
    from lib.mcp import tool_search
    return tool_search._catalog_index_capacity()


def _mcp_declarations_get(params: dict):
    """``mcp.declarations_get``: the durable conversation wire, or None."""
    from lib.storage.mcp_declarations import MCPDeclarationStore
    return MCPDeclarationStore().get(user_id=params['user_id'],
                                     conversation_id=params['conversation_id'])


def _mcp_declarations_freeze(params: dict) -> list:
    """``mcp.declarations_freeze``: freeze (once) and return the conversation wire."""
    from lib.storage.mcp_declarations import MCPDeclarationStore
    return MCPDeclarationStore().freeze(params['definitions'], user_id=params['user_id'],
                                        conversation_id=params['conversation_id'])


def _mcp_registry_catalog(params: dict) -> list:
    """``mcp.registry_catalog``: the unfiltered ``registry.CATALOG`` after its hot reload."""
    from lib.mcp import registry
    registry._reload_catalog_if_changed()
    return list(registry.CATALOG)


#: Binding calls every host gets unless the caller supplies its own handler.
DEFAULT_CALLBACKS: dict[str, Callable[[dict], Any]] = {
    'tool_search.index_capacity': _tool_search_index_capacity,
    'mcp.declarations_get': _mcp_declarations_get,
    'mcp.declarations_freeze': _mcp_declarations_freeze,
    'mcp.registry_catalog': _mcp_registry_catalog,
    'network.route_candidates': _network_route_candidates,
    'network.report': _network_report,
    'mcp.config_rows': _mcp_config_rows,
    'mcp.patch_server': _mcp_patch_server,
    'mcp.update_row': _mcp_update_row,
    'vault.get_entry': _vault_get_entry,
    'browser.connected_clients': _browser_connected_clients,
    'browser.select_client': _browser_select_client,
    'browser.command': _browser_command,
    'browser.read_allowed': _browser_read_allowed,
}


class RustMCPHost:
    """One ``tofu-mcp-host`` child; requests may run concurrently."""

    def __init__(self, binary: str, *, env: dict[str, str] | None = None,
                 callbacks: dict[str, Callable[[dict], Any]] | None = None):
        self.callbacks: dict[str, Callable[[dict], Any]] = {**DEFAULT_CALLBACKS, **dict(callbacks or {})}
        self._proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      env=env, start_new_session=True)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self.audit: list[tuple[str, dict]] = []
        self.logs: list[tuple[str, str]] = []
        self._reader = threading.Thread(target=self._read, name='tofu-mcp-host-reader', daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for raw in self._proc.stdout:
            message = json.loads(raw)
            method = message.get('method')
            if method == 'audit':
                self.audit.append((message['params']['event'], message['params']['fields']))
                continue
            if method == 'log':
                self.logs.append((message['params']['level'], message['params']['message']))
                continue
            if 'call' in message:
                threading.Thread(target=self._answer_call, args=(message,), name='tofu-mcp-host-call',
                                 daemon=True).start()
                continue
            with self._lock:
                future = self._pending.pop(message.get('id'), None)
            if future is None:
                continue
            if 'error' in message:
                future.set_exception(RustHostError(message['error']['kind'], message['error']['message']))
            else:
                future.set_result(message.get('result'))
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for future in pending:
            future.set_exception(RustHostError('transport', 'tofu-mcp-host exited'))

    def _answer_call(self, message: dict) -> None:
        handler = self.callbacks.get(message.get('method'))
        reply: dict[str, Any] = {'reply': message['call']}
        if handler is None:
            reply['error'] = {'kind': 'unavailable', 'message': f"the binding has no handler for {message.get('method')}"}
        else:
            try:
                reply['result'] = handler(message.get('params') or {})
            except Exception as exc:  # the host gets the failure as data, never a dead reader
                reply['error'] = {'kind': getattr(exc, 'kind', 'error'), 'message': f'{type(exc).__name__}: {exc}'}
        try:
            self._write(reply)
        except (OSError, ValueError):
            pass  # the host exited; it fails its own waiting call

    def _write(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False) + '\n'
        with self._lock:
            self._proc.stdin.write(line.encode('utf-8'))
            self._proc.stdin.flush()

    def submit(self, method: str, params: dict[str, Any]) -> tuple[int, Future]:
        future: Future = Future()
        with self._lock:
            request_id = next(self._ids)
            self._pending[request_id] = future
        self._write({'id': request_id, 'method': method, 'params': params})
        return request_id, future

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return self.submit(method, params or {})[1].result(timeout)

    def close(self, timeout: float = 10.0) -> int | None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass  # the host is already gone
        try:
            return self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait()


class RustMCPBridge:
    """One owner's bridge in a ``tofu-mcp-host``, with ``MCPBridge``'s call surface."""

    def __init__(self, host: RustMCPHost, owner_user_id: int | None = None):
        self._host = host
        self._owner = owner_user_id

    def _params(self, **params) -> dict[str, Any]:
        return {'owner_user_id': self._owner, **params}

    def connect_server(self, name: str, config: dict) -> list[str]:
        return self._host.call('connect_server', self._params(name=name, config=config))

    def set_disabled_tools(self, name: str, tools) -> None:
        self._host.call('set_disabled_tools', self._params(name=name, tools=list(tools)))

    def call_tool(self, namespaced_name: str, arguments: dict[str, Any], *,
                  timeout_override: int | None = None, cancel=None,
                  retry_on_disconnect: bool | None = None, meta: dict[str, Any] | None = None):
        from concurrent.futures import CancelledError
        from lib.mcp.bridge_relay import CANCEL_OBSERVATION_SECONDS, _decode_result, _raise_error
        if cancel is not None and cancel.is_set():
            raise CancelledError('MCP call cancelled')
        request_id, future = self._host.submit('call_tool', self._params(
            name=namespaced_name, arguments=arguments, timeout_override=timeout_override,
            retry_on_disconnect=retry_on_disconnect, meta=meta))
        if cancel is not None:
            def _watch():
                while not future.done():
                    if cancel.wait(CANCEL_OBSERVATION_SECONDS):
                        self._host.submit('cancel', {'id': request_id})
                        return
            threading.Thread(target=_watch, name='tofu-mcp-host-cancel', daemon=True).start()
        try:
            answer = future.result()
        except RustHostError as exc:
            _raise_error(exc.kind, str(exc))
        return _decode_result(answer)

    def call_tool_document(self, namespaced_name: str, arguments: dict[str, Any], *,
                           timeout: int | None = None, cancel=None) -> Any:
        from lib.mcp.documents import decode_document
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError('MCP document timeout must be a positive integer or None')
        return decode_document(self.call_tool(namespaced_name, arguments, timeout_override=timeout,
                                              cancel=cancel, retry_on_disconnect=False))

    def catalog_mirror(self, known: str) -> dict[str, Any]:
        return self._host.call('catalog', self._params(known=known))

    def tool_index_rows(self):
        return [tuple(row) for row in self._host.call('tool_index_rows', self._params())]

    def get_tool_info(self, namespaced_name: str):
        return next((info for name, info, _ in self.tool_index_rows() if name == namespaced_name), None)

    def get_openai_tool_defs(self) -> list[dict[str, Any]]:
        return self._host.call('openai_tool_defs', self._params())

    def get_tool_safety(self) -> dict[str, bool]:
        return self._host.call('tool_safety', self._params())

    def tool_catalog_generation(self) -> int:
        return self._host.call('tool_catalog_generation', self._params())

    def is_mcp_tool(self, fn_name: str) -> bool:
        return self.get_tool_info(fn_name) is not None

    # Catalog projections (``tofu_mcp::bridge::catalog``). Like ``MCPBridge``,
    # each generation's objects are built once and shared until it advances.
    def _generation_view(self, method: str, key: str, build: Callable[[dict], Any]):
        cached = getattr(self, key, None)
        answer = self._host.call(method, self._params(known=cached[0] if cached else None))
        if cached is not None and answer['generation'] == cached[0]:
            return cached[1]
        value = build(answer)
        setattr(self, key, (answer['generation'], value))
        return value

    def get_frozen_openai_tool_defs(self) -> tuple[int, tuple[dict[str, Any], ...]]:
        return self._generation_view(
            'catalog.frozen_defs', '_frozen_defs',
            lambda answer: (answer['generation'], tuple(answer['defs'])))

    def _projection(self):
        from types import MappingProxyType
        return self._generation_view(
            'catalog.projection', '_projection_cache',
            lambda answer: (answer['fingerprint'], tuple(answer['rows']),
                            MappingProxyType(answer['search_text'])))

    def get_tool_catalog_projection(self) -> tuple[str, tuple[dict[str, Any], ...]]:
        fingerprint, rows, _ = self._projection()
        return fingerprint, rows

    def get_tool_catalog_search_text_projection(self):
        return self._projection()[2]

    def get_tool_catalog_snapshot(self) -> list[dict[str, Any]]:
        return self._host.call('catalog.snapshot', self._params())

    def get_enabled_tool_summary(self) -> dict[str, Any]:
        return self._host.call('catalog.enabled_tool_summary', self._params())

    @property
    def server_count(self) -> int:
        return self._host.call('catalog.counts', self._params())['server_count']

    @property
    def tool_count(self) -> int:
        return self._host.call('catalog.counts', self._params())['tool_count']

    @property
    def connected(self) -> bool:
        return self._host.call('catalog.counts', self._params())['connected']

    def list_servers(self) -> list[dict[str, Any]]:
        return self._host.call('list_servers', self._params())

    def disconnect_all(self) -> None:
        self._host.call('disconnect_all', self._params())


def launcher_callbacks() -> dict[str, Callable[[dict], Any]]:
    """Answers for the Rust launcher (``tofu_mcp::vendor``): the facts ``lib.mcp.client`` owns.

    The vendored registry is read live after its hot reload, so a row added to
    ``vendored.py`` reaches the Rust client without a restart; the proxy pool
    answers come from the shared network authority (``lib.proxy``).
    """
    def context(params: dict) -> dict:
        import sys

        import lib.mcp.client as client
        import lib.mcp.vendored as vendored
        import lib.tools.process_environment as process_environment
        client._reload_vendored_if_changed()
        return {
            'launchers': {name: dict(row) for name, row in client._VENDORED_LAUNCHERS.items()},
            'extra_sources': {name: list(row.get('extra_sources', []))
                              for name, row in vendored.VENDORED_LAUNCHERS.items()},
            'repo_root': client._repo_root(),
            'source_root': vendored.repo_root(),
            'runtime_base': process_environment.get_runtime_base(),
            'interpreter': sys.executable or '',
            'base_prefix': getattr(sys, 'base_prefix', '') or '',
            'python_minor': f'{sys.version_info.major}.{sys.version_info.minor}',
        }

    def proxy_url(reachable: bool) -> Callable[[dict], str]:
        def answer(params: dict) -> str:
            from lib import proxy
            try:
                return (proxy.first_reachable_global_proxy_url() if reachable
                        else proxy.first_global_proxy_url()) or ''
            except Exception:  # the oracle treats an unavailable pool as no proxy
                return ''
        return answer

    return {'launcher_context': context, 'global_proxy_url': proxy_url(False),
            'reachable_global_proxy_url': proxy_url(True)}


__all__ = ['BINARY_ENV', 'RustHostError', 'RustMCPBridge', 'RustMCPHost', 'host_binary',
           'launcher_callbacks']
