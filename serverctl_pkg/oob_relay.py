"""Out-of-band model relay (SELF-REPAIR O3, designs/headless-recovery-agent/DESIGN.md).

Responsibility: give a sandboxed repair session (network namespace) a way to
call the project's model gateway without ever seeing its credential. The
watcher owns this relay on a Unix socket; it injects the project's example-corp
API key into each allowed request and forwards only the chat-completion and
model-list paths.

Key source: the project's own routing credential (P13), revealed through the
audited loopback endpoint while Tofu is up and held in memory only. The
reserved vault entry ``repair.model`` is the fallback when storage is down.
Plaintext never reaches a file, a log, a command argument or the session's
environment. Reports carry key tails only.

Entry points: ``Relay``, ``MemoryKeySource``, ``RevealKeySource``,
``VaultKeySource``. Dependencies: the standard library;
``serverctl_pkg.oob_alert.read_vault_entry`` for the fallback.
"""
from __future__ import annotations

import fcntl
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import Callable

# Only the two generative/read-only gateway faces a repair session needs. The
# TB harness uses the native prefix; a bare OpenAI base URL is also accepted.
ALLOWED_POST_PATHS = ('/v1/chat/completions', '/v1/openai/native/chat/completions')
ALLOWED_GET_PATHS = ('/v1/models', '/v1/openai/native/models')
# The gateway's native OpenAI face (the TB harness's provider config).
DEFAULT_UPSTREAM_BASE = 'https://api.openai.com'
UPSTREAM_TIMEOUT_S = 120.0
# The session's model base URL inside its network namespace: the launcher's loopback bridge listens on
# HTTP's own port there and pipes to the relay socket (no Tofu transport speaks http+unix).
SANDBOX_BASE_URL = 'http://127.0.0.1/v1/openai/native'
_BRIDGE_ADDRESS = ('127.0.0.1', 80)
# Linux <linux/sockios.h> and <net/if.h>: the loopback is down in a new network namespace.
_SIOCGIFFLAGS, _SIOCSIFFLAGS, _IFF_UP = 0x8913, 0x8914, 0x1
# Hop-by-hop headers that must never be forwarded (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers',
    'transfer-encoding', 'upgrade', 'host', 'content-length',
})


def key_hint(secret: str | None) -> str:
    return f'…{secret[-4:]}' if secret else ''


def _api_key_from_secret(secret: str | None) -> str | None:
    """The routing credential's secret envelope is ``{"api_key": ...}`` (TB relay load_keys)."""
    if not secret:
        return None
    try:
        envelope = json.loads(secret)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        secret = str(envelope.get('api_key') or '')
    return secret.strip() or None


class MemoryKeySource:
    """The credential resolved once and kept in memory only."""

    def __init__(self, resolver: Callable[[], str | None]):
        self._resolver = resolver
        self._secret: str | None = None
        self._resolved = False

    def get(self) -> str | None:
        if not self._resolved:
            self._resolved = True
            self._secret = self._resolver()
        return self._secret

    def refresh(self) -> None:
        self._resolved = False

    def hint(self) -> str:
        return key_hint(self.get())


class RevealKeySource:
    """Reveal the project's provider credential through the audited loopback API."""

    def __init__(self, api_origin: str, *, provider: str = 'example-corp', http_get_json=None, http_post_json=None):
        self.api_origin = api_origin.rstrip('/')
        self.provider = provider
        self._http = http_get_json or _http_get_json
        self._post = http_post_json or _http_post_json
        self._memory = MemoryKeySource(self._reveal)

    def _reveal(self) -> str | None:
        status, body, error = self._http(f'{self.api_origin}/api/v4/model-routing', 10.0)
        if status != 200 or not isinstance(body, dict):
            return None
        document = (body.get('data') or {}).get('model_routing') or {}
        accesses = {a.get('provider_access_id') for a in document.get('provider_accesses', [])
                    if a.get('provider_id') == self.provider}
        for credential in document.get('credentials', []):
            if (credential.get('provider_access_id') not in accesses
                    or credential.get('kind') != 'api_key' or not credential.get('enabled')):
                continue
            # POST-only and audit-logged (routes/api_v1/providers.py reveal_credential_secret).
            status, reply, error = self._post(
                f'{self.api_origin}/api/v4/model-routing/credentials/{credential["credential_id"]}/secret/reveal',
                10.0)
            if status == 200:
                secret = _api_key_from_secret((reply.get('data') or {}).get('secret'))
                if secret:
                    return secret
        return None

    def get(self) -> str | None:
        return self._memory.get()

    def refresh(self) -> None:
        self._memory.refresh()

    def hint(self) -> str:
        return self._memory.hint()


class VaultKeySource:
    """The reserved ``repair.model`` entry, read in place, for storage-down fallback."""

    def __init__(self, vault_dir: Path, *, read_entry=None):
        self._vault_dir = vault_dir
        if read_entry is None:
            from serverctl_pkg.oob_alert import read_vault_entry
            read_entry = read_vault_entry
        self._read = read_entry
        self._memory = MemoryKeySource(self._load)

    def _load(self) -> str | None:
        return _api_key_from_secret(self._read(self._vault_dir, 'repair.model'))

    def get(self) -> str | None:
        return self._memory.get()

    def refresh(self) -> None:
        self._memory.refresh()

    def hint(self) -> str:
        return self._memory.hint()


def _http_post_json(url: str, timeout: float):
    import urllib.request
    request = urllib.request.Request(url, data=b'{}', method='POST', headers={'Content-Type': 'application/json'})
    return _http_get_json(request, timeout)


