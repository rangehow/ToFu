"""The Rust MCP client's Streamable HTTP through Tofu's shared network authority.

``packages/tofu-mcp/src/network.rs`` asks the binding for the route plan
(``network.route_candidates``) and reports each attempt (``network.report``);
``lib/mcp/rust_host.py`` answers both from ``lib.proxy``. These tests replace
that authority (``lib.proxy.network_route_candidates`` /
``report_network_route``) with a scripted plan that BOTH clients consume — the
Python bridge through ``RoutingAsyncTransport`` and the Rust host through its
binding — and drive them through loopback forward proxies (one of which can
refuse connections or drop a request) to a real streamable-http MCP server
(the oracle fixture of ``test_mcp_remote_transport.py``) and to faulty HTTP
servers, asserting route order, connect-only failover, reports, no replay of
delivered requests, header delivery and redaction. The remaining cases port
``test_mcp_remote_transport.py`` against the Rust client.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from tests.mcp_protocol.rust_parity_support import mcp_host_binary, python_bridge, scene  # noqa: F401  (fixtures)
from tests.mcp_protocol.test_mcp_remote_transport import (
    _SERVER_SRC, SECRET, _CN_LOCAL_IDS, _amap_cfg, _free_port, _remote_cfg, _vault_remote_cfg,
)
__all__ = ['mcp_host_binary', 'scene']

pytestmark = pytest.mark.unit

TOOLS = {'whoami', 'paper_document', 'wait_for_cancel', 'cancellation_state'}

COVERS: dict[str, list[str]] = {
    'test_mcp_remote_transport.py': [
        'test_authenticated_remote_server_is_reachable_through_both_clients',
        'test_application_document_and_cancellation_cross_real_http_mcp_in_rust',
        'test_unresolved_header_credential_names_the_missing_key_in_both_clients',
        'test_env_supplies_the_exact_header_values_through_the_route',
        'test_a_vault_reference_fills_header_and_url_placeholders_in_rust',
        'test_an_unset_vault_entry_names_the_key_and_sends_nothing_in_rust',
        'test_an_unreferenced_vault_entry_is_not_required_in_rust',
        'test_redaction_parity',
        'test_list_servers_stored_shapes_are_redacted_by_rust',
        'test_stdio_classification_parity',
        'test_catalog_built_remote_config_connects_through_rust',
        'test_query_param_credential_reaches_the_server_through_rust',
        'test_url_only_server_still_advertises_its_required_env_key_in_rust',
        'test_missing_query_param_credential_names_the_key_in_rust',
        'test_every_config_field_is_classified_alike',
        'test_scrub_text_parity',
        'test_connect_error_message_is_scrubbed_in_both_clients',
        'test_china_local_entries_redact_and_advertise_alike',
    ],
}


# ── Loopback network fakes ───────────────────────────────────────────

@pytest.fixture(scope='module')
def auth_server(tmp_path_factory):
    """The oracle's bearer-protected streamable-http MCP server.

    Unlike the oracle fixture its output goes to a file: these tests drop
    connections on purpose, and the tracebacks would fill an unread pipe and
    stall the server.
    """
    import subprocess
    import sys
    port = _free_port()
    directory = tmp_path_factory.mktemp('mcpsrv')
    script = directory / 'server.py'
    script.write_text(_SERVER_SRC)
    with open(directory / 'server.log', 'wb') as log:
        proc = subprocess.Popen([sys.executable, str(script), str(port), SECRET],
                                stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 30
    while True:
        if proc.poll() is not None:
            pytest.skip(f"MCP test server died: {(directory / 'server.log').read_text()[:400]}")
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() > deadline:
                proc.kill()
                pytest.skip('MCP test server did not come up')
            time.sleep(0.1)
    yield f'http://127.0.0.1:{port}/mcp'
    proc.kill()
    proc.wait(timeout=10)


class ForwardProxy:
    """An HTTP/1.1 forward proxy for ``http://`` targets that records each request head.

    ``mode='reset'`` reads one request and closes without answering: the
    request may have been delivered, so it must never be replayed.
    """

    def __init__(self, mode: str = 'forward'):
        self.mode = mode
        self.heads: list[tuple[str, str, dict]] = []
        self._sock = socket.socket()
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(64)
        self.url = f'http://127.0.0.1:{self._sock.getsockname()[1]}'
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _pump(src, dst):
        try:
            while chunk := src.recv(65536):
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _serve(self, conn):
        buffer, upstream = b'', None
        try:
            while True:
                while b'\r\n\r\n' not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                head, buffer = buffer.split(b'\r\n\r\n', 1)
                lines = head.decode('latin-1').split('\r\n')
                method, target, version = lines[0].split(' ', 2)
                headers = {}
                for line in lines[1:]:
                    key, _, value = line.partition(':')
                    headers[key.strip().lower()] = value.strip()
                self.heads.append((method, target, headers))
                if self.mode == 'reset':
                    return
                parts = urlsplit(target)
                if upstream is None:
                    upstream = socket.create_connection((parts.hostname, parts.port))
                    threading.Thread(target=self._pump, args=(upstream, conn), daemon=True).start()
                length = int(headers.get('content-length') or 0)
                while len(buffer) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                body, buffer = buffer[:length], buffer[length:]
                path = parts.path + (f'?{parts.query}' if parts.query else '')
                upstream.sendall(f'{method} {path} {version}\r\n'.encode('latin-1')
                                 + '\r\n'.join(lines[1:]).encode('latin-1') + b'\r\n\r\n' + body)
        except OSError:
            pass
        finally:
            if upstream is None:
                conn.close()

    def mcp_posts(self) -> list[str]:
        return [target for method, target, _ in self.heads if method == 'POST']

    def close(self):
        self._closed = True
        self._sock.close()


class RefusedPort:
    """A bound, never-listening port: every connection is refused (proven connect failure)."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(('127.0.0.1', 0))
        self.url = f'http://127.0.0.1:{self._sock.getsockname()[1]}'

    def close(self):
        self._sock.close()


