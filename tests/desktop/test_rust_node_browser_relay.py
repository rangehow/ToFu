"""The Rust node behind an SSO edge: pairing and polls carried by the signed-in page.

Real processes against the Python Home (``live_server``). A recording SSO edge
(a reverse proxy that refuses every request without the browser's login cookie
with a gateway 401) stands in front of the Home; the pairing link names only
that edge. The Rust node cannot pass it, so it opens its loopback browser relay
(packages/tofu-node/src/relay.rs). A simulated signed-in page speaks the page
side of the relay protocol exactly as frontend/src/features/devices/relay.ts
does (discover 127.0.0.1:15180-15189 by ``/v1/status`` with its Origin, take a
job, check its target path, send it to the Home with its cookie, return status
and body); it redeems the pairing on the node's behalf, and every later poll
rides the page, so the Home records route ``browser_relay``.

Also: the packaged app's local link (``--local-server`` with the launcher's
process capability in ``TOFU_DESKTOP_AGENT_TOKEN``) reaches the Home without
pairing and keeps polling.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from tests.desktop.test_rust_node_interop import OWNER, _api, _nodes, _spawn, _stop, _wait
from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

RELAY_PORTS = range(15180, 15190)  # relay.ts BROKER_PORT_FIRST..LAST
COOKIE = 'tofu_sso=signed-in'
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _send(url: str, *, method='GET', body: bytes | None = None, headers=None, timeout=15.0):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _DIRECT.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class SsoEdge:
    """A gateway that forwards to the Home only requests carrying the login cookie."""

    def __init__(self, home: str):
        self.home = home
        self.forwarded: list[str] = []
        self.refused: list[str] = []
        edge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _relay(self):
                size = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(size) if size else None
                if COOKIE not in (self.headers.get('Cookie') or ''):
                    edge.refused.append(self.path)
                    return self._answer(401, b'{"error":"Unauthorized"}')
                edge.forwarded.append(self.path)
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() in ('content-type', 'accept', 'x-bridge-secret')}
                status, payload = _send(edge.home + self.path, method=self.command, body=body,
                                        headers=headers, timeout=30)
                self._answer(status, payload)

            do_GET = do_POST = _relay

            def _answer(self, status, payload):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


class SignedInPage:
    """The page side of the relay protocol (frontend/src/features/devices/relay.ts)."""

    def __init__(self, page_base: str):
        self.base = page_base  # the Home as this tab sees it (the SSO edge)
        self.origin = page_base  # scheme://host:port, no path
        self.carried: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def _local(self, url, *, method='GET', body=None, timeout=13.0):
        headers = {'Origin': self.origin}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        return _send(url, method=method, body=body, headers=headers, timeout=timeout)

    def _discover(self) -> str:
        for port in RELAY_PORTS:
            broker = f'http://127.0.0.1:{port}'
            try:
                status, body = self._local(broker + '/v1/status', timeout=0.65)
                if status == 200 and json.loads(body).get('kind') == 'tofu-agent-browser-relay':
                    return broker
            except (OSError, ValueError):
                continue
        return ''

    def _forward(self, broker: str, job: dict) -> None:
        target = job.get('url') or ''
        is_pair = target == self.base + '/api/desktop/pair'
        if not is_pair and target != self.base + '/api/desktop/poll':
            payload = {'id': job['id'], 'status': 409, 'body': json.dumps({'error': 'relay_target_mismatch'})}
        else:
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Cookie': COOKIE}
            secret = (job.get('headers') or {}).get('X-Bridge-Secret')
            if secret and not is_pair:
                headers['X-Bridge-Secret'] = secret
            status, body = _send(target, method='POST', body=json.dumps(job.get('payload') or {}).encode(),
                                 headers=headers, timeout=30)
            self.carried.append(target.rsplit('/', 1)[-1])
            payload = {'id': job['id'], 'status': status, 'body': body.decode()}
        self._local(broker + '/v1/result', method='POST', body=json.dumps(payload).encode(), timeout=5)

    def _watch(self) -> None:
        while not self.stop.is_set():
            broker = self._discover()
            if not broker:
                self.stop.wait(0.3)
                continue
            try:
                while not self.stop.is_set():
                    status, body = self._local(broker + '/v1/take')
                    if status == 204:
                        continue
                    if status != 200:
                        break
                    self._forward(broker, json.loads(body))
            except (OSError, ValueError):
                continue  # the broker went away (pairing relay closed): rediscover

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join(timeout=20)


def test_the_signed_in_page_pairs_the_rust_node_and_carries_its_polls(live_server, tmp_path):
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link

    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    log = tmp_path / 'node.log'
    host = ('relay-machine', 'relay-host', 'e' * 64)
    proc = node_id = None
    with SsoEdge(live_server) as edge, SignedInPage(edge.url) as page:
        try:
            status, minted = _api(live_server, 'POST', f'/api/v4/desktop/pair-code?base={live_server}/', {})
            assert status == 201, minted
            # The link names only the SSO edge (no LAN ``via`` route): the page is the one way in.
            link = build_pairing_link(edge.url, parse_pairing_link(minted['link']).secret)
            proc = _spawn(config, log, '--join', link, '--poll-interval', '0.2', host=host)
            _wait(lambda: 'fingerprint' in log.read_text() or proc.poll() is not None, 'joined', timeout=90)
            assert proc.poll() is None, log.read_text()[-3000:]
            assert '/api/desktop/pair' in edge.refused  # the direct redeem hit the SSO wall
            assert 'pair' in page.carried
            key = subprocess.run(node_command('--simulate-host', *host, '--host-key'), capture_output=True,
                                 text=True, check=True).stdout.strip()
            node_id = json.loads(config.read_text())['agent_ids'][key]
            saved = json.loads(config.read_text())
            rows = saved['attachments'] or next(iter(saved['host_attachments'].values()))
            assert rows[0]['url'] == edge.url and rows[0]['state'] == 'active'

            pending = _wait(lambda: _nodes(live_server).get(node_id), 'listed')
            assert pending['state'] == 'pending' and pending['fingerprint'] in log.read_text()
            status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
            assert status == 200, body
            node = _wait(lambda: (n := _nodes(live_server).get(node_id)) and n['online'] and n, 'online',
                         timeout=60)
            # The Home records the route of the node's latest admitted poll.
            _wait(lambda: _nodes(live_server)[node_id]['route'] == 'browser_relay', 'route browser_relay')
            assert node['kind'] == 'server'
            # Only the page holds the login cookie: every poll the edge forwarded came through it.
            assert '/api/desktop/poll' in edge.forwarded
        finally:
            _stop(proc)
            if node_id:
                _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{node_id}')


def test_the_local_link_uses_the_launchers_process_capability(live_server, tmp_path):
    from lib.bridge_auth import process_agent_token
    from lib.desktop import bridge as db

    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    log = tmp_path / 'node.log'
    env = dict(os.environ, TOFU_DESKTOP_CONFIG=str(config), HOME=str(config.parent), NO_PROXY='*',
               no_proxy='*', TOFU_DESKTOP_AGENT_TOKEN=process_agent_token())
    proc = None
    try:
        with log.open('ab') as handle:
            proc = subprocess.Popen(
                node_command('--simulate-host', 'local-machine', 'local-host', 'f' * 64,
                             '--local-server', live_server, '--poll-interval', '0.2'),
                env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        key = subprocess.run(node_command('--simulate-host', 'local-machine', 'local-host', 'f' * 64,
                                          '--host-key'), env=env, capture_output=True, text=True,
                             check=True).stdout.strip()
        node_id = _wait(lambda: json.loads(config.read_text()).get('agent_ids', {}).get(key)
                        if config.exists() else None, 'identity')
        _wait(lambda: any(a['agent_id'] == node_id and a['online'] for a in db.list_agents(user_id=OWNER)),
              'local link online')
        assert proc.poll() is None, log.read_text()[-2000:]
        _wait(lambda: f'Link status ({live_server}): Ok' in log.read_text(), 'local link reported ok')
        # Never joined anything: the capability is the only credential.
        saved = json.loads(config.read_text())
        assert not saved.get('attachments') and not any((saved.get('host_attachments') or {}).values())
    finally:
        _stop(proc)
        with db.command_queue_lock:
            db._agents.clear()


def test_a_local_server_without_the_capability_is_a_usage_error(tmp_path):
    env = dict(os.environ, TOFU_DESKTOP_CONFIG=str(tmp_path / 'c.json'))
    env.pop('TOFU_DESKTOP_AGENT_TOKEN', None)
    done = subprocess.run(node_command('--local-server', 'http://127.0.0.1:1'), env=env,
                          capture_output=True, text=True)
    assert done.returncode == 2 and 'TOFU_DESKTOP_AGENT_TOKEN' in done.stderr
    assert Path(tmp_path / 'c.json').exists() is False
