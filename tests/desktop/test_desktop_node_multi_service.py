"""One simulated desktop, two disposable loopback protocol fixtures.

Services and host facts are synthetic; pairing, frame construction, polling,
status interpretation and reconnect use production owners. This is not native
installation or full-server authentication. No command or browser relay runs.
"""
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit


def _wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.01)
    return False


@contextlib.contextmanager
def _service(identity):
    state = {'pairings': 0, 'polls': 0, 'nodes': {}, 'credential': 'fixture-' + identity,
             'available': True, 'drop': False, 'errors': [],
             'seen_credentials': [], 'last_seen': {}, 'hold': False,
             'poll_held': threading.Event(), 'release_poll': threading.Event()}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/api/desktop/poll' and state['hold']:
                state['hold'] = False
                state['poll_held'].set()
                if not state['release_poll'].wait(10):
                    state['errors'].append('held poll was not released')
                self.answer(401, {'error': 'bridge_auth_required'})
                return
            with lock:
                if self.path == '/api/desktop/pair':
                    assert body['code'] == 'fixture-pair-' + identity
                    assert body['kind'] == 'computer'
                    state['pairings'] += 1
                    state['credential'] = 'fixture-%s-%d' % (identity, state['pairings'])
                    self.answer(201, {'token': state['credential'], 'id': identity})
                elif self.path == '/api/desktop/poll':
                    credential = self.headers.get('X-Bridge-Secret')
                    state['seen_credentials'].append(credential)
                    if credential != state['credential']:
                        state['errors'].append('unexpected credential')
                        self.answer(401, {})
                        return
                    assert body['results'] == body['streams'] == []
                    node = body['agent']
                    assert not any(node['capabilities'].values())
                    assert node['share_roots'] == [] and node['route'] == 'direct'
                    if not state['available']:
                        if state['drop']:
                            self.close_connection = True
                        else:
                            self.answer(503, {})
                        return
                    state['nodes'][node['agent_id']] = node
                    state['last_seen'][node['agent_id']] = time.monotonic()
                    state['polls'] += 1
                    from lib.desktop_wire import protocol_version
                    self.answer(200, {'commands': [], 'protocol_version': protocol_version(),
                                      'server_time': time.time()})
                else:
                    self.answer(404, {})

        def do_GET(self):
            if self.path == '/api/health':
                with lock:
                    self.answer(200 if state['available'] else 503,
                                {'bootId': 'fixture-' + identity})
                return
            assert self.path == '/api/v4/desktop/nodes'
            assert self.headers['Authorization'] == 'Bearer owner-' + identity
            with lock:
                from lib.desktop import bridge
                now = time.monotonic()
                self.answer(200, {'nodes': [node for node_id, node in state['nodes'].items()
                    if now - state['last_seen'][node_id] < bridge._CONNECTED_WINDOW_S],
                    'service': identity})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_port
    try:
        yield base, state
    finally:
        state['release_poll'].set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('outage', ['http', 'disconnect'])