class FaultyServer:
    """Answers every POST with a 500 (``status``) or a truncated 200 body (``truncate``); keeps each body."""

    def __init__(self, mode: str):
        owner = self
        self.bodies: list[bytes] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.bodies.append(self.rfile.read(int(self.headers.get('content-length') or 0)))
                if mode == 'status':
                    body = b'boom'
                    self.send_response(500)
                    self.send_header('Content-Type', 'text/plain')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', '1000')
                    self.end_headers()
                    self.wfile.write(b'{"jsonrpc": "2.0", "id"')
                    self.wfile.flush()
                    self.close_connection = True

            def do_GET(self):
                self.send_response(405)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_DELETE = do_GET

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self._server.server_address[1]}/mcp'
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class Authority:
    """A scripted shared network authority (``lib.proxy``) both clients consume."""

    def __init__(self, monkeypatch):
        import lib.proxy as proxy
        self.plan: list[tuple[str, str | None]] = []
        self.reports: list[tuple[str, bool, float | None]] = []
        self.asked: list[str] = []
        monkeypatch.setattr(proxy, 'network_route_candidates', self._candidates)
        monkeypatch.setattr(proxy, 'report_network_route', self._report)

    def _candidates(self, url, *, scope=None, **_):
        from lib.network_routes import Route
        self.asked.append(url)
        return [Route(route_id, route_id, 'proxy' if proxy_url else 'direct', index, proxy_url or '')
                for index, (route_id, proxy_url) in enumerate(self.plan)]

    def _report(self, url, route, ok, latency_ms=None, **_):
        self.reports.append((route.route_id, ok, latency_ms))

    def outcomes(self) -> list[tuple[str, bool]]:
        return [(route_id, ok) for route_id, ok, _ in self.reports]


@pytest.fixture
def authority(monkeypatch):
    return Authority(monkeypatch)


@pytest.fixture
def net():
    made = []

    def make(kind, *args):
        made.append(kind(*args))
        return made[-1]
    yield make
    for item in made:
        item.close()


def _host(binary, scene, *, vault=None, callbacks=None):
    from lib.mcp.rust_host import RustMCPHost
    host = RustMCPHost(binary, env=dict(os.environ), callbacks=callbacks)
    host.call('configure', {**scene.host_settings(), 'vault': vault or {}}, timeout=30)
    return host


