"""Home node registry: durable pairing, confirm-on-join, credential binding, twins.

contracts/desktop_node_registry_v1.schema.json; docs/modules/remote_execution.md,
"Node registry, confirmation and trust". Everything here runs against a real
storage authority (credential sidecar) through the public HTTP routes.
"""
from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import importlib
import threading

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]

OWNER = 31
OTHER = 32


def _bearer(owner=OWNER):
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=owner, name='test-registry-%d' % owner, scopes=['chat'])
    return {'Authorization': 'Bearer ' + token}


def _data(response):
    body = response.get_json()
    return body.get('data', body) if isinstance(body, dict) else body


@pytest.fixture
def home(monkeypatch):
    from lib.api_keys import list_keys, revoke_key
    from lib.desktop import bridge, node_registry
    monkeypatch.setattr(bridge, 'POLL_WAIT_TIMEOUT', 0)

    def clear():
        with bridge.command_queue_lock:
            bridge.command_queue.clear()
            bridge._agents.clear()
        with node_registry._LOCK:
            node_registry._PRESENCE.clear()
        for owner in (OWNER, OTHER):
            registry = node_registry.NodeRegistry(owner)
            for record in registry.list():
                registry.forget(record['nodeId'])
            for key in list_keys(owner_user_id=owner):
                revoke_key(key['id'], owner_user_id=owner)

    clear()
    yield
    clear()


def _mint(cli, auth):
    r = cli.post('/api/v4/desktop/pair-code', headers=auth)
    assert r.status_code == 201, r.get_data(as_text=True)
    return _data(r)


def _pair(cli, auth, node_id, *, name='box', previous=None, confirm=False):
    minted = _mint(cli, auth)
    body = {'code': minted['code'], 'node_id': node_id, 'name': name, 'platform': 'linux',
            'kind': 'server'}
    if previous:
        body['previous_node_id'] = previous
    r = cli.post('/api/desktop/pair', json=body)
    assert r.status_code == 201, r.get_data(as_text=True)
    paired = _data(r)
    if confirm:
        assert cli.post(f'/api/v4/desktop/nodes/{node_id}/confirm', headers=auth,
                        json={}).status_code == 200
    return paired


def _poll(cli, token, node_id, *, boot='a' * 32, results=None):
    return cli.post('/api/desktop/poll', headers={'X-Bridge-Secret': token}, json={
        'results': results or [], 'streams': [],
        'agent': {'agent_id': node_id, 'name': node_id, 'platform': 'linux',
                  'capabilities': {}, 'share_roots': [], 'boot_id': boot}})


def _nodes(cli, auth):
    return {n['node_id']: n for n in _data(cli.get('/api/v4/desktop/nodes', headers=auth))['nodes']}


def test_pairing_is_durable_one_shot_and_survives_process_state_loss(flask_client, home):
    from lib.desktop import pairing
    auth = _bearer()
    minted = _mint(flask_client, auth)
    # A Home restart loses every in-memory structure; the pairing lives in storage.
    importlib.reload(pairing)
    assert pairing.consume_code(minted['code'], node_id='n1').owner_user_id == OWNER
    assert pairing.consume_code(minted['code'], node_id='n1') is None
    from lib.desktop_wire.node_protocol import parse_pairing_link
    # The link secret named the same pairing: consumed with the code.
    assert pairing.consume_code(parse_pairing_link(minted['link']).secret) is None