def test_one_simulated_installation_keeps_two_service_attachments(tmp_path, monkeypatch, outage):
    import requests
    from desktop import launcher
    from lib.desktop_agent import config, links
    from lib.desktop_agent import _run
    from lib.desktop_agent._permissions import safe_default

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'desktop.json'))
    monkeypatch.setattr(config, '_host', lambda: (
        'synthetic-host', {'machine': 'fixture', 'hostname': 'fixture', 'container': ''},
        'synthetic-host'))
    monkeypatch.setattr(links, 'ensure_relay', lambda *args: None)
    monkeypatch.setattr(launcher, '_log', lambda *args: None)
    node_id = config.node_identity()['node_id']
    monkeypatch.setattr(_run, '_system_facts', lambda: None)

    def dispatch_forbidden(*args, **kwargs):
        pytest.fail('command-free fixture must never dispatch a command')

    monkeypatch.setattr(_run, 'dispatch_command', dispatch_forbidden)

    def listing(url, identity, expected=True):
        response = requests.get(url + '/api/v4/desktop/nodes', timeout=2,
                                headers={'Authorization': 'Bearer owner-' + identity})
        assert response.status_code == 200
        body = response.json()
        assert body['service'] == identity
        assert [row['agent_id'] for row in body['nodes']] == ([node_id] if expected else [])
        return body['nodes'][0] if expected else None

    with _service('a') as (a, sa), _service('b') as (b, sb):
        state = {}
        runtime = launcher._node_links(state)
        try:
            ra = runtime.join(a + '/#tofu-pair=fixture-pair-a', node_id=node_id)
            rb = runtime.join(b + '/#tofu-pair=fixture-pair-b', node_id=node_id)
            saved = config.attachments()
            runtime.start()
            assert _wait_for(lambda: sa['polls'] and sb['polls'])
            boot = listing(a, 'a')['boot_id']
            assert listing(b, 'b')['boot_id'] == boot == _run._BOOT_ID
            assert _wait_for(lambda: all(row['last_status'].get('state') == 'ok'
                                         for row in runtime.statuses()))
            assert state['perms'] == safe_default()
            assert len(runtime.statuses()) == 2
            assert runtime.stop()
            assert config.attachments() == saved

            monkeypatch.setattr(_run, '_BOOT_ID', 'b' * 32)
            runtime = launcher._node_links({})
            runtime.start()
            assert _wait_for(lambda: sa['nodes'][node_id]['boot_id'] == _run._BOOT_ID
                             and sb['nodes'][node_id]['boot_id'] == _run._BOOT_ID)
            assert config.node_identity()['node_id'] == node_id
            assert config.attachments() == saved
            assert sa['pairings'] == sb['pairings'] == 1
            assert len(sa['nodes']) == len(sb['nodes']) == 1

            # A service outage changes production status, not B's worker or pairing.
            before_b = sb['polls']
            sa['drop'] = outage == 'disconnect'
            sa['available'] = False
            expected_status = 'unreachable' if sa['drop'] else 'http'
            assert _wait_for(lambda: next(row for row in runtime.statuses()
                if row['id'] == ra['id'])['last_status'].get('state') == expected_status)
            sa['last_seen'][node_id] = 0
            listing(a, 'a', expected=False)
            assert _wait_for(lambda: sb['polls'] > before_b)
            listing(b, 'b')
            assert config.attachments() == saved
            assert sa['pairings'] == sb['pairings'] == 1
            sa['available'] = True
            assert _wait_for(lambda: next(row for row in runtime.statuses()
                if row['id'] == ra['id'])['last_status'].get('state') == 'ok')
            assert listing(a, 'a')['boot_id'] == _run._BOOT_ID
            assert config.attachments() == saved

            # Explicit active rejoin renews only A's credential and worker.
            b_worker = runtime._links[rb['id']]['thread']
            a_worker = runtime._links[ra['id']]['thread']
            renewed = runtime.join(a + '/#tofu-pair=fixture-pair-a', node_id=node_id)
            assert renewed['id'] == ra['id'] and not a_worker.is_alive()
            assert _wait_for(lambda: sa['seen_credentials'][-1] == renewed['secret'])
            assert runtime._links[rb['id']]['thread'] is b_worker and b_worker.is_alive()
            assert sa['pairings'] == 2 and sb['pairings'] == 1
            assert next(row for row in config.attachments() if row['id'] == rb['id']) == rb
            assert set(sb['seen_credentials']) == {rb['secret']}
            assert set(sa['seen_credentials']) == {ra['secret'], renewed['secret']}
            assert listing(a, 'a')['agent_id'] == listing(b, 'b')['agent_id'] == node_id

            before_b = sb['polls']
            assert runtime.leave(ra['id'])
            after_a = sa['polls']
            assert _wait_for(lambda: sb['polls'] > before_b)
            assert sa['polls'] == after_a
            assert config.attachments() == [rb]
            assert [row['id'] for row in runtime.statuses()] == [rb['id']]
            sa['last_seen'][node_id] = 0
            listing(a, 'a', expected=False)
            assert listing(b, 'b')['boot_id'] == _run._BOOT_ID
            assert sa['errors'] == sb['errors'] == []
        finally:
            assert runtime.stop()
        assert sa['errors'] == sb['errors'] == []