@pytest.fixture
def rust(mcp_host_binary, scene):
    from lib.mcp.rust_host import RustMCPBridge
    host = _host(mcp_host_binary, scene)
    yield RustMCPBridge(host, 1)
    host.close()


def _connect_error(bridge, name, cfg) -> str:
    with pytest.raises(Exception) as caught:
        bridge.connect_server(name, cfg)
    return str(caught.value)


def _both(monkeypatch, rust):
    return [('python', python_bridge(monkeypatch)), ('rust', rust)]


def _disconnect(bridge):
    try:
        bridge.disconnect_all()
    except Exception:
        pass


# ── Route plan, failover and reports ─────────────────────────────────

@pytest.mark.parametrize('order', [('first', 'second'), ('second', 'first')])
def test_route_order_follows_the_binding_plan(order, auth_server, authority, net, rust, monkeypatch):
    proxies = {'first': net(ForwardProxy), 'second': net(ForwardProxy)}
    authority.plan = [(f'pool:{tag}', proxies[tag].url) for tag in order]
    for client, bridge in _both(monkeypatch, rust):
        authority.reports.clear()
        for proxy in proxies.values():
            proxy.heads.clear()
        try:
            assert {getattr(t, 'name', t) for t in bridge.connect_server('authcheck', _remote_cfg(auth_server))} == TOOLS
            assert 'authenticated-ok' in str(bridge.call_tool('mcp__authcheck__whoami', {}))
        finally:
            _disconnect(bridge)
        leader, other = proxies[order[0]], proxies[order[1]]
        assert leader.mcp_posts(), client
        assert other.heads == [], client
        assert {(route_id, ok) for route_id, ok in authority.outcomes()} == {(f'pool:{order[0]}', True)}, client
        assert all(latency is not None and latency >= 0 for _, _, latency in authority.reports)


def test_a_refused_first_route_fails_over_and_both_outcomes_are_reported(
        auth_server, authority, net, rust, monkeypatch):
    refused, proxy = net(RefusedPort), net(ForwardProxy)
    authority.plan = [('pool:refused', refused.url), ('pool:proxy', proxy.url)]
    for client, bridge in _both(monkeypatch, rust):
        authority.reports.clear()
        try:
            bridge.connect_server('authcheck', _remote_cfg(auth_server))
            assert 'authenticated-ok' in str(bridge.call_tool('mcp__authcheck__whoami', {}))
        finally:
            _disconnect(bridge)
        outcomes = authority.outcomes()
        assert outcomes[:2] == [('pool:refused', False), ('pool:proxy', True)], client
        assert ('pool:refused', True) not in outcomes, client
        ok_latencies = [latency for route_id, ok, latency in authority.reports if ok]
        assert ok_latencies and all(latency > 0 for latency in ok_latencies), client


def test_exhausted_routes_are_a_typed_connect_failure_in_both_clients(
        authority, net, rust, monkeypatch):
    first, second = net(RefusedPort), net(RefusedPort)
    authority.plan = [('pool:a', first.url), ('pool:b', second.url)]
    url = f'http://127.0.0.1:9/mcp?key={SECRET}'
    messages = {}
    for client, bridge in _both(monkeypatch, rust):
        authority.reports.clear()
        try:
            messages[client] = _connect_error(bridge, 'gone', {'transport': 'streamable-http', 'url': url})
        finally:
            _disconnect(bridge)
        assert authority.outcomes()[:2] == [('pool:a', False), ('pool:b', False)], client
        assert all(not ok for _, ok in authority.outcomes()), client
    for client, message in messages.items():
        assert SECRET not in message, client
    assert 'No configured network route connected' in messages['rust']