def test_concurrent_redemptions_of_one_code_yield_one_credential(flask_client, home):
    from lib.desktop import pairing
    minted = _mint(flask_client, _bearer())
    winners, barrier = [], threading.Barrier(6)

    def redeem(index):
        barrier.wait()
        if pairing.consume_code(minted['code'], node_id=f'n{index}') is not None:
            winners.append(index)

    threads = [threading.Thread(target=redeem, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(winners) == 1


def test_new_node_waits_for_confirmation_and_never_reaches_the_bridge(flask_client, home):
    from lib.desktop import bridge
    from lib.desktop_wire.node_protocol import fingerprint
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-p')
    assert paired['fingerprint'] == fingerprint('node-p', paired['id'])
    r = _poll(flask_client, paired['token'], 'node-p')
    assert r.status_code == 200
    assert r.get_json()['node'] == {'state': 'pending_confirmation',
                                    'fingerprint': paired['fingerprint']}
    assert r.get_json()['commands'] == []
    assert bridge.list_agents(str(OWNER)) == []
    node = _nodes(flask_client, auth)['node-p']
    assert (node['state'], node['kind'], node['fingerprint']) == (
        'pending', 'server', paired['fingerprint'])
    r = flask_client.post('/api/v4/desktop/nodes/node-p/confirm', headers=auth,
                          json={'name': 'gpu-1'})
    assert r.status_code == 200 and _data(r)['state'] == 'active'
    assert 'node' not in _poll(flask_client, paired['token'], 'node-p').get_json()
    assert [a['agent_id'] for a in bridge.list_agents(str(OWNER))] == ['node-p']
    assert _nodes(flask_client, auth)['node-p']['name'] == 'gpu-1'
    # Another owner can neither see nor confirm it.
    assert flask_client.post('/api/v4/desktop/nodes/node-p/confirm', headers=_bearer(OTHER),
                             json={}).status_code == 404
    assert _nodes(flask_client, _bearer(OTHER)) == {}


def test_rejected_node_is_revoked_and_forgotten(flask_client, home):
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-r')
    assert flask_client.post('/api/v4/desktop/nodes/node-r/reject', headers=auth).status_code == 200
    assert _poll(flask_client, paired['token'], 'node-r').status_code == 401
    assert 'node-r' not in _nodes(flask_client, auth)


def test_pending_poll_is_released_by_the_confirmation(flask_client, home, monkeypatch):
    import asyncio
    from lib.desktop import bridge, node_registry
    monkeypatch.setattr(bridge, 'POLL_WAIT_TIMEOUT', 30)
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-w')
    released = {}

    async def hold():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await node_registry.wait_for_confirmation(OWNER, 'node-w', 30)
        released['after'] = loop.time() - started

    thread = threading.Thread(target=lambda: asyncio.run(hold()))
    thread.start()
    deadline = threading.Event()
    while not node_registry._WAITERS and not deadline.wait(0.01):
        pass
    node_registry.NodeRegistry(OWNER).confirm('node-w')
    thread.join(10)
    assert released['after'] < 10
    assert paired['token']


def test_a_credential_presented_by_another_node_id_is_a_conflict(flask_client, home):
    from lib.desktop import bridge
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-a', confirm=True)
    assert _poll(flask_client, paired['token'], 'node-a').status_code == 200
    # A copied config on another host presents the same credential under a new id.
    r = _poll(flask_client, paired['token'], 'node-copy')
    assert r.status_code == 409
    assert r.get_json() == {'error': 'desktop_node_identity_conflict',
                            'reason': 'credential_reused', 'hint': r.get_json()['hint']}
    assert [a['agent_id'] for a in bridge.list_agents(str(OWNER))] == ['node-a']
    node = _nodes(flask_client, auth)['node-a']
    assert node['state'] == 'conflict' and node['conflict']['agentId'] == 'node-copy'
    # The original keeps working; confirming acknowledges and clears the conflict.
    assert _poll(flask_client, paired['token'], 'node-a').status_code == 200
    flask_client.post('/api/v4/desktop/nodes/node-a/confirm', headers=auth, json={})
    assert _nodes(flask_client, auth)['node-a']['state'] == 'active'


def test_two_live_processes_of_one_node_refuse_the_earlier_boot(flask_client, home):
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-t', confirm=True)
    assert _poll(flask_client, paired['token'], 'node-t', boot='1' * 32).status_code == 200
    # A restart (new boot, old one silent) is never a conflict.
    assert _poll(flask_client, paired['token'], 'node-t', boot='2' * 32).status_code == 200
    assert _poll(flask_client, paired['token'], 'node-t', boot='2' * 32).status_code == 200
    # The superseded boot polling again is a twin presenting one identity.
    r = _poll(flask_client, paired['token'], 'node-t', boot='1' * 32)
    assert r.status_code == 409 and r.get_json()['reason'] == 'duplicate_boot'
    assert _poll(flask_client, paired['token'], 'node-t', boot='2' * 32).status_code == 200
    assert _nodes(flask_client, auth)['node-t']['conflict']['reason'] == 'duplicate_boot'


def test_re_pairing_an_active_node_is_a_pending_replacement(flask_client, home):
    auth = _bearer()
    old = _pair(flask_client, auth, 'node-x', confirm=True)
    new = _pair(flask_client, auth, 'node-x')
    # The active credential keeps working until the owner decides.
    assert 'node' not in _poll(flask_client, old['token'], 'node-x').get_json()
    assert _poll(flask_client, new['token'], 'node-x').get_json()['node']['fingerprint'] \
        == new['fingerprint']
    node = _nodes(flask_client, auth)['node-x']
    assert node['state'] == 'active' and node['replacement_fingerprint'] == new['fingerprint']
    r = flask_client.post('/api/v4/desktop/nodes/node-x/confirm', headers=auth, json={})
    assert _data(r)['revoked_credentials'] == [old['id']]
    assert _poll(flask_client, old['token'], 'node-x').status_code == 401
    assert 'node' not in _poll(flask_client, new['token'], 'node-x').get_json()
    assert _nodes(flask_client, auth)['node-x']['fingerprint'] == new['fingerprint']


def test_rejecting_a_replacement_keeps_the_active_node(flask_client, home):
    auth = _bearer()
    old = _pair(flask_client, auth, 'node-y', confirm=True)
    new = _pair(flask_client, auth, 'node-y')
    flask_client.post('/api/v4/desktop/nodes/node-y/reject', headers=auth)
    assert _poll(flask_client, new['token'], 'node-y').status_code == 401
    assert _poll(flask_client, old['token'], 'node-y').status_code == 200
    assert _nodes(flask_client, auth)['node-y']['replacement_fingerprint'] is None


def test_a_recreated_host_is_shown_and_confirmed_as_a_re_pair(flask_client, home):
    auth = _bearer()
    old = _pair(flask_client, auth, 'node-old', confirm=True, name='container')
    new = _pair(flask_client, auth, 'node-new', previous='node-old', name='container')
    listed = _nodes(flask_client, auth)
    assert listed['node-new']['predecessor'] == {'node_id': 'node-old', 'name': 'container',
                                                 'online': False}
    flask_client.post('/api/v4/desktop/nodes/node-new/confirm', headers=auth, json={})
    listed = _nodes(flask_client, auth)
    assert set(listed) == {'node-new'} and listed['node-new']['predecessor'] is None
    assert _poll(flask_client, old['token'], 'node-old').status_code == 401
    assert _poll(flask_client, new['token'], 'node-new').status_code == 200


def test_a_live_predecessor_is_a_twin_and_stays(flask_client, home):
    auth = _bearer()
    old = _pair(flask_client, auth, 'node-1', confirm=True)
    assert _poll(flask_client, old['token'], 'node-1').status_code == 200
    _pair(flask_client, auth, 'node-2', previous='node-1')
    assert _nodes(flask_client, auth)['node-2']['predecessor']['online'] is True
    flask_client.post('/api/v4/desktop/nodes/node-2/confirm', headers=auth, json={})
    assert set(_nodes(flask_client, auth)) == {'node-1', 'node-2'}
    assert _poll(flask_client, old['token'], 'node-1').status_code == 200


def test_legacy_credentials_are_adopted_but_orphaned_pairing_credentials_refused(
        flask_client, home):
    from lib.api_keys import create_key
    from lib.desktop.node_registry import CREDENTIAL_ORIGIN
    auth = _bearer()
    _row, legacy = create_key('old-laptop', scopes=['agents:bridge'], owner_user_id=OWNER)
    assert _poll(flask_client, legacy, 'legacy-node').status_code == 200
    node = _nodes(flask_client, auth)['legacy-node']
    assert node['state'] == 'active' and node['online']
    _row, orphan = create_key('lost', scopes=['agents:bridge'], owner_user_id=OWNER,
                              metadata={'origin': CREDENTIAL_ORIGIN, 'node_id': 'ghost'})
    r = _poll(flask_client, orphan, 'ghost')
    assert r.status_code == 409 and r.get_json()['error'] == 'desktop_node_unregistered'


def test_removal_and_token_revocation_forget_the_node_offline_or_not(flask_client, home):
    auth = _bearer()
    a = _pair(flask_client, auth, 'node-del', confirm=True)
    # Offline (never polled) nodes are removable: the registry, not liveness, decides.
    r = flask_client.delete('/api/v4/desktop/nodes/node-del', headers=auth)
    assert r.status_code == 200 and r.get_json()['credential'] == a['id']
    assert _poll(flask_client, a['token'], 'node-del').status_code == 401
    b = _pair(flask_client, auth, 'node-tok', confirm=True)
    assert flask_client.delete('/api/v4/desktop/token/' + b['id'], headers=auth).status_code == 200
    assert 'node-tok' not in _nodes(flask_client, auth)


def test_pairing_requires_a_node_id_and_a_known_kind(flask_client, home):
    auth = _bearer()
    code = _mint(flask_client, auth)['code']
    assert flask_client.post('/api/desktop/pair', json={'code': code}).status_code == 400
    assert flask_client.post('/api/desktop/pair', json={
        'code': code, 'node_id': 'n', 'kind': 'phone'}).status_code == 400
    # A refused request did not consume the pairing.
    assert flask_client.post('/api/desktop/pair', json={
        'code': code, 'node_id': 'n'}).status_code == 201


def test_code_join_form_parses_to_a_single_route_pairing():
    from lib.desktop_wire.node_protocol import LegacyConnectLine, PairingLink, parse_join_input
    parsed = parse_join_input('https://home.example/proxy/15000/ 123456')
    assert parsed == PairingLink('https://home.example/proxy/15000', '123456',
                                 ('https://home.example/proxy/15000',))
    assert isinstance(parse_join_input('https://home.example tofu_live_x'), LegacyConnectLine)


def test_a_restarted_node_settles_its_predecessors_claimed_commands(flask_client, home):
    from lib.desktop import bridge
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-boot', confirm=True)
    assert _poll(flask_client, paired['token'], 'node-boot', boot='a' * 32).status_code == 200
    out = {}
    worker = threading.Thread(target=lambda: out.update(result=bridge.send_desktop_command(
        'desktop_list_files', {}, target_agent_id='node-boot', user_id=str(OWNER))))
    worker.start()
    deadline = threading.Event()
    while not bridge.command_queue and not deadline.wait(0.01):
        pass
    claimed = bridge.take_pending_commands(agent_id='node-boot', user_id=str(OWNER))
    assert len(claimed) == 1
    # The same boot polling again keeps the claim (it may still report it).
    assert _poll(flask_client, paired['token'], 'node-boot', boot='a' * 32).status_code == 200
    assert worker.is_alive()
    # A new process of the node cannot report it: settled as unknown, never replayed.
    assert _poll(flask_client, paired['token'], 'node-boot', boot='b' * 32).status_code == 200
    worker.join(10)
    _cmd, error = out['result']
    assert error and 'restarted while running this command' in error
    assert bridge.take_pending_commands(agent_id='node-boot', user_id=str(OWNER)) == []


def test_a_held_pending_poll_whose_node_was_rejected_is_answered_as_revoked(flask_client, home):
    from lib.desktop.node_registry import NodeRegistry
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-held')
    registry = NodeRegistry(OWNER)
    assert registry.admit_poll(credential_id=paired['id'], agent_id='node-held', frame={}).state == 'pending'
    flask_client.post('/api/v4/desktop/nodes/node-held/reject', headers=auth)
    # The poll authenticated before the rejection; on wake it is refused like a revoked credential.
    assert registry.admit_poll(credential_id=paired['id'], agent_id='node-held', frame={}).state == 'revoked'


# ── Routing/permissions hooks (lib/desktop/device_policy.py, lib/desktop/routing.py) ──

def _ceiling(cli, auth, node_id, **tiers):
    return cli.put(f'/api/v4/devices/{node_id}/ceiling', headers=auth,
                   json={'tiers': {'read': True, **tiers}})


def _defaults(cli, auth, **kinds):
    return cli.put('/api/v4/devices/defaults', headers=auth, json=kinds)


def test_an_offline_confirmed_node_is_addressable_by_the_policy_routes(flask_client, home):
    auth = _bearer()
    _pair(flask_client, auth, 'node-off', confirm=True)  # never polled
    assert _ceiling(flask_client, auth, 'node-off', exec=True).status_code == 200
    assert _defaults(flask_client, auth, server='node-off').status_code == 200
    # A pending node is not a device yet.
    _pair(flask_client, auth, 'node-wait')
    assert _ceiling(flask_client, auth, 'node-wait', exec=True).status_code == 404


def test_removal_drops_the_ceiling_and_default_mark(flask_client, home):
    from lib.desktop import device_policy
    auth = _bearer()
    _pair(flask_client, auth, 'node-gone', confirm=True)
    _ceiling(flask_client, auth, 'node-gone', exec=True)
    _defaults(flask_client, auth, server='node-gone')
    assert flask_client.delete('/api/v4/desktop/nodes/node-gone', headers=auth).status_code == 200
    record, _rev = device_policy.policy(OWNER)
    assert 'node-gone' not in record['ceilings'] and record['defaults'].get('server') is None


def test_a_confirmed_re_pair_keeps_ceiling_and_default(flask_client, home):
    from lib.desktop import device_policy
    auth = _bearer()
    _pair(flask_client, auth, 'node-v1', confirm=True)
    _ceiling(flask_client, auth, 'node-v1', exec=True, write=True)
    _defaults(flask_client, auth, server='node-v1')
    # Same node ID re-paired: keyed by node ID, nothing moves.
    _pair(flask_client, auth, 'node-v1')
    flask_client.post('/api/v4/desktop/nodes/node-v1/confirm', headers=auth, json={})
    record, _rev = device_policy.policy(OWNER)
    assert record['ceilings']['node-v1']['exec'] and record['defaults']['server'] == 'node-v1'
    # A recreated host (new node ID naming its predecessor) inherits both.
    _pair(flask_client, auth, 'node-v2', previous='node-v1')
    flask_client.post('/api/v4/desktop/nodes/node-v2/confirm', headers=auth, json={})
    record, _rev = device_policy.policy(OWNER)
    assert 'node-v1' not in record['ceilings']
    assert record['ceilings']['node-v2']['exec'] and record['ceilings']['node-v2']['write']
    assert record['defaults']['server'] == 'node-v2'


@pytest.mark.home_ceiling
def test_placement_uses_the_registry_kind_and_listing_the_intersection(flask_client, home):
    from lib.desktop import bridge, routing
    auth = _bearer()
    paired = _pair(flask_client, auth, 'node-k', confirm=True)  # paired as kind 'server'
    # The node claims another kind in its frame; the owner-confirmed registry wins.
    flask_client.post('/api/desktop/poll', headers={'X-Bridge-Secret': paired['token']}, json={
        'results': [], 'streams': [],
        'agent': {'agent_id': 'node-k', 'name': 'node-k', 'platform': 'linux', 'kind': 'computer',
                  'capabilities': {'exec': True, 'write': True}, 'share_roots': [],
                  'boot_id': 'c' * 32}})
    agent = next(a for a in bridge.list_agents(str(OWNER)) if a['agent_id'] == 'node-k')
    assert routing.device_kind(agent) == 'server'
    tiers = _nodes(flask_client, auth)['node-k']['tiers']
    assert (tiers['exec'], tiers['write']) == (False, False)  # Home ceiling: read-only
    _ceiling(flask_client, auth, 'node-k', exec=True)
    tiers = _nodes(flask_client, auth)['node-k']['tiers']
    assert (tiers['exec'], tiers['write']) == (True, False)  # ceiling AND device switch