def test_legacy_migration_keeps_another_attachment_and_permission_choices(tmp_path, monkeypatch):
    from lib.desktop_agent import config
    from lib.desktop_agent._permissions import safe_default
    from lib.desktop_wire.node_protocol import attachment_id
    path = tmp_path / 'desktop.json'
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(path))
    monkeypatch.setattr(config, '_host', lambda: (
        'synthetic-host', {'machine': 'fixture', 'hostname': 'fixture', 'container': ''},
        'synthetic-host'))
    b = {'id': attachment_id('http://127.0.0.1:19002'),
         'url': 'http://127.0.0.1:19002', 'secret': 'fixture-b',
         'candidates': [], 'label': 'B', 'state': 'revoked', 'paired_at': 123}
    choices = {'enabled': False, 'perms': safe_default()}
    roots = [{'name': 'synthetic', 'path': str(tmp_path / 'shared')}]
    path.write_text(json.dumps({'agent_id': 'fixture-node', 'attachments': [b],
        'remote_server': {'url': 'http://127.0.0.1:19001', 'secret': 'fixture-legacy'},
        'share_roots': roots, 'computer_control': choices}))
    rows = config.attachments()
    assert len(rows) == 2
    assert next(row for row in rows if row['id'] == b['id']) == b
    assert next(row for row in rows if row['id'] != b['id'])['secret'] == 'fixture-legacy'
    assert config.attachments() == rows
    persisted = config.load_config()
    assert persisted['remote_server'] == {}
    assert persisted['agent_id'] == 'fixture-node'
    assert persisted['computer_control'] == choices
    assert persisted['share_roots'] == roots


def test_delayed_old_credential_refusal_preserves_renewal(tmp_path, monkeypatch):
    from lib.desktop_agent import config, links, _run
    from lib.desktop_agent._permissions import safe_default
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'desktop.json'))
    monkeypatch.setattr(config, '_host', lambda: (
        'synthetic-host', {'machine': 'fixture', 'hostname': 'fixture', 'container': ''},
        'synthetic-host'))
    monkeypatch.setattr(_run, '_system_facts', lambda: None)
    monkeypatch.setattr(links, 'ensure_relay', lambda *args: None)
    node_id = config.node_identity()['node_id']
    runtime = links.NodeLinks(safe_default(), lambda msg: None)
    with _service('a') as (a, sa), _service('b') as (b, sb):
        try:
            ra = runtime.join(a + '/#tofu-pair=fixture-pair-a', node_id=node_id)
            rb = runtime.join(b + '/#tofu-pair=fixture-pair-b', node_id=node_id)
            runtime.start()
            assert _wait_for(lambda: sa['polls'] and sb['polls'])
            b_worker = runtime._links[rb['id']]['thread']
            sa['hold'] = True
            assert sa['poll_held'].wait(10)
            save = config.save_attachment

            def save_then_release(record, **kwargs):
                result = save(record, **kwargs)
                if record['id'] == ra['id']:
                    assert record['secret'] != ra['secret']
                    sa['release_poll'].set()
                return result

            monkeypatch.setattr(config, 'save_attachment', save_then_release)
            renewed = runtime.join(a + '/#tofu-pair=fixture-pair-a', node_id=node_id)
            assert _wait_for(lambda: renewed['secret'] in sa['seen_credentials'])
            assert next(r for r in config.attachments() if r['id'] == ra['id']) == renewed
            assert runtime._links[rb['id']]['thread'] is b_worker and b_worker.is_alive()
            assert next(r for r in config.attachments() if r['id'] == rb['id']) == rb
            assert runtime.stop()
            old_counts = sa['polls'], sb['polls']
            runtime.start()
            assert _wait_for(lambda: sa['polls'] > old_counts[0] and sb['polls'] > old_counts[1])
            assert len(runtime.statuses()) == 2
            assert sa['pairings'] == 2 and sb['pairings'] == 1
            assert next(r for r in config.attachments() if r['id'] == ra['id']) == renewed
            # A refusal of the current credential must still be durable.
            sa['poll_held'].clear()
            sa['hold'] = True
            assert _wait_for(lambda: next(r for r in config.attachments()
                if r['id'] == ra['id'])['state'] == 'revoked')
            assert next(r for r in config.attachments() if r['id'] == rb['id']) == rb
            assert sa['errors'] == sb['errors'] == []
        finally:
            sa['release_poll'].set()
            assert runtime.stop()


def test_route_health_fixture_answers_production_probe_without_credentials():
    from lib.desktop_agent._probe import probe_server
    with _service('health') as (url, service):
        assert probe_server(url) == (True, '')
        service['available'] = False
        assert probe_server(url) == (False, 'http_503')
        service['available'] = True
        assert probe_server(url) == (True, '')
        assert service['pairings'] == service['polls'] == 0
        assert service['seen_credentials'] == service['errors'] == []