@pytest.mark.parametrize('mode', ['status', 'truncate'])
def test_a_delivered_request_is_never_replayed(mode, authority, net, rust, monkeypatch):
    first, second = net(ForwardProxy), net(ForwardProxy)
    authority.plan = [('pool:first', first.url), ('pool:second', second.url)]
    for client, bridge in _both(monkeypatch, rust):
        server = net(FaultyServer, mode)
        authority.reports.clear()
        try:
            _connect_error(bridge, 'faulty', {'transport': 'streamable-http', 'url': server.url})
        finally:
            _disconnect(bridge)
        # The Python SDK falls back from ``server/discover`` to ``initialize``: two
        # messages, each delivered once. No message may reach the server twice.
        assert server.bodies and len(server.bodies) == len(set(server.bodies)), (client, server.bodies)
        assert second.heads == [], client
        assert ('pool:first', False) not in authority.outcomes(), client
        assert ('pool:second', True) not in authority.outcomes(), client


def test_an_ambiguous_disconnect_is_neither_replayed_nor_reported(
        auth_server, authority, net, rust, monkeypatch):
    dropping, proxy = net(ForwardProxy, 'reset'), net(ForwardProxy)
    authority.plan = [('pool:dropping', dropping.url), ('pool:proxy', proxy.url)]
    for client, bridge in _both(monkeypatch, rust):
        authority.reports.clear()
        dropping.heads.clear()
        try:
            _connect_error(bridge, 'authcheck', _remote_cfg(auth_server))
        finally:
            _disconnect(bridge)
        assert len(dropping.heads) == 1, client
        assert proxy.heads == [], client
        assert authority.outcomes() == [], client


def test_env_supplies_the_exact_header_values_through_the_route(
        auth_server, authority, net, rust, monkeypatch):
    proxy = net(ForwardProxy)
    authority.plan = [('pool:proxy', proxy.url)]
    cfg = {**_remote_cfg(auth_server),
           'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}', 'X-Trace': 'plain'}}
    for client, bridge in _both(monkeypatch, rust):
        proxy.heads.clear()
        try:
            bridge.connect_server('authcheck', cfg)
        finally:
            _disconnect(bridge)
        posts = [headers for method, _, headers in proxy.heads if method == 'POST']
        assert posts, client
        for headers in posts:
            assert headers['authorization'] == f'Bearer {SECRET}', client
            assert headers['x-trace'] == 'plain', client


def test_connect_error_message_is_scrubbed_in_both_clients(auth_server, authority, net, rust, monkeypatch):
    proxy = net(ForwardProxy)
    authority.plan = [('pool:proxy', proxy.url)]
    cfg = {'transport': 'streamable-http', 'url': auth_server + '?key=${AMAP_MAPS_API_KEY}',
           'env': {'AMAP_MAPS_API_KEY': SECRET + '-WRONG'}}
    for client, bridge in _both(monkeypatch, rust):
        proxy.heads.clear()
        try:
            message = _connect_error(bridge, 'amap-fail', cfg)
        finally:
            _disconnect(bridge)
        assert SECRET not in message, (client, message)
        assert any(f'key={SECRET}-WRONG' in target for _, target, _ in proxy.heads), client


def test_standalone_rust_client_with_no_route_handler_connects_direct(auth_server, mcp_host_binary, scene):
    from lib.mcp.rust_host import RustMCPBridge

    def unavailable(params):
        class Unavailable(Exception):
            kind = 'unavailable'
        raise Unavailable('no authority in this composition')

    host = _host(mcp_host_binary, scene, callbacks={'network.route_candidates': unavailable})
    try:
        bridge = RustMCPBridge(host, 1)
        assert set(bridge.connect_server('authcheck', _remote_cfg(auth_server))) == TOOLS
        bridge.disconnect_all()
    finally:
        host.close()


def test_a_failing_route_authority_fails_closed(auth_server, mcp_host_binary, scene):
    from lib.mcp.rust_host import RustMCPBridge

    def broken(params):
        raise RuntimeError('route table unreadable')

    host = _host(mcp_host_binary, scene, callbacks={'network.route_candidates': broken})
    try:
        message = _connect_error(RustMCPBridge(host, 1), 'authcheck', _remote_cfg(auth_server))
        assert 'route table unreadable' in message
    finally:
        host.close()


