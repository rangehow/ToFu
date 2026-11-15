"""Node protocol v1: contract, pairing links, migration, multi-node routing,
two-sided permission tiers and revoke.

Authority: contracts/desktop_node_v1.schema.json, lib/desktop_wire/node_protocol.py,
docs/modules/remote_execution.md ("Nodes and pairing", "Permissions").
"""
from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import json
import threading
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]

ROOT = Path(__file__).resolve().parents[2]
OWNER = 41
OTHER = 42


def _schema():
    return json.loads((ROOT / 'contracts/desktop_node_v1.schema.json').read_text())


def _payload(response):
    body = dict(response.get_json())
    body.pop('ok', None)
    return body


def _validator(definition):
    from jsonschema import Draft202012Validator
    schema = _schema()
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/' + definition})


# ── Contract ──────────────────────────────────────────────────────────

def test_contract_is_a_valid_schema_with_read_only_default():
    from jsonschema import Draft202012Validator
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    tiers = schema['x-permission-tiers']
    assert tiers['default'] == {tier: tier == 'read' for tier in tiers['order']}
    assert set(tiers['permission_keys']) == set(tiers['order']) - {'read'}


def test_every_device_command_and_desktop_tool_has_a_tier():
    from lib.desktop_agent._dispatch import COMMANDS
    from lib.desktop_tools import DESKTOP_TOOL_NAMES
    from lib.desktop_wire.node_protocol import required_tier
    for name in set(COMMANDS) | set(DESKTOP_TOOL_NAMES) | {
            'egress_http_stream', 'egress_cancel', 'paper_build', 'paper_build_cancel'}:
        assert required_tier(name) is not None, name
    assert required_tier('desktop_system_info', {'type': 'kill'}) == 'exec'
    assert required_tier('desktop_system_info', {'type': 'overview'}) == 'read'
    assert required_tier('not_a_command') is None


def test_device_permission_keys_match_the_contract():
    from lib.desktop_agent._permissions import PERMISSION_KEYS
    assert set(PERMISSION_KEYS) == set(
        _schema()['x-permission-tiers']['permission_keys'].values())


# ── Pairing links ─────────────────────────────────────────────────────

@pytest.mark.parametrize('pasted', [
    'https://h.example/proxy/15000/#tofu-pair=Abc_12-xyz',
    '  https://h.example/proxy/15000#tofu-pair=Abc_12-xyz \n',
    'https://h.example/proxy/15000/#/chat&tofu-pair=Abc_12-xyz',
    'https://h.example/proxy/15000/?tab=1#tofu-pair=Abc_12-xyz',
])
def test_pairing_link_parsing_is_robust(pasted):
    from lib.desktop_wire.node_protocol import parse_pairing_link
    link = parse_pairing_link(pasted)
    assert link.server_url == 'https://h.example/proxy/15000'
    assert link.secret == 'Abc_12-xyz'
    assert link.candidates == ('https://h.example/proxy/15000',)


def test_pairing_link_round_trip_puts_via_routes_first():
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link
    text = build_pairing_link('https://h.example/proxy/15000/', 'sec_123',
                              via=['http://10.0.0.5:15000', 'https://h.example/proxy/15000'])
    link = parse_pairing_link(text)
    assert link.candidates == ('http://10.0.0.5:15000', 'https://h.example/proxy/15000')
    _validator('PairingLink').validate({
        'server_url': link.server_url, 'secret': link.secret,
        'candidates': list(link.candidates)})


@pytest.mark.parametrize('bad, code', [
    ('https://h.example/proxy/15000/#/chat', 'not_a_pairing_link'),
    ('https://u:p@h.example/#tofu-pair=abcdef', 'bad_url'),
    ('ftp://h.example/#tofu-pair=abcdef', 'bad_url'),
    ('https://h.example/#tofu-pair=a&tofu-pair=b', 'not_a_pairing_link'),
    ('https://h.example/#tofu-pair=has space', 'bad_secret'),
    ('https://h.example/#tofu-pair=short', 'bad_secret'),
])
def test_bad_pairing_links_are_coded_refusals(bad, code):
    from lib.desktop_wire.node_protocol import PairingInputError, parse_pairing_link
    with pytest.raises(PairingInputError) as exc:
        parse_pairing_link(bad)
    assert exc.value.code == code


# ── Migration of already-paired devices ──────────────────────────────