def _http_get_json(url, timeout: float):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8')), ''
    except Exception as error:
        return None, None, f'{type(error).__name__}: {error}'


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    close_connection = True  # one request per Unix connection: the sandbox sees EOF
    relay: 'Relay' = None  # set by Relay.start

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        self._forward('GET')

    def do_POST(self):  # noqa: N802
        self._forward('POST')

    def _forward(self, method: str) -> None:
        if self.relay.stopped:
            self.send_error(503, f'relay stopped: {self.relay.stopped}')
            return
        path = self.path.split('?', 1)[0]
        allowed = path in (ALLOWED_GET_PATHS if method == 'GET' else ALLOWED_POST_PATHS)
        if not allowed:
            self.send_error(404, 'not an allowed relay path')
            return
        secret = self.relay.key_source.get()
        if not secret:
            self.send_error(503, 'no project model credential available')
            return
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        upstream = self.relay.upstream_base
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in _HOP_BY_HOP and name.lower() != 'authorization'}
        headers['Authorization'] = f'Bearer {secret}'
        headers['Content-Length'] = str(len(body))
        parsed = urllib.parse.urlsplit(upstream)
        if parsed.scheme == 'https':
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT_S)
        else:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT_S)
        upstream_path = parsed.path.rstrip('/') + path
        try:
            connection.request(method, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            if rate_limited(response.status, payload):
                self.relay.stop(f'provider rate limit (429) at {time.strftime("%H:%M:%S")}')
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in _HOP_BY_HOP:
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()


def rate_limited(status: int, payload: bytes) -> bool:
    """A provider rate limit: HTTP 429, or a 429 / rate_limit error event inside a 200 stream or body."""
    if status == 429:
        return True
    for line in payload.splitlines():
        text = line[5:].strip() if line.startswith(b'data:') else line.strip()
        if b'429' not in text and b'rate_limit' not in text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue
        error = event.get('error') if isinstance(event, dict) else None
        if isinstance(error, dict) and (str(error.get('code')) == '429' or str(error.get('status')) == '429'
                                        or 'rate_limit' in str(error.get('type') or '')):
            return True
    return False


class Relay:
    """A Unix-socket HTTP relay that injects the project's key into allowed requests.

    It serves one request at a time and stops for good at the provider's first
    rate limit (``rate_limited``): an unattended repair yields the project's
    quota to production traffic instead of retrying into it.
    """

    def __init__(self, key_source, *, upstream_base: str = DEFAULT_UPSTREAM_BASE, socket_path: Path):
        self.key_source = key_source
        self.upstream_base = upstream_base
        self.socket_path = socket_path
        self.stopped = ''
        self._server = None
        self._thread = None

    def stop(self, reason: str) -> None:
        self.stopped = self.stopped or reason

    def reset(self) -> None:
        """After a verified recovery: lift a rate-limit stop and resolve the credential again."""
        self.stopped = ''
        self.key_source.refresh()

    def key_hint(self) -> str:
        return key_hint(self.key_source.get())

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        handler = type('RelayHandler', (_Handler,), {'relay': self})
        self._server = http.server.HTTPServer(('127.0.0.1', 0), handler, bind_and_activate=False)
        self._server.server_bind = self._bind_unix
        self._server.server_activate = lambda: None
        self._server.server_bind()
        self._server.server_activate()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name='oob-repair-relay')
        self._thread.start()

    def _bind_unix(self):
        path = str(self.socket_path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._server.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.socket.bind(path)
        self._server.socket.listen(5)
        self._server.server_address = path

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def preflight(self) -> tuple[bool, str]:
        secret = self.key_source.get()
        if not secret:
            return False, 'no project model credential available'
        return True, f'credential {self.key_hint()} present; relay listens on {self.socket_path}'


# ── the sandbox side: loopback bridge to the relay socket ──

def _loopback_up() -> None:
    """Bring ``lo`` up in this (new, private) network namespace; the mapped root owns it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack('16sH22x', b'lo', 0)
        flags = struct.unpack('16sH', fcntl.ioctl(probe, _SIOCGIFFLAGS, request)[:18])[1]
        fcntl.ioctl(probe, _SIOCSIFFLAGS, struct.pack('16sH22x', b'lo', flags | _IFF_UP))


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _serve_bridge(listener: socket.socket, relay_socket: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return  # the listener closed: the session ended
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(relay_socket)
        except OSError:
            client.close()
            upstream.close()
            continue
        for source, sink in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pipe, args=(source, sink), daemon=True).start()


def bridge(relay_socket: str, argv: list[str]) -> int:
    """Inside the sandbox: bridge ``SANDBOX_BASE_URL`` to the relay socket, run ``argv``, return its exit code."""
    _loopback_up()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(_BRIDGE_ADDRESS)
    listener.listen(16)
    threading.Thread(target=_serve_bridge, args=(listener, relay_socket), daemon=True).start()
    try:
        return subprocess.call(argv)
    finally:
        listener.close()


def bridge_argv(relay_socket: Path, argv: list[str]) -> list[str]:
    """The launcher argv that runs ``argv`` behind the loopback bridge (the sandbox's first process)."""
    return [sys.executable, '-I', str(Path(__file__).resolve()), 'bridge', '--socket', str(relay_socket),
            '--', *argv]


__all__ = ['ALLOWED_GET_PATHS', 'ALLOWED_POST_PATHS', 'MemoryKeySource', 'Relay', 'RevealKeySource',
           'SANDBOX_BASE_URL', 'VaultKeySource', 'bridge', 'bridge_argv', 'key_hint', 'rate_limited']


if __name__ == '__main__':
    if len(sys.argv) > 4 and sys.argv[1] == 'bridge' and sys.argv[2] == '--socket' and sys.argv[4] == '--':
        sys.exit(bridge(sys.argv[3], sys.argv[5:]))
    sys.exit('usage: oob_relay.py bridge --socket <relay socket> -- <argv...>')