def test_network_get_follows_the_plan_and_reports(authority, net, rust):
    refused, proxy, server = net(RefusedPort), net(ForwardProxy), net(FaultyServer, 'status')
    authority.plan = [('pool:refused', refused.url), ('pool:proxy', proxy.url)]
    answer = rust._host.call('network_get', {'url': server.url}, timeout=60)
    assert answer['status'] == 405
    assert authority.outcomes() == [('pool:refused', False), ('pool:proxy', True)]
    authority.plan = [('pool:refused', refused.url)]
    from lib.mcp.rust_host import RustHostError
    with pytest.raises(RustHostError) as caught:
        rust._host.call('network_get', {'url': server.url + f'?key={SECRET}'}, timeout=60)
    assert caught.value.kind == 'transport'
    assert 'No configured network route connected' in str(caught.value)


# ── test_mcp_remote_transport.py against the Rust client ─────────────

def test_authenticated_remote_server_is_reachable_through_both_clients(auth_server, rust, monkeypatch):
    results = {}
    for client, bridge in _both(monkeypatch, rust):
        try:
            tools = bridge.connect_server('authcheck', _remote_cfg(auth_server))
            assert {getattr(t, 'name', t) for t in tools} == TOOLS, client
            results[client] = bridge.call_tool('mcp__authcheck__whoami', {})
        finally:
            _disconnect(bridge)
    assert 'authenticated-ok' in str(results['rust'])
    assert results['rust'] == results['python']


def test_application_document_and_cancellation_cross_real_http_mcp_in_rust(auth_server, rust, monkeypatch):
    from lib.mcp.documents import decode_document

    results = {}
    for client, bridge in _both(monkeypatch, rust):
        try:
            bridge.connect_server('papers', _remote_cfg(auth_server))
            results[client] = bridge.call_tool('mcp__papers__paper_document', {})
        finally:
            _disconnect(bridge)
    assert '[Truncated:' in results['rust']
    assert decode_document(results['rust']) == {'build_id': 'a' * 32, 'chunk': 'x' * 201000}
    assert results['rust'] == results['python']

    try:
        rust.connect_server('papers', _remote_cfg(auth_server))
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(rust.call_tool_document, 'mcp__papers__wait_for_cancel', {},
                                     timeout=10, cancel=cancel)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = rust.call_tool_document('mcp__papers__cancellation_state', {}, timeout=5)
                    if state['held']:
                        break
                    time.sleep(.05)
                assert state['held'], 'The remote tool did not start'
                cancel.set()
                with pytest.raises(CancelledError):
                    future.result(timeout=2)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = rust.call_tool_document('mcp__papers__cancellation_state', {}, timeout=5)
                    if state['retired']:
                        break
                    time.sleep(.05)
                assert state == {'held': False, 'retired': True}
            finally:
                cancel.set()
    finally:
        _disconnect(rust)


def test_unresolved_header_credential_names_the_missing_key_in_both_clients(rust, monkeypatch):
    cfg = {'transport': 'streamable-http', 'url': 'http://127.0.0.1:9/mcp',
           'headers': {'Authorization': 'Bearer ${NOPE_MISSING_KEY}'}, 'env': {}}
    for client, bridge in _both(monkeypatch, rust):
        assert 'NOPE_MISSING_KEY' in _connect_error(bridge, 'rollinggo', cfg), client


@pytest.fixture
def vault_host(mcp_host_binary, scene, monkeypatch):
    from lib.mcp.rust_host import RustMCPBridge
    import lib.credentials_vault as credentials_vault
    entries = {'rollinggo_api_key': SECRET}
    monkeypatch.setattr(credentials_vault, 'get_entry', entries.get)

    def make(vault):
        host = _host(mcp_host_binary, scene, vault=vault)
        made.append(host)
        return RustMCPBridge(host, 1)
    made = []
    yield make
    for host in made:
        host.close()


def test_a_vault_reference_fills_header_and_url_placeholders_in_rust(auth_server, vault_host, monkeypatch):
    for client, bridge in [('python', python_bridge(monkeypatch)),
                           ('rust', vault_host({'rollinggo_api_key': SECRET}))]:
        cfg = {**_vault_remote_cfg(auth_server + '?key=${ROLLINGGO_API_KEY}'), 'headers': {}}
        try:
            bridge.connect_server('rollinggo', cfg)
            assert 'authenticated-ok' in str(bridge.call_tool('mcp__rollinggo__whoami', {})), client
        finally:
            _disconnect(bridge)
        bridge = bridge if client == 'python' else vault_host({'rollinggo_api_key': SECRET})
        try:
            bridge.connect_server('rollinggo', _vault_remote_cfg(auth_server))
            assert 'authenticated-ok' in str(bridge.call_tool('mcp__rollinggo__whoami', {})), client
        finally:
            _disconnect(bridge)