def test_legacy_remote_server_migrates_once_keeping_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    (tmp_path / 'agent.json').write_text(json.dumps({
        'agent_id': 'abc123',
        'share_roots': [{'name': 'p', 'path': '/tmp/p'}],
        'remote_server': {'url': 'https://tofu.example.com/', 'secret': 'tok_OLD'},
        'attach_candidates': ['http://10.0.0.2:15000'],
    }))
    from lib.desktop_agent.config import attachments, load_config
    rows = attachments()
    assert len(rows) == 1
    assert rows[0]['url'] == 'https://tofu.example.com'
    assert rows[0]['secret'] == 'tok_OLD'
    assert rows[0]['candidates'] == ['http://10.0.0.2:15000']
    assert rows[0]['state'] == 'active'
    _validator('Attachment').validate(rows[0])
    cfg = load_config()
    assert cfg['remote_server'] == {}
    assert cfg['agent_id'] == 'abc123'
    assert cfg['share_roots'] == [{'name': 'p', 'path': '/tmp/p'}]
    assert attachments() == rows  # idempotent


def test_legacy_connect_line_joins_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    import lib.desktop_agent.attachments as attachments_mod
    monkeypatch.setattr(attachments_mod, '_redeem',
                        lambda *a, **k: pytest.fail('a connect line needs no pairing'))
    record = attachments_mod.join('https://corp.example.com/tofu/\ttok_P', node_id='n1')
    assert (record['url'], record['secret']) == ('https://corp.example.com/tofu', 'tok_P')
    from lib.desktop_agent.config import attachments
    assert attachments() == [record]


def test_join_tries_via_routes_first_and_stops_on_a_refused_code(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    import lib.desktop_agent.attachments as attachments_mod
    tried = []

    def redeem(url, secret, node_id, name, timeout):
        tried.append(url)
        return (None, 'unreachable') if url.startswith('http://10.') else ('tok', '')

    monkeypatch.setattr(attachments_mod, '_redeem', redeem)
    record = attachments_mod.join(
        'https://h.example/p/#tofu-pair=abcdefgh&via=http%3A%2F%2F10.0.0.5%3A15000',
        node_id='n1', name='box')
    assert tried == ['http://10.0.0.5:15000', 'https://h.example/p']
    assert record['url'] == 'https://h.example/p'
    assert record['candidates'] == ['http://10.0.0.5:15000']

    tried.clear()
    monkeypatch.setattr(attachments_mod, '_redeem',
                        lambda url, *a, **k: tried.append(url) or (None, 'invalid_code'))
    with pytest.raises(attachments_mod.JoinError) as exc:
        attachments_mod.join(
            'https://h.example/p/#tofu-pair=abcdefgh&via=http%3A%2F%2F10.0.0.5%3A15000',
            node_id='n1')
    assert exc.value.code == 'invalid_code'
    assert tried == ['http://10.0.0.5:15000']


def test_sso_fronted_server_is_joined_through_the_signed_in_page(tmp_path, monkeypatch):
    """No direct route answers (an SSO edge): the node opens its loopback
    relay for the link origin and the page carries the one-shot redeem."""
    import requests
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    import lib.desktop_agent.attachments as attachments_mod
    from lib.desktop_agent import _browser_relay
    monkeypatch.setattr(attachments_mod, '_redeem', lambda *a, **k: (None, 'http_401'))
    started = []
    real = _browser_relay.BrowserRelay

    class Relay(real):
        def __init__(self, urls, **kwargs):
            super().__init__(urls, port_start=15580, port_end=15589, **kwargs)
            started.append(self)

    monkeypatch.setattr(_browser_relay, 'BrowserRelay', Relay)
    out = {}
    worker = threading.Thread(target=lambda: out.update(record=attachments_mod.join(
        'https://ide.example/proxy/15000/#tofu-pair=abcdefgh', node_id='n9',
        name='office')))
    worker.start()
    _wait(lambda: started and started[0].port)
    base = 'http://127.0.0.1:%d' % started[0].port
    origin = {'Origin': 'https://ide.example'}
    assert requests.get(base + '/v1/status', headers={'Origin': 'https://evil.example'},
                        timeout=2).status_code == 403
    assert requests.get(base + '/v1/status', headers=origin, timeout=2).status_code == 200
    job = None
    while job is None:
        taken = requests.get(base + '/v1/take', headers=origin, timeout=15)
        job = taken.json() if taken.status_code == 200 else None
    assert job['url'] == 'https://ide.example/proxy/15000/api/desktop/pair'
    assert job['payload']['code'] == 'abcdefgh' and job['payload']['node_id'] == 'n9'
    assert requests.post(base + '/v1/result', headers=origin, timeout=2, json={
        'id': job['id'], 'status': 201, 'body': json.dumps({'token': 'tok-via-page'})},
    ).status_code == 200
    worker.join(5)
    assert out['record']['url'] == 'https://ide.example/proxy/15000'
    assert out['record']['secret'] == 'tok-via-page'
    assert started[0]._closed


# ── Server: pairing → nodes → routing → tiers → revoke ───────────────

def _bearer(owner):
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=owner, name='test-node-%d' % owner,
                             scopes=['chat'])
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def clean_bridge():
    from lib.api_keys import list_keys, revoke_key
    from lib.desktop import bridge
    from lib.desktop.pairing import _STORE, _STORE_LOCK

    def clear():
        with bridge.command_queue_lock:
            bridge.command_queue.clear()
            bridge._agents.clear()
        with _STORE_LOCK:
            _STORE.clear()
        for owner in (OWNER, OTHER):
            for key in list_keys(owner_user_id=owner):
                revoke_key(key['id'], owner_user_id=owner)

    clear()
    yield
    clear()


