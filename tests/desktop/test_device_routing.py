"""Device routing and Home permissions (contracts/device_routing_v1.schema.json).

Owner decisions 6 and 7 (docs/MULTI_DEVICE_NETWORK_DESIGN.md): effective
permissions are the Home ceiling AND the device's own switch, a new device is
read-only, the Home enforces at one boundary and the node re-checks; a call
that names no device runs on the default of its kind, else on the only online
device, else is refused. A lost device settles its calls as device_unavailable,
never retried elsewhere, and a repeated idempotency key never runs twice.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from lib.desktop import bridge as db
from lib.desktop import device_policy, routing

pytestmark = [pytest.mark.unit, pytest.mark.home_ceiling]
OWNER, OTHER = 7, 8
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()


def _register(agent_id, name='', owner=OWNER, caps=None, kind=None):
    frame = {'name': name or agent_id, 'platform': 'linux',
             'capabilities': caps if caps is not None else {}}
    if kind:
        frame['kind'] = kind
    return db.register_agent(agent_id, frame, user_id=str(owner))


def _offline(agent_id):
    with db.command_queue_lock:
        db._agents[agent_id]['last_seen'] -= db._CONNECTED_WINDOW_S + 1


# ── contract ────────────────────────────────────────────────────────────

def test_tiers_definition_is_the_node_protocol_tiers():
    node = json.loads((ROOT / 'contracts/desktop_node_v1.schema.json').read_text())
    routing_contract = json.loads((ROOT / 'contracts/device_routing_v1.schema.json').read_text())
    mine = {k: v for k, v in routing_contract['$defs']['Tiers'].items() if k != 'description'}
    assert mine == node['$defs']['Tiers']
    assert db._CONNECTED_WINDOW_S == routing_contract['x-liveness-window-seconds']


# ── policy: ceiling AND device, new devices read-only ───────────────────

def test_new_device_is_read_only_and_effective_is_the_intersection():
    caps = {'write': True, 'exec': True}
    assert device_policy.effective_tiers(OWNER, 'n1', caps) == {
        'read': True, 'write': False, 'exec': False, 'gui': False, 'egress': False,
        'codex_login': False}
    _record, rev = device_policy.set_ceiling(OWNER, 'n1', {'write': True, 'gui': True})
    assert rev == 1
    effective = device_policy.effective_tiers(OWNER, 'n1', caps)
    # gui is allowed by the Home but not by the device: the Home never raises it.
    assert effective['write'] is True and effective['gui'] is False and effective['exec'] is False
    assert device_policy.effective_tiers(OTHER, 'n1', caps)['write'] is False


def test_forgetting_a_node_resets_it_to_read_only_and_clears_defaults():
    device_policy.set_ceiling(OWNER, 'n1', {'write': True})
    device_policy.set_defaults(OWNER, {'computer': 'n1'})
    device_policy.forget_node(OWNER, 'n1')
    assert device_policy.home_tiers(OWNER, 'n1')['write'] is False
    assert device_policy.defaults(OWNER)['computer'] is None


def test_ceiling_request_rejects_removing_read_and_unknown_tiers():
    with pytest.raises(ValueError):
        device_policy.set_ceiling(OWNER, 'n1', {'read': False})
    with pytest.raises(ValueError):
        device_policy.set_ceiling(OWNER, 'n1', {'root': True})


def test_unavailable_policy_fails_closed(monkeypatch):
    from lib.agent_core.storage_errors import StorageError

    def broken(write=False):
        raise StorageError('storage_unavailable', 'down')
    device_policy.set_ceiling(OWNER, 'n1', {'write': True})
    device_policy.invalidate()
    monkeypatch.setattr(device_policy, '_default_client', broken)
    assert device_policy.home_tiers(OWNER, 'n1')['write'] is False


def test_cached_policy_changes_only_on_explicit_invalidation(policy_records):
    device_policy.set_ceiling(OWNER, 'n1', {'write': True})
    assert device_policy.home_tiers(OWNER, 'n1')['write'] is True
    # Another process committed a lower ceiling: this one keeps its cache ...
    device_policy.set_ceiling(OWNER, 'n1', {}, client=policy_records)
    device_policy._cache[OWNER] = ({'version': 1, 'owner': OWNER, 'defaults': {
        'computer': None, 'server': None, 'browser': None},
        'ceilings': {'n1': {t: True for t in device_policy.CEILING_TIERS}}}, 1)
    assert device_policy.home_tiers(OWNER, 'n1')['write'] is True
    # ... until device_policy_reconcile drops it.
    from lib.tasks_pkg.command_controls import apply_command_control
    assert apply_command_control('device_policy_reconcile', '', user_id=OWNER) == {'applied': True}
    assert device_policy.home_tiers(OWNER, 'n1')['write'] is False


# ── placement ───────────────────────────────────────────────────────────

def test_named_device_runs_there_or_is_refused_never_replaced():
    _register('a1', 'laptop')
    _register('a2', 'desk')
    assert routing.route_device_call(OWNER, selector='LAPTOP').agent['agent_id'] == 'a1'
    _offline('a1')
    refusal = routing.route_device_call(OWNER, selector='laptop')
    assert isinstance(refusal, routing.RoutingRefusal) and refusal.code == 'device_offline'
    assert refusal.agent['agent_id'] == 'a1'
    assert routing.route_device_call(OWNER, selector='nope').code == 'device_not_found'


def test_unnamed_call_uses_default_else_only_online_else_refuses():
    assert routing.route_device_call(OWNER).code == 'device_none'
    _register('a1', 'laptop')
    route = routing.route_device_call(OWNER)
    assert route.agent['agent_id'] == 'a1' and route.reason == 'only_online'
    _register('a2', 'desk')
    _register('f1', 'foreign', owner=OTHER)
    refusal = routing.route_device_call(OWNER)
    assert refusal.code == 'device_ambiguous'
    assert 'laptop' in refusal.message and 'desk' in refusal.message
    assert 'foreign' not in refusal.message
    device_policy.set_defaults(OWNER, {'computer': 'a2'})
    route = routing.route_device_call(OWNER)
    assert route.agent['agent_id'] == 'a2' and route.reason == 'default'
    # The default is per kind: a server-kind call does not take the computer default.
    assert routing.route_device_call(OWNER, kind='server').code == 'device_ambiguous'
    _offline('a2')
    refusal = routing.route_device_call(OWNER)
    assert refusal.code == 'device_offline' and 'not replaced' in refusal.message


def test_desktop_tool_without_device_is_refused_when_several_are_online():
    from lib.tasks_pkg.handlers.misc import _agents
    _register('a1', 'laptop')
    _register('a2', 'desk')
    text = _agents._run_desktop('desktop_list_files', {}, user_id=str(OWNER))
    assert text.startswith('Desktop Agent Error: device_ambiguous')
    assert db.pending_commands_count() == 0


# ── one enforcement boundary on the Home, re-check on claim and on the node ─

def test_enqueue_refuses_a_tier_outside_ceiling_and_device():
    _register('a1', caps={'write': True})
    result, error = db.send_desktop_command('desktop_write_file', {'path': 'x', 'content': ''},
                                            timeout=0, target_agent_id='a1', user_id=str(OWNER))
    assert result is None and error.startswith('permission_denied') and 'Home' in error
    device_policy.set_ceiling(OWNER, 'a1', {'write': True, 'exec': True})
    cmd_id, error = db.enqueue_desktop_command('desktop_write_file', {}, target_agent_id='a1',
                                               user_id=str(OWNER))
    assert error is None and cmd_id
    # exec: the Home allows it, the device switch does not.
    _cmd, error = db.enqueue_desktop_command('desktop_run_command', {}, target_agent_id='a1',
                                             user_id=str(OWNER))
    assert error.startswith('permission_denied') and 'has not granted' in error


def test_ceiling_lowered_after_enqueue_withdraws_the_command_before_claim():
    _register('a1', caps={'write': True})
    device_policy.set_ceiling(OWNER, 'a1', {'write': True})
    cmd_id, error = db.enqueue_desktop_command('desktop_write_file', {}, target_agent_id='a1',
                                               user_id=str(OWNER))
    assert error is None
    device_policy.set_ceiling(OWNER, 'a1', {})
    assert db.take_pending_commands(agent_id='a1', user_id=str(OWNER)) == []
    cmd = db.command_queue[cmd_id]
    assert cmd['event'].is_set() and cmd['error'].startswith('permission_denied')
    assert 'claimed_agent_id' not in cmd


def test_claimed_command_carries_the_home_ceiling_and_the_node_rechecks_it():
    from lib.desktop_agent._dispatch import home_tier_refusal
    _register('a1', caps={'write': True})
    device_policy.set_ceiling(OWNER, 'a1', {'write': True})
    db.enqueue_desktop_command('desktop_write_file', {}, target_agent_id='a1',
                               user_id=str(OWNER))
    [wire] = db.take_pending_commands(agent_id='a1', user_id=str(OWNER))
    assert wire['home_tiers']['write'] is True and wire['home_tiers']['exec'] is False
    assert home_tier_refusal('desktop_write_file', {}, wire['home_tiers']) is None
    assert home_tier_refusal('desktop_run_command', {}, wire['home_tiers']).startswith(
        'permission_denied')
    assert home_tier_refusal('desktop_read_file', {}, {'read': True}) is None
    assert home_tier_refusal('desktop_write_file', {}, None) is None  # pre-ceiling Home


# ── node loss: settle once, never elsewhere ─────────────────────────────

def test_lost_device_settles_claimed_and_queued_calls_as_device_unavailable(monkeypatch):
    monkeypatch.setattr(db, '_CONNECTED_WINDOW_S', 0.3)
    _register('a1', caps={})
    _register('a2', caps={})
    outcome = {}

    def call():
        outcome['value'] = db.send_desktop_command('desktop_list_files', {}, timeout=None,
                                                   target_agent_id='a1', user_id=str(OWNER),
                                                   cmd_id='claimed-call')
    worker = threading.Thread(target=call)
    worker.start()
    deadline = time.monotonic() + 2
    while 'claimed-call' not in db.command_queue and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [c['id'] for c in db.take_pending_commands(agent_id='a1', user_id=str(OWNER))] == [
        'claimed-call']
    started = time.monotonic()
    while worker.is_alive() and time.monotonic() - started < 3:
        _register('a2', caps={})  # another device stays online
        time.sleep(0.05)
    worker.join(timeout=0)
    assert not worker.is_alive(), 'the call hung after its device was lost'
    result, error = outcome['value']
    assert result is None and error.startswith('device_unavailable') and 'not retried' in error
    assert db.take_pending_commands(agent_id='a2', user_id=str(OWNER)) == []


def test_repeated_idempotency_key_attaches_to_the_queued_command():
    _register('a1', caps={})
    cmd_id = routing.call_idempotency_key({'_userId': OWNER, 'convId': 'c', 'id': 't'}, 3,
                                          'call_1', 'desktop_list_files')
    assert cmd_id == routing.call_idempotency_key(
        {'_userId': OWNER, 'convId': 'c', 'id': 't'}, 3, 'call_1', 'desktop_list_files')
    results = []

    def call():
        results.append(db.send_desktop_command('desktop_list_files', {}, timeout=5,
                                               target_agent_id='a1', user_id=str(OWNER),
                                               cmd_id=cmd_id))
    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    claimed = db.take_pending_commands(agent_id='a1', user_id=str(OWNER))
    assert [c['id'] for c in claimed] == [cmd_id]
    db.resolve_results([{'id': cmd_id, 'result': {'ok': 1}}], agent_id='a1',
                       user_id=str(OWNER))
    for thread in threads:
        thread.join(timeout=3)
    assert results == [({'ok': 1}, None), ({'ok': 1}, None)]
    # Another owner or type can never reuse the key.
    db.command_queue[cmd_id] = {**claimed[0], 'user_id': str(OWNER), 'event': threading.Event(),
                                'target_agent_id': 'a1', 'type': 'desktop_list_files'}
    assert db.send_desktop_command('desktop_write_file', {}, timeout=0, target_agent_id='a1',
                                   user_id=str(OWNER), cmd_id=cmd_id)[1].startswith(
        'Desktop command id is already used')


# ── node journal ────────────────────────────────────────────────────────

def test_node_journal_never_reruns_a_side_effecting_command(tmp_path):
    from lib.desktop_agent._journal import CallJournal
    journal = CallJournal(str(tmp_path / 'calls'))
    write = {'id': 'k1', 'type': 'desktop_write_file', 'params': {}}
    assert journal.begin(write) is None
    # A restart before completion: the outcome is unknown, it does not run again.
    again = CallJournal(str(tmp_path / 'calls')).begin(write)
    assert again['error'].startswith('device_unavailable') and 'not run again' in again['error']
    journal.complete('desktop_write_file', {}, {'id': 'k1', 'result': {'ok': True}, 'error': None})
    assert CallJournal(str(tmp_path / 'calls')).begin(write) == {
        'id': 'k1', 'result': {'ok': True}, 'error': None}
    read = {'id': 'k2', 'type': 'desktop_read_file', 'params': {}}
    assert journal.begin(read) is None and journal.begin(read) is None  # reads re-run


# ── browser kind ────────────────────────────────────────────────────────

def test_unpinned_browser_call_uses_default_browser_or_refuses():
    from lib.browser.dispatch import _route_unpinned_client
    one = [{'client_id': 'b1', 'name': 'work', 'last_poll': 2}]
    two = one + [{'client_id': 'b2', 'name': 'home', 'last_poll': 9}]
    assert _route_unpinned_client(str(OWNER), one) == ('b1', None)
    client, refusal = _route_unpinned_client(str(OWNER), two)
    assert client is None and 'device_ambiguous' in refusal
    device_policy.set_defaults(OWNER, {'browser': 'b1'})
    assert _route_unpinned_client(str(OWNER), two) == ('b1', None)
    device_policy.set_defaults(OWNER, {'browser': 'b3'})
    client, refusal = _route_unpinned_client(str(OWNER), two)
    assert client is None and 'device_offline' in refusal


# ── policy reaches every process that runs Turns ────────────────────────

def test_shard_pool_repeats_device_policy_reconcile_on_every_shard(monkeypatch):
    from serverctl_pkg import execution_shards
    from lib.tasks_pkg import command_control_transport
    calls = []
    pool = execution_shards.ShardPool.__new__(execution_shards.ShardPool)
    monkeypatch.setattr(pool, '_endpoints', lambda: [{'id': 1}, {'id': 2}], raising=False)
    monkeypatch.setattr(command_control_transport, 'call',
                        lambda endpoint, method, task_id, **kw: calls.append(
                            (endpoint['id'], method)) or {'applied': endpoint['id'] == 1})
    assert pool.apply_command_control('device_policy_reconcile', '', user_id=OWNER) == {
        'applied': False}
    assert calls == [(1, 'device_policy_reconcile'), (2, 'device_policy_reconcile')]


@pytest.mark.api
def test_ceiling_and_defaults_routes_are_owner_scoped(flask_client, monkeypatch):
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=1, name='devices-routing', scopes=['chat'])
    headers = {'Authorization': f'Bearer {token}'}
    _register('node-1', 'laptop', owner=1, caps={'write': True})
    _register('node-x', 'foreign', owner=2, caps={'write': True})
    resp = flask_client.put('/api/v4/devices/node-1/ceiling', headers=headers,
                            json={'tiers': {'write': True, 'exec': True}})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    body = body.get('data', body)
    assert body['home']['exec'] is True and body['device']['exec'] is False
    assert body['effective']['write'] is True and body['effective']['exec'] is False
    assert body['execution'] in ('not_split', 'applied')
    assert flask_client.put('/api/v4/devices/node-x/ceiling', headers=headers,
                            json={'tiers': {'write': True}}).status_code == 404
    assert flask_client.put('/api/v4/devices/node-1/ceiling', headers=headers,
                            json={'tiers': {'read': False}}).status_code == 400
    listing = flask_client.get('/api/v4/devices/permissions', headers=headers).get_json()
    listing = listing.get('data', listing)
    assert set(listing['permissions']) == {'node-1'} and listing['liveness_window_s'] == 15
    assert flask_client.put('/api/v4/devices/defaults', headers=headers,
                            json={'computer': 'node-x'}).status_code == 404
    resp = flask_client.put('/api/v4/devices/defaults', headers=headers,
                            json={'computer': 'node-1'})
    assert resp.status_code == 200
    got = flask_client.get('/api/v4/devices/defaults', headers=headers).get_json()
    got = got.get('data', got)
    assert got['defaults'] == {'computer': 'node-1', 'server': None, 'browser': None}