def test_an_unset_vault_entry_names_the_key_and_sends_nothing_in_rust(vault_host, monkeypatch, net, authority):
    proxy = net(ForwardProxy)
    authority.plan = [('pool:proxy', proxy.url)]
    import lib.credentials_vault as credentials_vault
    monkeypatch.setattr(credentials_vault, 'get_entry', {}.get)
    for client, bridge in [('python', python_bridge(monkeypatch)), ('rust', vault_host({}))]:
        message = _connect_error(bridge, 'rollinggo', _vault_remote_cfg('http://127.0.0.1:9/mcp'))
        assert 'ROLLINGGO_API_KEY' in message and 'rollinggo_api_key' in message, (client, message)
    assert proxy.heads == []


def test_an_unreferenced_vault_entry_is_not_required_in_rust(auth_server, vault_host, monkeypatch):
    cfg = {**_remote_cfg(auth_server),
           'env': {'ROLLINGGO_API_KEY': 'vault:rollinggo_api_key', 'UNUSED': 'vault:not_configured'}}
    for client, bridge in [('python', python_bridge(monkeypatch)),
                           ('rust', vault_host({'rollinggo_api_key': SECRET}))]:
        try:
            assert {getattr(t, 'name', t) for t in bridge.connect_server('rollinggo', cfg)} == TOOLS, client
        finally:
            _disconnect(bridge)


def test_query_param_credential_reaches_the_server_through_rust(auth_server, rust, monkeypatch):
    cfg = {'transport': 'streamable-http', 'url': auth_server + '?key=${AMAP_MAPS_API_KEY}',
           'env': {'AMAP_MAPS_API_KEY': SECRET}, 'enabled': True}
    for client, bridge in _both(monkeypatch, rust):
        try:
            assert {getattr(t, 'name', t) for t in bridge.connect_server('amap', cfg)} == TOOLS, client
            assert 'authenticated-ok' in str(bridge.call_tool('mcp__amap__whoami', {})), client
        finally:
            _disconnect(bridge)


def test_missing_query_param_credential_names_the_key_in_rust(rust, monkeypatch):
    cfg = {'transport': 'streamable-http', 'url': 'http://127.0.0.1:9/mcp?key=${AMAP_MAPS_API_KEY}', 'env': {}}
    for client, bridge in _both(monkeypatch, rust):
        assert 'AMAP_MAPS_API_KEY' in _connect_error(bridge, 'amap', cfg), client


def test_catalog_built_remote_config_connects_through_rust(auth_server, rust, monkeypatch):
    import lib.mcp.registry as reg
    entry = {
        'id': 'rollinggo-hotel', 'name': 'RollingGo Hotel', 'description': 'hotels',
        'category': reg.CAT_OTHER, 'transport': 'streamable-http', 'endpoint': auth_server,
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'}, 'command': '', 'args': [],
        'env_specs': [{'key': 'ROLLINGGO_API_KEY', 'label': 'API Key', 'required': True, 'secret': True}],
    }
    monkeypatch.setattr(reg, 'get_catalog_entry', lambda sid: entry)
    cfg = reg.build_server_config('rollinggo-hotel', {'ROLLINGGO_API_KEY': SECRET})
    assert 'command' not in cfg
    try:
        assert set(rust.connect_server('hotel', cfg)) == TOOLS
    finally:
        _disconnect(rust)


# ── Redaction, classification and catalog configs (Rust vs Python) ──

_REDACTION_CASES = [
    _remote_cfg('http://127.0.0.1:1/mcp'),
    {'transport': 'streamable-http', 'url': 'http://x/mcp', 'headers': {'Authorization': f'Bearer {SECRET}'}},
    {'transport': 'streamable-http', 'url': f'https://mcp.amap.com/mcp?key={SECRET}'},
    {'transport': 'streamable-http', 'url': 'https://x/mcp', 'oauth_refresh_token': SECRET,
     'proxy_password': 'hunter2'},
]