def _pair(cli, auth, node_id, name):
    r = cli.post('/api/v4/desktop/pair-code?base=http://localhost/proxy/15000/',
                 headers=dict(auth, Host='localhost'))
    assert r.status_code == 201, r.get_data(as_text=True)
    body = _payload(r)
    _validator('PairCodeResponse').validate(body)
    from lib.desktop_wire.node_protocol import parse_pairing_link
    link = parse_pairing_link(body['link'])
    assert link.server_url == 'http://localhost/proxy/15000'
    r = cli.post('/api/desktop/pair', json={'code': link.secret, 'node_id': node_id,
                                            'name': name, 'platform': 'linux'})
    assert r.status_code == 201, r.get_data(as_text=True)
    paired = _payload(r)
    _validator('PairResponse').validate(paired)
    assert paired['node_id'] == node_id
    # The 6-digit form of the same pairing is consumed with it.
    assert cli.post('/api/desktop/pair', json={'code': body['code']}).status_code == 409
    return paired['token']


def _poll(cli, token, node_id, name, **caps):
    return cli.post('/api/desktop/poll', headers={'X-Bridge-Secret': token}, json={
        'results': [], 'streams': [],
        'agent': {'agent_id': node_id, 'name': name, 'platform': 'linux',
                  'capabilities': caps, 'share_roots': []}})


def test_two_paired_nodes_are_listed_routed_tier_checked_and_revocable(
        flask_client, clean_bridge):
    from lib.desktop import bridge
    auth = _bearer(OWNER)
    status = flask_client.get('/api/v4/desktop/status', headers=auth).get_json()
    assert status['setup_state'] == 'pair'
    assert status['connected'] is False

    tok_a = _pair(flask_client, auth, 'node-a', 'laptop')
    tok_b = _pair(flask_client, auth, 'node-b', 'Workstation')
    # Polls with a results body return without a long-poll wait.
    for token, node, name in ((tok_a, 'node-a', 'laptop'),
                              (tok_b, 'node-b', 'Workstation')):
        bridge.register_agent(node, {'name': name, 'capabilities': {}},
                              user_id=str(OWNER),
                              key_id=_key_id(flask_client, auth, name))
    listing = _payload(flask_client.get('/api/v4/desktop/nodes', headers=auth))
    _validator('NodeListing').validate(listing)
    nodes = {n['node_id']: n for n in listing['nodes']}
    assert set(nodes) == {'node-a', 'node-b'}
    assert all(n['online'] and n['attachment'] == 'paired' for n in nodes.values())
    assert nodes['node-a']['tiers'] == {'read': True, 'write': False, 'exec': False,
                                        'gui': False, 'egress': False,
                                        'codex_login': False}
    status = flask_client.get('/api/v4/desktop/status', headers=auth).get_json()
    assert (status['setup_state'], status['connected']) == ('connected', True)
    # Another owner sees none of them.
    other = flask_client.get('/api/v4/desktop/nodes', headers=_bearer(OTHER)).get_json()
    assert other['nodes'] == []

    # Multi-node routing: unaddressed is refused, addressing by name reaches one.
    _cmd, err = bridge.send_desktop_command('desktop_list_files', {}, timeout=0,
                                            user_id=str(OWNER))
    assert err and 'laptop' in err and 'Workstation' in err
    target, err = bridge.resolve_device_target('workstation', str(OWNER))
    assert err is None and target['agent_id'] == 'node-b'
    delivered = _deliver_and_settle(bridge, 'desktop_list_files', 'node-b')
    assert delivered == ['node-b']

    # Server-side tier admission: read-only node refuses exec before enqueue.
    _cmd, err = bridge.send_desktop_command('desktop_run_command', {'command': 'id'},
                                            timeout=0, target_agent_id='node-b',
                                            user_id=str(OWNER))
    assert err and 'exec permission' in err
    assert not bridge.command_queue
    bridge.register_agent('node-b', {'name': 'Workstation',
                                     'capabilities': {'exec': True}},
                          user_id=str(OWNER), key_id=_key_id(flask_client, auth, 'Workstation'))
    assert _deliver_and_settle(bridge, 'desktop_run_command', 'node-b') == ['node-b']

    # Revoke: the node disappears at once, its pending command fails, its
    # credential no longer polls.
    pending = {}

    def wait_for_result():
        pending['result'] = bridge.send_desktop_command(
            'desktop_list_files', {}, target_agent_id='node-a', user_id=str(OWNER))

    waiter = threading.Thread(target=wait_for_result)
    waiter.start()
    _wait(lambda: bridge.command_queue)
    r = flask_client.delete('/api/v4/desktop/nodes/node-a', headers=auth)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['disconnected'] == ['node-a']
    waiter.join(5)
    assert pending['result'][1] and 'revoked' in pending['result'][1]
    assert _poll(flask_client, tok_a, 'node-a', 'laptop').status_code == 401
    listing = flask_client.get('/api/v4/desktop/nodes', headers=auth).get_json()
    assert [n['node_id'] for n in listing['nodes']] == ['node-b']
    # Another owner cannot remove this owner's node.
    assert flask_client.delete('/api/v4/desktop/nodes/node-b',
                               headers=_bearer(OTHER)).status_code == 404


