"""The Rust node's subscription egress and adapter sidecar against the Python Home.

Real processes: the Python Home (``live_server``, in this process) and the Rust
node (``tofu node --allow-egress``). A fake CLIProxyAPI sidecar (a Python
script installed as ``<config dir>/adapter/cliproxyapi``, so ``adapter_ensure``
never downloads) serves the loopback target class on the policy port; the
Home's own server-side entry points (``lib.desktop.egress.egress_http`` /
``open_stream`` and ``send_desktop_command``) drive the node. No provider
host is ever contacted: the subscription class is only exercised by refusal.

Covers adapter ensure/status/stop (port policy, config.yaml, health gate,
process lifecycle), egress_http (loopback relay, headers, refusals), the
streamed frames meta → body → done for a multi-frame body, and a mid-stream
cancel that reaches the upstream socket (the fake sees the disconnect).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

import pytest

from tests.desktop.test_rust_node_interop import OWNER, _api, _nodes, _stop, _wait
from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

HOST = ('egress-machine', 'egress-host', 'e' * 64)

FAKE_SIDECAR = r'''
import http.server, json, os, re, sys, time
config = open(sys.argv[sys.argv.index('--config') + 1]).read()
port = int(re.search(r'^port: (\d+)$', config, re.M).group(1))
key = re.search(r'api-keys:\n  - "([^"]*)"', config).group(1)
assert 'host: "127.0.0.1"' in config
root = os.getcwd()

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass
    def _send(self, status, body, ctype='application/json', extra=()):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.headers.get('Authorization') != f'Bearer {key}':
            return self._send(401, b'{}')
        if self.path == '/v1/models':
            return self._send(200, json.dumps({'data': [{'id': 'fake-model'}]}).encode())
        if self.path == '/big':
            return self._send(200, bytes(range(256)) * 1024, 'application/octet-stream')
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Set-Cookie', 'never=relayed')
            self.end_headers()
            try:
                for n in range(600):
                    data = f'data: {n}\n\n'.encode()
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b'0\r\n\r\n')
            except OSError:
                open(os.path.join(root, 'stream-disconnected'), 'w').write(str(n))
            return
        self._send(404, b'{}')
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.headers.get('Authorization') != f'Bearer {key}':
            return self._send(401, b'{}')
        echo = {'path': self.path, 'body': body.decode(), 'probe': self.headers.get('X-Probe')}
        self._send(200, json.dumps(echo).encode(), extra=[('X-Upstream', 'fake'), ('Set-Cookie', 'x=1')])

http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


def _pair(base: str, config: Path, log: Path) -> tuple[subprocess.Popen, str]:
    status, minted = _api(base, 'POST', f'/api/v4/desktop/pair-code?base={base}/', {})
    assert status == 201, minted
    # Only the test Home's own route (the minted LAN ``via`` route may name another port).
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link
    link = build_pairing_link(base, parse_pairing_link(minted['link']).secret)
    env = dict(os.environ, TOFU_DESKTOP_CONFIG=str(config), HOME=str(config.parent),
               NO_PROXY='*', no_proxy='*')
    with log.open('ab') as handle:
        proc = subprocess.Popen(node_command('--simulate-host', *HOST, '--join', link, '--allow-egress',
                                             '--poll-interval', '0.1'),
                                env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    _wait(lambda: 'fingerprint' in log.read_text() or proc.poll() is not None, 'joined')
    assert proc.poll() is None, log.read_text()[-2000:]
    key = subprocess.run(node_command('--simulate-host', *HOST, '--host-key'), capture_output=True,
                         text=True, check=True).stdout.strip()
    return proc, json.loads(config.read_text())['agent_ids'][key]


def _bindable(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def _occupied_port_with_free_successors() -> socket.socket:
    """A listening socket on a port below the ephemeral range whose next
    port is bindable (an ephemeral port can be a live connection's local end)."""
    low = int(Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split()[0]) \
        if Path('/proc/sys/net/ipv4/ip_local_port_range').exists() else 32768
    for base in range(max(1024, low - 9000), low - 16, 9):
        if all(_bindable(p) for p in range(base, base + 8)):
            busy = socket.socket()
            busy.bind(('127.0.0.1', base))
            busy.listen()
            return busy
    pytest.skip('no free loopback port block below the ephemeral range')


def _send(command: str, params: dict, node_id: str, timeout: float = 60):
    from lib.desktop.bridge import send_desktop_command
    return send_desktop_command(command, params, timeout=timeout, target_agent_id=node_id,
                                user_id=OWNER, ttl=timeout)


def test_adapter_and_egress_through_the_rust_node(live_server, tmp_path):
    from lib.desktop import bridge as db
    from lib.desktop import egress

    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    adapter_root = config.parent / 'adapter'
    adapter_root.mkdir()
    binary = adapter_root / 'cliproxyapi'
    binary.write_text(f'#!{sys.executable}\n{FAKE_SIDECAR}')
    binary.chmod(0o755)
    log = tmp_path / 'node.log'
    proc = node_id = None
    sidecar_pid = 0
    try:
        proc, node_id = _pair(live_server, config, log)
        status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
        assert status == 200, body
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{node_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'egress': True}})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[node_id]['online'] and _nodes(live_server)[node_id]['tiers']['egress'],
              'egress effective')

        # ensure: a busy desired port moves to the next free one; config is loopback-only and keyed.
        busy = _occupied_port_with_free_successors()
        desired = busy.getsockname()[1]
        result, error = _send('adapter_ensure', {'port': desired, 'api_key': 'ta_test-key',
                                                 'mgmt_secret': 'm' * 64, 'auto_update': False}, node_id)
        busy.close()
        assert error is None and result['running'] is True, (error, result, log.read_text()[-3000:])
        port = result['port']
        sidecar_pid = result['pid']
        assert port != desired and desired < port < desired + 8
        assert result['installed'] and result['active'] and result['auto_update'] is False
        policy = json.loads((adapter_root / 'policy.json').read_text())
        assert policy['port'] == port and policy['api_key'] == 'ta_test-key' and policy['active'] is True
        yaml = (adapter_root / 'config.yaml').read_text()
        assert 'host: "127.0.0.1"' in yaml and 'allow-remote: false' in yaml
        assert oct((adapter_root / 'config.yaml').stat().st_mode & 0o777) == '0o600'
        result, error = _send('adapter_status', {}, node_id)
        assert error is None and result['running'] and result['pid'] == sidecar_pid

        # egress_http over the loopback class: the node relays to its own adapter port only.
        auth = {'Authorization': 'Bearer ta_test-key', 'X-Probe': 'p1'}
        response = egress.egress_http(f'http://127.0.0.1:{port}/v1/echo', method='POST', headers=auth,
                                      body=b'hello upstream', user_id=OWNER, agent_id=node_id,
                                      target='loopback', loopback_port=port)
        assert response.status_code == 200
        assert response.json() == {'path': '/v1/echo', 'body': 'hello upstream', 'probe': 'p1'}
        assert response.headers.get('X-Upstream') == 'fake'
        assert not {k.lower() for k in response.headers} & {'set-cookie'}
        response = egress.egress_http(f'http://127.0.0.1:{port}/big', method='GET', headers=auth,
                                      user_id=OWNER, agent_id=node_id, target='loopback', loopback_port=port)
        assert response.content == bytes(range(256)) * 1024

        # Refusals by the node itself (the server-side pre-check is bypassed on purpose).
        result, error = _send('egress_http', {'url': f'http://127.0.0.1:{port + 1}/', 'target': 'loopback'},
                              node_id)
        assert 'whitelist' in str(error or result.get('error')), (result, error)
        result, error = _send('egress_http', {'url': 'https://example.com/', 'target': 'subscription'}, node_id)
        assert 'whitelist' in str(error or result.get('error')), (result, error)
        result, error = _send('egress_http', {'url': f'http://127.0.0.1:{port}/', 'target': 'loopback',
                                              'method': 'DELETE'}, node_id)
        assert 'unsupported method' in str(error or result.get('error')), (result, error)

        # Streamed: a multi-frame body (64 KiB frames) arrives whole, then the stream is done.
        reader = egress.open_stream(f'http://127.0.0.1:{port}/big', method='GET', headers=auth,
                                    user_id=OWNER, agent_id=node_id, target='loopback', loopback_port=port)
        assert reader.status_code == 200
        assert b''.join(reader.iter_content()) == bytes(range(256)) * 1024
        frames, done = db.get_frames(reader._cmd_id)
        assert done and [s for _, s, _ in frames].count('body') >= 4
        assert frames[0][1] == 'meta' and json.loads(frames[0][2])['status'] == 200

        # Mid-stream cancel: closing the reader sends egress_cancel; the node shuts the
        # upstream socket and the fake upstream sees the disconnect long before its end.
        marker = adapter_root / 'stream-disconnected'
        reader = egress.open_stream(f'http://127.0.0.1:{port}/stream', method='GET', headers=auth,
                                    user_id=OWNER, agent_id=node_id, target='loopback', loopback_port=port)
        assert reader.status_code == 200
        assert 'set-cookie' not in {k.lower() for k in reader.headers}
        lines = []
        for line in reader.iter_lines():
            if line:
                lines.append(line)
            if len(lines) >= 3:
                break
        assert lines == ['data: 0', 'data: 1', 'data: 2']
        reader.close()
        _wait(marker.exists, 'upstream saw the cancel', timeout=20)
        assert int(marker.read_text()) < 300
        _wait(lambda: (db.get_frames(reader._cmd_id) or ([], True))[1], 'cancelled stream settled', timeout=20)
        assert 'cancelled in-flight stream' in log.read_text()

        # stop: inactive policy, the sidecar process is gone.
        result, error = _send('adapter_stop', {}, node_id)
        assert error is None and result['running'] is False and result['active'] is False
        _wait(lambda: subprocess.run(['kill', '-0', str(sidecar_pid)], capture_output=True).returncode != 0,
              'sidecar exited', timeout=15)
        assert json.loads((adapter_root / 'policy.json').read_text())['active'] is False
        log_text = log.read_text()
        assert 'ta_test-key' not in log_text and 'hello upstream' not in log_text and 'm' * 64 not in log_text
    finally:
        _stop(proc)
        if sidecar_pid:
            try:
                os.kill(sidecar_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for listed in list(_nodes(live_server)):
            _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{listed}')
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()


def test_a_resumed_link_restarts_the_ensured_adapter(live_server, tmp_path):
    """maybe_resume: an active policy brings the sidecar back when an egress link starts."""
    from lib.desktop import bridge as db

    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    adapter_root = config.parent / 'adapter'
    adapter_root.mkdir()
    (adapter_root / 'cliproxyapi').write_text(f'#!{sys.executable}\n{FAKE_SIDECAR}')
    (adapter_root / 'cliproxyapi').chmod(0o755)
    busy = _occupied_port_with_free_successors()
    port = busy.getsockname()[1] + 1
    busy.close()
    (adapter_root / 'policy.json').write_text(json.dumps(
        {'port': port, 'api_key': 'ta_resume', 'mgmt_secret': 's' * 64, 'version': 'latest',
         'auto_update': False, 'active': True}))
    log = tmp_path / 'node.log'
    proc = None
    try:
        proc, _node_id = _pair(live_server, config, log)
        import urllib.request

        def healthy():
            request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',
                                             headers={'Authorization': 'Bearer ta_resume'})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as r:
                    return r.status == 200
            except OSError:
                return False
        _wait(healthy, 'resumed sidecar healthy', timeout=30)
        assert 'resuming sidecar' in log.read_text()
        assert re.search(r'host: "127\.0\.0\.1"', (adapter_root / 'config.yaml').read_text())
    finally:
        _stop(proc)
        # The sidecar outlives the node by design (it has its own session), as with the Python node.
        subprocess.run(['pkill', '-f', str(adapter_root / 'cliproxyapi')], check=False)
        for listed in list(_nodes(live_server)):
            _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{listed}')
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()