@pytest.mark.parametrize('cfg', _REDACTION_CASES)
def test_redaction_parity(cfg, rust):
    from lib.mcp.transport import redact_config
    answer = rust._host.call('redact_config', {'config': cfg}, timeout=30)
    assert answer['config'] == redact_config(cfg)
    blob = json.dumps(answer['config'])
    assert SECRET not in blob and 'hunter2' not in blob


def test_list_servers_stored_shapes_are_redacted_by_rust(rust):
    """The route's MCP-client part: the stored row (env + literal headers) as redaction sees it."""
    from lib.mcp.transport import redact_config
    stored = _remote_cfg('http://127.0.0.1:1/mcp')
    stored['headers'] = {'Authorization': f'Bearer {SECRET}', 'X-Api-Key': SECRET}
    answer = rust._host.call('redact_config', {'config': stored}, timeout=30)
    assert answer['config'] == redact_config(stored)
    assert SECRET not in json.dumps(answer['config'])


def test_every_config_field_is_classified_alike(rust):
    from lib.mcp.types import MCPServerConfig
    from lib.mcp.transport import redact_config
    cfg = {field: SECRET for field in MCPServerConfig.__annotations__}
    cfg.update({'transport': 'streamable-http', 'url': f'https://x/mcp?key={SECRET}',
                'headers': {'Authorization': f'Bearer {SECRET}'}, 'env': {'K': SECRET},
                'args': ['--token', SECRET], 'enabled': True, 'timeout': 5})
    answer = rust._host.call('redact_config', {'config': cfg}, timeout=30)
    assert answer['config'] == redact_config(cfg)


@pytest.mark.parametrize('transport,expect_stdio', [
    ('stdio', True), (None, True), ('sse', False), ('streamable-http', False),
    ('http', False), ('streamable_http', False),
])
def test_stdio_classification_parity(transport, expect_stdio, rust):
    """``is_stdio`` as the Rust client applies it (``updates.parse_package_ref``'s first gate)."""
    from lib.mcp.updates import parse_package_ref
    cfg = {'command': 'npx', 'url': 'http://x/mcp', 'args': ['-y', 'pkg']}
    if transport is not None:
        cfg['transport'] = transport
    ref, reason = rust._host.call('updates.parse_package_ref', {'config': cfg}, timeout=30)
    assert (reason != 'remote-transport') is expect_stdio
    assert [ref, reason] == list(parse_package_ref(cfg))


def test_scrub_text_parity(rust):
    from lib.mcp.transport import scrub_text
    raw = (f"Client error '401 Unauthorized' for url "
           f"'https://mcp.amap.com/mcp?key={SECRET}'\n"
           f'For more information check: https://example.com/401')
    out = rust._host.call('scrub_text', {'text': raw}, timeout=30)
    assert out == scrub_text(raw)
    assert SECRET not in out and 'mcp.amap.com/mcp?key=***' in out and 'https://example.com/401' in out


def test_url_only_server_still_advertises_its_required_env_key_in_rust(rust):
    assert rust._host.call('header_env_keys', {'config': _amap_cfg()}, timeout=30) == ['AMAP_MAPS_API_KEY']


def test_china_local_entries_redact_and_advertise_alike(rust):
    import lib.mcp.registry as reg
    from lib.mcp.transport import header_env_keys, is_stdio, redact_config
    for sid in _CN_LOCAL_IDS:
        entry = reg.get_catalog_entry(sid)
        cfg = reg.build_server_config(sid, {s['key']: SECRET for s in entry.get('env_specs', [])})
        answer = rust._host.call('redact_config', {'config': cfg}, timeout=30)
        assert answer['config'] == redact_config(cfg), sid
        assert SECRET not in json.dumps(answer['config']), sid
        keys = rust._host.call('header_env_keys', {'config': cfg}, timeout=30)
        assert keys == header_env_keys(cfg), sid
        if not is_stdio(cfg) and entry.get('env_specs'):
            assert keys, sid