def test_token_revoke_disconnects_its_node(flask_client, clean_bridge):
    from lib.desktop import bridge
    auth = _bearer(OWNER)
    _pair(flask_client, auth, 'node-c', 'nas')
    key_id = _key_id(flask_client, auth, 'nas')
    bridge.register_agent('node-c', {'name': 'nas'}, user_id=str(OWNER), key_id=key_id)
    r = flask_client.delete('/api/v4/desktop/token/' + key_id, headers=auth)
    assert r.status_code == 200
    assert r.get_json()['disconnected'] == ['node-c']
    assert not any(a['agent_id'] == 'node-c' for a in bridge.list_agents(str(OWNER)))


def test_offline_paired_credential_is_listed_as_an_offline_node(flask_client, clean_bridge):
    auth = _bearer(OWNER)
    _pair(flask_client, auth, 'node-d', 'old-pc')
    nodes = flask_client.get('/api/v4/desktop/nodes', headers=auth).get_json()['nodes']
    assert [(n['node_id'], n['name'], n['online']) for n in nodes] == [(None, 'old-pc', False)]


def test_pair_link_base_is_pinned_to_the_request_host(flask_client, clean_bridge):
    auth = _bearer(OWNER)
    r = flask_client.post('/api/v4/desktop/pair-code?base=https://evil.example/',
                          headers=dict(auth, Host='localhost'))
    assert r.status_code == 201
    from lib.desktop_wire.node_protocol import parse_pairing_link
    assert 'evil.example' not in parse_pairing_link(r.get_json()['link']).server_url


def test_device_refuses_tiers_it_was_not_granted():
    from lib.desktop_agent._dispatch import dispatch_command
    from lib.desktop_agent._permissions import safe_default
    for cmd, params in (('desktop_write_file', {'path': 'x', 'content': ''}),
                        ('desktop_run_command', {'command': 'id'}),
                        ('desktop_screenshot', {}),
                        ('desktop_clipboard', {}),
                        ('egress_http', {}),
                        ('desktop_system_info', {'type': 'kill', 'pid': 1})):
        assert 'requires --allow-' in dispatch_command(cmd, params, safe_default())['error'], cmd


# ── helpers ──────────────────────────────────────────────────────────

def _key_id(cli, auth, name):
    tokens = cli.get('/api/v4/desktop/devices', headers=auth).get_json()['tokens']
    return next(t['id'] for t in tokens if t['name'] == name)


def _wait(predicate):
    import time
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    assert predicate()


def _deliver_and_settle(bridge, cmd_type, target):
    """Enqueue to ``target`` by id; return which nodes received it."""
    out = {}

    def call():
        out['result'] = bridge.send_desktop_command(
            cmd_type, {}, timeout=5, target_agent_id=target, user_id=str(OWNER))

    thread = threading.Thread(target=call)
    thread.start()
    _wait(lambda: bridge.command_queue)
    received = []
    for node in ('node-a', 'node-b'):
        cmds = bridge.take_pending_commands(agent_id=node, user_id=str(OWNER))
        for cmd in cmds:
            received.append(node)
            bridge.resolve_results([{'id': cmd['id'], 'result': {'ok': True}}],
                                   agent_id=node, user_id=str(OWNER))
    thread.join(5)
    assert out['result'] == ({'ok': True}, None)
    return received
