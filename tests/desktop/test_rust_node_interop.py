"""The Rust node program (``tofu node``, packages/tofu-node) against the Python Home.

Real processes: the Rust node binary talks to (1) a recording fake Home, so every
frame it puts on the wire is validated against contracts/desktop_node_v1 and
contracts/desktop_bridge_v3, and (2) the real Python Home (``live_server``), so
the multi-device invariants hold end to end with the Rust node:

- confirm before join: a joined node is pending and unaddressable until the
  owner confirms it;
- read-only default and permission intersection: the Home ceiling AND the
  node's own switch, enforced on both ends;
- an unaddressed call with several online devices is refused unless a default
  is marked for the kind;
- twins sharing one home directory (one config file) are distinct nodes with
  distinct credentials, and the Rust and Python nodes derive the same slot key.

The two-process Home (API host + execution host) runs in
tests/chaos/test_chaos_nodes.py, parametrized over both node implementations.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import jsonschema
import pytest

from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

_REPO = Path(__file__).resolve().parents[2]
OWNER = '1'


def _schema(name: str) -> dict:
    return json.loads((_REPO / 'contracts' / name).read_text())


def _validate(document, schema: dict, definition: str | None = None) -> None:
    target = dict(schema['$defs'][definition], **{'$defs': schema['$defs']}) if definition else schema
    jsonschema.Draft202012Validator(target).validate(document)


def _spawn(config: Path, log: Path, *args: str, host=('machine-a', 'host-a', 'a' * 64)):
    env = dict(os.environ, TOFU_DESKTOP_CONFIG=str(config), HOME=str(config.parent),
               NO_PROXY='*', no_proxy='*')
    handle = log.open('ab')
    try:
        return subprocess.Popen(node_command('--simulate-host', *host, *args), env=env,
                                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        handle.close()


def _stop(proc) -> None:
    if proc is not None and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _wait(predicate, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f'never {what}')


# ── wire frames against the contracts ─────────────────────────────────────────

class _FakeHome:
    """Records every pair and poll body; answers pending, then one command batch."""

    def __init__(self):
        from lib.desktop_wire.node_protocol import fingerprint
        self.fingerprint = fingerprint
        self.pairs, self.polls, self.responses = [], [], []
        self.confirmed = threading.Event()
        self.node_id = ''
        home = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
                if self.path == '/api/desktop/pair':
                    home.pairs.append(body)
                    home.node_id = body['node_id']
                    answer = {'id': 'cred-1', 'name': body.get('name', ''), 'token': 'secret-token',
                              'scopes': ['agents:bridge'], 'owner_id': 1, 'node_id': body['node_id'],
                              'fingerprint': fingerprint(body['node_id'], 'cred-1'),
                              'node_state': 'pending_confirmation', 'kind': body['kind']}
                    return self._send(201, {'ok': True, 'data': answer})
                if self.path == '/api/desktop/poll':
                    assert self.headers['X-Bridge-Secret'] == 'secret-token'
                    home.polls.append(body)
                    answer = home.answer(body)
                    home.responses.append(answer)
                    return self._send(200, answer)
                self._send(404, {})

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.sent = False

    def answer(self, poll):
        now = time.time()
        base = {'protocol_version': 3, 'server_time': now, 'commands': []}
        if not self.confirmed.is_set():
            return dict(base, node={'state': 'pending_confirmation',
                                    'fingerprint': self.fingerprint(self.node_id, 'cred-1')})
        if not self.sent:
            self.sent = True
            ceiling = {'read': True, 'write': False, 'exec': False, 'gui': False, 'egress': False,
                       'codex_login': False}
            target = poll['agent']['agent_id']
            base['commands'] = [
                {'id': 'read-1', 'type': 'desktop_system_info', 'params': {'type': 'overview'},
                 'target_agent_id': target, 'expires_at': now + 90, 'deadline_enforced': False,
                 'home_tiers': ceiling},
                {'id': 'write-1', 'type': 'desktop_write_file', 'params': {'path': '/nonexistent/x', 'content': 'x'},
                 'target_agent_id': target, 'expires_at': now + 90, 'deadline_enforced': False,
                 'home_tiers': ceiling},
                {'id': 'gui-1', 'type': 'desktop_screenshot', 'params': {},
                 'target_agent_id': target, 'expires_at': now + 90, 'deadline_enforced': False,
                 'home_tiers': dict(ceiling, gui=True)},
            ]
        return base

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


def test_every_frame_the_rust_node_sends_matches_the_contracts(tmp_path):
    node_schema, bridge_schema = _schema('desktop_node_v1.schema.json'), _schema('desktop_bridge_v3.schema.json')
    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    proc = None
    with _FakeHome() as home:
        try:
            proc = _spawn(config, tmp_path / 'node.log', '--join', f'{home.url} 123456',
                          '--allow-write', '--poll-interval', '0.05')
            _wait(lambda: len(home.polls) >= 2, 'polled while pending')
            log = (tmp_path / 'node.log').read_text()
            assert home.fingerprint(home.node_id, 'cred-1') in log  # the node shows the fingerprint
            home.confirmed.set()
            _wait(lambda: {r['id'] for p in home.polls for r in p['results']} >= {'read-1', 'write-1', 'gui-1'},
                  'reported every result')
        finally:
            _stop(proc)
    # Pairing: one PairRequest; the headless join declares kind=server.
    assert len(home.pairs) == 1
    _validate(home.pairs[0], node_schema, 'PairRequest')
    assert home.pairs[0]['kind'] == 'server' and home.pairs[0]['code'] == '123456'
    # Every poll frame is an AgentRegistration; a pending node received no work.
    for poll in home.polls:
        _validate(poll['agent'], bridge_schema, 'AgentRegistration')
        assert poll['agent']['protocol_version'] == 3 and poll['agent']['route'] == 'direct'
        assert poll['agent']['agent_id'] == home.node_id
    for response in home.responses:
        _validate(response, bridge_schema)
    assert len({p['agent']['boot_id'] for p in home.polls}) == 1
    capabilities = home.polls[-1]['agent']['capabilities']
    # Device switch: write on, exec and GUI off.
    assert capabilities['write'] is True and capabilities['exec'] is False and capabilities['gui'] is False
    assert capabilities['paper_build'] == 0
    results = {r['id']: r for p in home.polls for r in p['results']}
    assert results['read-1']['error'] is None and results['read-1']['result']
    # Permission intersection on the node: its switch allows write, the Home ceiling does not.
    assert results['write-1']['error'].startswith('permission_denied')
    # The Home allows GUI here, the node's switch does not.
    assert results['gui-1']['error'] == 'Command desktop_screenshot requires --allow-gui flag'
    # The saved attachment is a contract Attachment.
    saved = json.loads(config.read_text())
    rows = saved['attachments'] or next(iter(saved['host_attachments'].values()))
    attachment = rows[0]
    _validate(attachment, node_schema, 'Attachment')
    assert attachment['fingerprint'] == home.fingerprint(home.node_id, 'cred-1')


def test_rust_and_python_nodes_derive_the_same_host_slot():
    host = ('image-machine', 'shared-host', 'c' * 64)
    rust = subprocess.run(node_command('--simulate-host', *host, '--host-key'), capture_output=True,
                          text=True, check=True).stdout.strip()
    python = subprocess.run([sys.executable, '-m', 'tests.chaos.node_main', *host, '--host-key'],
                            cwd=str(_REPO), capture_output=True, text=True, check=True,
                            env=dict(os.environ, PYTHONPATH=str(_REPO))).stdout.strip().splitlines()[-1]
    assert rust == python
    twin = subprocess.run(node_command('--simulate-host', host[0], host[1], 'd' * 64, '--host-key'),
                          capture_output=True, text=True, check=True).stdout.strip()
    assert twin != rust  # containers sharing machine id, hostname and $HOME differ


# ── the real Python Home ──────────────────────────────────────────────────────

def _api(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b'{}')
            return response.status, payload.get('data', payload) if isinstance(payload, dict) else payload
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors='replace')


def _nodes(base: str) -> dict:
    status, body = _api(base, 'GET', '/api/v4/desktop/nodes')
    assert status == 200, body
    return {n['node_id']: n for n in body['nodes'] if n['node_id']}


def _pair(base: str, config: Path, log: Path, host) -> tuple[subprocess.Popen, str]:
    status, minted = _api(base, 'POST', f'/api/v4/desktop/pair-code?base={base}/', {})
    assert status == 201, minted
    # The minted link is used as is: its LAN route names this Home's own port
    # (tests/desktop/test_pair_code_lan_route.py), never the inherited PORT.
    proc = _spawn(config, log, '--join', minted['link'], '--allow-write', '--poll-interval', '0.2', host=host)
    _wait(lambda: 'fingerprint' in log.read_text() or proc.poll() is not None, 'joined')
    assert proc.poll() is None, log.read_text()[-2000:]
    cfg = json.loads(config.read_text())
    key = subprocess.run(node_command('--simulate-host', *host, '--host-key'), capture_output=True,
                         text=True, check=True).stdout.strip()
    return proc, cfg['agent_ids'][key]


def test_rust_nodes_on_the_python_home_keep_the_multi_device_invariants(live_server, tmp_path):
    from lib.desktop import bridge as db
    from lib.tasks_pkg.handlers.misc import _agents

    shared = tmp_path / 'shared-home' / 'desktop_agent.json'  # one network home for both containers
    shared.parent.mkdir()
    host_a = ('image-machine', 'shared-host', 'a' * 64)
    host_b = ('image-machine', 'shared-host', 'b' * 64)
    first = second = None
    effect = tmp_path / 'effect.txt'
    try:
        # Confirm before join: pending, listed with its fingerprint, unaddressable.
        first, first_id = _pair(live_server, shared, tmp_path / 'a.log', host_a)
        pending = _wait(lambda: _nodes(live_server).get(first_id), 'listed')
        assert pending['state'] == 'pending' and pending['kind'] == 'server' and not pending['online']
        assert pending['fingerprint'] in (tmp_path / 'a.log').read_text()
        text = _agents._run_desktop('desktop_system_info', {'type': 'overview', 'device': first_id}, user_id=OWNER)
        assert 'Error' in text or 'error' in text.lower(), text
        status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{first_id}/confirm', {})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[first_id]['online'], 'online after confirmation')

        # Read-only default although the node allows writes; the ceiling raises it.
        text = _agents._run_desktop('desktop_write_file', {'path': str(effect), 'content': 'x', 'device': first_id},
                                    user_id=OWNER)
        assert 'permission_denied' in text and not effect.exists(), text
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{first_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'write': True}})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[first_id]['tiers']['write'], 'write effective')
        text = _agents._run_desktop('desktop_write_file', {'path': str(effect), 'content': 'x', 'device': first_id},
                                    user_id=OWNER)
        assert effect.read_text() == 'x', text
        # The Home never raises what the device withholds: exec is off on the node.
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{first_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'write': True, 'exec': True}})
        assert status == 200, body
        text = _agents._run_desktop('desktop_run_command', {'command': 'true', 'device': first_id}, user_id=OWNER)
        assert 'permission' in text.lower(), text
        assert _nodes(live_server)[first_id]['tiers']['exec'] is False

        # A twin container on the same home directory: its own node and credential.
        second, second_id = _pair(live_server, shared, tmp_path / 'b.log', host_b)
        assert second_id != first_id
        listed = _wait(lambda: _nodes(live_server).get(second_id), 'twin listed')
        assert listed['predecessor'] and listed['predecessor']['node_id'] == first_id
        assert listed['predecessor']['online'] is True  # a live twin, not a successor
        status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{second_id}/confirm', {})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[second_id]['online'], 'twin online')
        cfg = json.loads(shared.read_text())
        secrets = [row['secret'] for rows in [cfg.get('attachments') or []]
                   + list((cfg.get('host_attachments') or {}).values()) for row in rows]
        assert len(secrets) == 2 and len(set(secrets)) == 2
        assert _nodes(live_server)[first_id]['online']  # both stay connected

        # Several online and no default: an unaddressed call is refused, not placed.
        text = _agents._run_desktop('desktop_system_info', {'type': 'overview'}, user_id=OWNER)
        assert 'device_ambiguous' in text or 'no default' in text, text
        status, body = _api(live_server, 'PUT', '/api/v4/devices/defaults', {'computer': second_id})
        assert status == 200, body
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{second_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'write': True}})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[second_id]['tiers']['write'], 'twin write effective')
        # Only the default may write now, so a write that lands proves the placement.
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{first_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True}})
        assert status == 200, body
        placed = tmp_path / 'placed.txt'
        text = _agents._run_desktop('desktop_write_file', {'path': str(placed), 'content': 'default'}, user_id=OWNER)
        assert placed.read_text() == 'default', text
        assert any(a['agent_id'] == second_id for a in db.list_agents(user_id=OWNER) if a['online'])
    finally:
        _stop(first)
        _stop(second)
        for node_id in [n for n in _nodes(live_server)]:
            _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{node_id}')
        _api(live_server, 'PUT', '/api/v4/devices/defaults', {'computer': None})
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()
