"""Out-of-band model relay (serverctl_pkg/oob_relay.py, SELF-REPAIR O3).

Specification of designs/headless-recovery-agent/DESIGN.md, piece O3: the
repair session runs in a network namespace and reaches the project's model
gateway only through a Unix-socket relay that injects the project's sankuai
credential. The key is revealed through the audited loopback endpoint while
Tofu is up and held in memory only; the reserved vault entry ``repair.model``
is the fallback when storage is down. The agent never sees the key.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from serverctl_pkg import oob_relay
from tests.support.production_api_guard import production_api_guard  # noqa: F401 - autouse guard

pytestmark = pytest.mark.unit

SECRET = 'sk-live-project-credential-199c'


class _Upstream(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, bytes]] = []

    def do_GET(self):  # noqa: N802
        _Upstream.requests.append((self.path, dict(self.headers), b''))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length)
        _Upstream.requests.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_):
        pass


@pytest.fixture
def upstream():
    _Upstream.requests = []
    server = HTTPServer(('127.0.0.1', 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _relay(tmp_path, upstream, *, key=SECRET, origin=None):
    source = oob_relay.MemoryKeySource(lambda: key)
    base = f'http://127.0.0.1:{upstream.server_address[1]}' if upstream is not None else 'http://127.0.0.1:9'
    return oob_relay.Relay(source, upstream_base=base, socket_path=tmp_path / 'relay.sock')


def _request(sock: Path, path='/v1/chat/completions', body=b'{}', headers=None, method='POST'):
    lines = [f'{method} {path} HTTP/1.1', 'Host: relay', 'Content-Length: %d' % len(body)]
    for name, value in (headers or {}).items():
        lines.append(f'{name}: {value}')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(sock))
        client.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode() + body)
        client.settimeout(5)
        raw = b''
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
    return raw


def test_the_relay_injects_the_project_credential_and_strips_the_incoming_one(tmp_path, upstream):
    relay = _relay(tmp_path, upstream)
    relay.start()
    try:
        raw = _request(tmp_path / 'relay.sock', body=b'{"model": "m"}',
                       headers={'Authorization': 'Bearer attacker', 'X-Custom': 'keep'})
    finally:
        relay.close()
    assert raw.startswith(b'HTTP/1.1 200')
    path, headers, body = _Upstream.requests[0]
    assert path == '/v1/chat/completions' and body == b'{"model": "m"}'
    assert headers['Authorization'] == f'Bearer {SECRET}'
    assert headers['X-Custom'] == 'keep'
    assert SECRET.encode() not in raw


def test_the_relay_only_forwards_the_allowlisted_paths(tmp_path, upstream):
    relay = _relay(tmp_path, upstream)
    relay.start()
    try:
        denied = _request(tmp_path / 'relay.sock', path='/v1/other')
        models = _request(tmp_path / 'relay.sock', path='/v1/models', method='GET', body=b'')
    finally:
        relay.close()
    assert denied.startswith(b'HTTP/1.1 404')
    assert models.startswith(b'HTTP/1.1 200')
    assert [path for path, _, _ in _Upstream.requests] == ['/v1/models']


def test_the_key_is_never_written_to_disk_or_returned(tmp_path, upstream):
    relay = _relay(tmp_path, upstream)
    relay.start()
    try:
        _request(tmp_path / 'relay.sock')
    finally:
        relay.close()
    for path in tmp_path.rglob('*'):
        if path.is_file() and path.name != 'relay.sock':
            assert SECRET not in path.read_text(errors='replace')
    assert relay.key_hint() == '…199c'


def test_preflight_fails_when_no_credential_is_available(tmp_path):
    relay = _relay(tmp_path, upstream=None, key=None)
    assert relay.preflight() == (False, 'no project model credential available')


def test_key_source_refreshes_only_when_asked():
    calls = []
    source = oob_relay.MemoryKeySource(lambda: calls.append(1) or SECRET)
    assert source.get() == SECRET
    assert source.get() == SECRET and len(calls) == 1
    source.refresh()
    source.get()
    assert len(calls) == 2 and source.get() == SECRET
    assert source.hint() == '…199c'


class _RoutingApi(BaseHTTPRequestHandler):
    """A scratch Tofu's model-routing API. The reveal route is POST-only, as in production
    (routes/api_v1/providers.py reveal_credential_secret): a GET is refused with 405."""
    calls: list[tuple[str, str]] = []

    def _json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        _RoutingApi.calls.append(('GET', self.path))
        if self.path == '/api/v4/model-routing':
            self._json(200, {'data': {'model_routing': {
                'credentials': [{'credential_id': 'c1', 'provider_access_id': 'pa1', 'kind': 'api_key',
                                 'enabled': True, 'key_hint': '…199c'}],
                'provider_accesses': [{'provider_access_id': 'pa1', 'provider_id': 'sankuai'}]}}})
        else:
            self._json(405, {'error': 'method not allowed'})

    def do_POST(self):  # noqa: N802
        _RoutingApi.calls.append(('POST', self.path))
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path == '/api/v4/model-routing/credentials/c1/secret/reveal':
            self._json(200, {'data': {'credential_id': 'c1', 'secret': json.dumps({'api_key': SECRET})}})
        else:
            self._json(404, {'error': 'not found'})

    def log_message(self, *_):
        pass


def test_reveal_key_source_reads_the_audited_loopback_endpoint_with_its_post(tmp_path):
    _RoutingApi.calls = []
    api = HTTPServer(('127.0.0.1', 0), _RoutingApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        source = oob_relay.RevealKeySource(f'http://127.0.0.1:{api.server_address[1]}', provider='sankuai')
        assert source.get() == SECRET and source.hint() == '…199c'
    finally:
        api.shutdown()
        api.server_close()
    assert _RoutingApi.calls == [('GET', '/api/v4/model-routing'),
                                 ('POST', '/api/v4/model-routing/credentials/c1/secret/reveal')]


def test_vault_key_source_reads_the_reserved_entry(tmp_path):
    vault = tmp_path / 'config'
    vault.mkdir()
    (vault / '.credentials_vault.key').write_text('x')
    (vault / 'credentials_vault.json').write_text(json.dumps({'entries': {}}))

    def read_entry(directory, name):
        assert directory == vault and name == 'repair.model'
        return json.dumps({'api_key': SECRET})
    source = oob_relay.VaultKeySource(vault, read_entry=read_entry)
    assert source.get() == SECRET and source.hint() == '…199c'
    assert oob_relay.VaultKeySource(vault, read_entry=lambda d, n: None).get() is None


class _Throttled(BaseHTTPRequestHandler):
    """An upstream that rate-limits: a 429 status, or a 200 stream whose error event carries 429."""
    mode = 'status'
    calls = 0

    def do_POST(self):  # noqa: N802
        _Throttled.calls += 1
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if _Throttled.mode == 'status':
            self.send_response(429)
            body = b'{"error": {"code": 429, "message": "rate limited"}}'
            self.send_header('Content-Type', 'application/json')
        else:
            self.send_response(200)
            body = (b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
                    b'data: {"error": {"code": 429, "type": "rate_limit_exceeded"}}\n\n')
            self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@pytest.mark.parametrize('mode', ['status', 'in-stream'])
def test_the_relay_stops_at_the_first_429_including_in_stream(tmp_path, mode):
    # O7 condition (c9b62a60): stop at the first 429. The repair session yields the project's quota to
    # production traffic; the provider's own rate-limit signal ends the relay, never a retry.
    _Throttled.mode, _Throttled.calls = mode, 0
    server = HTTPServer(('127.0.0.1', 0), _Throttled)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = oob_relay.Relay(oob_relay.MemoryKeySource(lambda: SECRET),
                            upstream_base=f'http://127.0.0.1:{server.server_address[1]}',
                            socket_path=tmp_path / 'relay.sock')
    relay.start()
    try:
        first = _request(tmp_path / 'relay.sock')
        second = _request(tmp_path / 'relay.sock')
    finally:
        relay.close()
        server.shutdown()
        server.server_close()
    assert first.split(b'\r\n', 1)[0] == (b'HTTP/1.1 429 Too Many Requests' if mode == 'status' else b'HTTP/1.1 200 OK')
    assert second.startswith(b'HTTP/1.1 503') and b'provider rate limit (429)' in second
    assert _Throttled.calls == 1 and relay.stopped.startswith('provider rate limit (429)')


def _namespaces_available() -> bool:
    """A bounded probe: on a loaded host the namespace clone itself can stall, so
    an unavailable answer is reported as a skip, never as a hang."""
    import subprocess
    try:
        probe = subprocess.Popen(['unshare', '-Urnp', '--fork', 'true'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return probe.wait(timeout=15) == 0
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait(timeout=5)
        return False


@pytest.mark.skipif(not _namespaces_available(), reason='user, network and PID namespaces are not permitted here')
def test_a_network_namespace_reaches_the_relay_only_through_its_unix_socket(tmp_path, upstream):
    """The O3 claim: a sandboxed session has no network, yet it can still call the model relay."""
    import subprocess
    import sys
    import textwrap
    relay = _relay(tmp_path, upstream)
    relay.start()
    script = textwrap.dedent(f'''
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect({str(tmp_path / 'relay.sock')!r})
        body = b'{{"model": "m"}}'
        s.sendall(b'POST /v1/openai/native/chat/completions HTTP/1.1\\r\\nHost: relay\\r\\nContent-Length: %d\\r\\n\\r\\n' % len(body) + body)
        print(s.recv(65536).decode().splitlines()[0])
    ''')
    try:
        # close_fds keeps the relay's listening socket out of the namespace;
        # otherwise server_close() waits on a connection held open there.
        result = subprocess.run(['unshare', '--map-root-user', '--net', '--pid', '--kill-child', '--',
                                 sys.executable, '-I', '-c', script], capture_output=True, text=True, timeout=30,
                                close_fds=True)
    finally:
        relay.close()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith('HTTP/1.1 200')
    path, headers, _ = _Upstream.requests[0]
    assert path == '/v1/openai/native/chat/completions' and headers['Authorization'] == f'Bearer {SECRET}'


@pytest.mark.skipif(not _namespaces_available(), reason='user, network and PID namespaces are not permitted here')
def test_an_http_client_in_the_sandbox_reaches_the_relay_through_the_loopback_bridge_only(tmp_path, upstream):
    """tofu-agent's model client speaks HTTP to a base URL; no Tofu transport implements http+unix.

    The sandbox launcher brings loopback up inside the namespace and bridges http://127.0.0.1 (HTTP's
    own port) to the relay socket, so the session's base URL is ``oob_relay.SANDBOX_BASE_URL`` and
    nothing else on the host is reachable.
    """
    import subprocess
    import sys
    import textwrap
    from serverctl_pkg import oob_repair
    relay = _relay(tmp_path, upstream)
    relay.start()
    client = textwrap.dedent(f'''
        import json, urllib.request, urllib.error
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
        request = urllib.request.Request({oob_relay.SANDBOX_BASE_URL!r} + '/chat/completions',
                                         data=b'{{"model": "m"}}', headers={{'Content-Type': 'application/json'}})
        with opener.open(request, timeout=20) as response:
            print(response.status, response.read().decode())
        try:
            opener.open('http://127.0.0.1:{upstream.server_address[1]}/v1/models', timeout=5)
            print('HOST REACHED')
        except (urllib.error.URLError, OSError) as error:
            print('host unreachable:', type(error).__name__)
    ''')
    argv = oob_repair.sandbox_argv([sys.executable, '-I', '-c', client], relay_socket=tmp_path / 'relay.sock')
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, close_fds=True)
    finally:
        relay.close()
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == '200 {"ok": true}' and lines[1].startswith('host unreachable')
    path, headers, _ = _Upstream.requests[0]
    assert path == '/v1/openai/native/chat/completions' and headers['Authorization'] == f'Bearer {SECRET}'
