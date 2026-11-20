"""Node links: one outbound bridge loop per joined server plus the local server.

Contract: docs/modules/remote_execution.md ("Nodes and pairing",
"Client execution and connection lifecycle"); owner lib/desktop_agent/links.py.
"""
import subprocess
import sys
import threading
import time

import pytest

from lib.desktop_agent._pair import resume_attachment as resume_routes

pytestmark = pytest.mark.unit


class Relay:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A NodeLinks over a private config with run_agent stubbed per link."""
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    import lib.desktop_agent as agent
    import lib.desktop_agent._pair as pair
    from lib.desktop_agent import links

    calls = {}
    lock = threading.Lock()

    def run(url, perms, **kwargs):
        with lock:
            calls[url] = (perms, kwargs)
        kwargs['stop_event'].wait(5)

    monkeypatch.setattr(agent, 'run_agent', run)
    monkeypatch.setattr(pair, 'resume_attachment', lambda url, secret, **kw: (url, secret))
    monkeypatch.setattr(links, 'ensure_relay',
                        lambda state, log: state.setdefault('browser_relay', Relay()))
    return links, calls


def _wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _attach(url, secret):
    from lib.desktop_agent.config import save_attachment
    from lib.desktop_wire.node_protocol import attachment_id
    return save_attachment({'id': attachment_id(url), 'url': url, 'secret': secret,
                            'candidates': [url + '/alt'], 'label': url,
                            'state': 'active'})


def test_one_link_per_server_sharing_one_permission_dict(node):
    links, calls = node
    _attach('https://a.example', 'tok-a')
    _attach('https://b.example', 'tok-b')
    perms = {'allow_exec': False}
    runtime = links.NodeLinks(perms, lambda msg: None)
    try:
        runtime.start(local=('http://127.0.0.1:15432', 'process-cap'))
        assert _wait_for(lambda: len(calls) == 3)
        assert set(calls) == {'http://127.0.0.1:15432', 'https://a.example',
                              'https://b.example'}
        assert all(live is perms for live, _ in calls.values())
        local_hooks = calls['http://127.0.0.1:15432'][1]
        assert local_hooks['bridge_secret'] == 'process-cap'
        assert local_hooks['route_repair'] is None
        assert local_hooks['browser_relay'] is None
        remote_hooks = calls['https://a.example'][1]
        assert remote_hooks['bridge_secret'] == 'tok-a'
        assert isinstance(remote_hooks['browser_relay'], Relay)
        rows = {row['id']: row for row in runtime.statuses()}
        assert rows['local']['local'] is True
        assert sum(1 for row in rows.values() if not row['local']) == 2
    finally:
        assert runtime.stop()
    assert all(not row['enabled'] for row in runtime.statuses())


def test_revoked_link_stops_and_is_persisted(node):
    links, calls = node
    record = _attach('https://a.example', 'tok-a')
    runtime = links.NodeLinks({}, lambda msg: None)
    try:
        runtime.start()
        assert _wait_for(lambda: 'https://a.example' in calls)
        hooks = calls['https://a.example'][1]
        hooks['on_status']({'state': 'auth'})
        assert hooks['stop_event'].is_set()
        from lib.desktop_agent.config import attachments
        assert attachments()[0]['state'] == 'revoked'
        row = next(r for r in runtime.statuses() if r['id'] == record['id'])
        assert row['state'] == 'revoked'
    finally:
        runtime.stop()
    # A revoked attachment does not start again on the next launch.
    calls.clear()
    again = links.NodeLinks({}, lambda msg: None)
    try:
        again.start()
        time.sleep(0.05)
        assert 'https://a.example' not in calls
    finally:
        again.stop()


def test_route_repair_uses_own_candidates_and_persists(node, monkeypatch):
    links, calls = node
    import lib.desktop_agent._pair as pair
    _attach('https://a.example', 'tok-a')
    seen = {}

    def resume(url, secret, **kw):
        seen.update(kw)
        kw['persist']('https://a.example/alt')
        return 'https://a.example/alt', secret

    monkeypatch.setattr(pair, 'resume_attachment', resume)
    runtime = links.NodeLinks({}, lambda msg: None)
    try:
        runtime.start()
        assert _wait_for(lambda: 'https://a.example' in calls)
        assert calls['https://a.example'][1]['route_repair']() == (
            'https://a.example/alt', 'tok-a')
        assert list(seen['candidates']) == ['https://a.example/alt']
        from lib.desktop_agent.config import attachments
        assert attachments()[0]['url'] == 'https://a.example/alt'
    finally:
        runtime.stop()


def test_join_starts_a_link_and_leave_stops_it(node, monkeypatch):
    links, calls = node
    import lib.desktop_agent.attachments as attachments_mod
    monkeypatch.setattr(attachments_mod, '_redeem',
                        lambda url, body, timeout: ({'token': 'tok-new', 'id': 'k1'}, ''))
    runtime = links.NodeLinks({}, lambda msg: None)
    try:
        runtime.start()
        record = runtime.join('https://c.example/proxy/15000/#tofu-pair=abcdefgh',
                              node_id='node-1', name='laptop')
        assert _wait_for(lambda: 'https://c.example/proxy/15000' in calls)
        assert runtime.leave(record['id']) is True
        assert calls['https://c.example/proxy/15000'][1]['stop_event'].is_set()
        from lib.desktop_agent.config import attachments
        assert attachments() == []
    finally:
        runtime.stop()


def test_restart_refuses_worker_that_has_not_stopped():
    from lib.desktop_agent import links

    class Stuck:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 5

    state = {'thread': Stuck(), 'stop': threading.Event(), 'enabled': True}
    assert links.restart(state, {}, lambda msg: None) is False
    assert state['stop'].is_set()
    assert isinstance(state['thread'], Stuck)
    assert not state['enabled']


def test_links_module_import_is_headless():
    code = '''import sys
import lib.desktop_agent.links
assert not any(name in sys.modules for name in
               ('server', 'quart', 'tkinter', 'pystray', 'PIL', 'desktop.launcher'))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_relay_is_per_link_with_dynamic_routes_and_cleanup(monkeypatch):
    from lib.desktop_agent import _browser_relay, links
    created = []

    class Broker(Relay):
        def __init__(self, urls, **kwargs):
            super().__init__()
            self.urls = urls
            created.append(self)

        def start(self):
            return True

    monkeypatch.setattr(_browser_relay, 'BrowserRelay', Broker)
    state = {'url': 'https://original.test', 'candidates': ['https://candidate.test']}
    links.ensure_relay(state, lambda msg: None)
    links.ensure_relay(state, lambda msg: None)
    assert len(created) == 1
    state['url'] = 'https://new.test'
    assert created[0].urls() == ['https://new.test', 'https://candidate.test']
    links.stop(state, lambda msg: None)
    assert created[0].closed
    assert 'browser_relay' not in state


def test_launcher_runs_the_local_link_read_only_by_default(node, monkeypatch):
    links, calls = node
    from desktop import launcher
    from lib.desktop_agent._permissions import safe_default
    monkeypatch.setattr(launcher, '_log', lambda msg: None)
    state = {}
    try:
        launcher._start_computer_control(15432, state)
        launcher._start_computer_control(15432, state)
        assert _wait_for(lambda: 'http://127.0.0.1:15432' in calls)
        assert len(calls) == 1
        perms, _hooks = calls['http://127.0.0.1:15432']
        assert perms == safe_default()
        assert state['enabled']
    finally:
        launcher._stop_computer_control(state)
    assert not state['enabled']


@pytest.mark.parametrize('cancel', [None, threading.Event])
def test_rejoining_one_running_attachment_preserves_the_other(node, monkeypatch, cancel):
    links, calls = node
    import lib.desktop_agent.attachments as attachments_mod
    from lib.desktop_agent.config import attachments
    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    monkeypatch.setattr(attachments_mod, '_redeem',
                        lambda *args, **kwargs: ({'token': 'tok-renewed', 'id': 'k2'}, ''))
    runtime = links.NodeLinks({'allow_exec': False}, lambda msg: None)
    try:
        runtime.start()
        assert _wait_for(lambda: len(calls) == 2)
        old_a = calls[a['url']][1]
        old_b = calls[b['url']][1]
        renewed = runtime.join(a['url'] + '/#tofu-pair=abcdefgh',
                               node_id='node-1', stop=cancel() if cancel else None)
        assert renewed['id'] == a['id']
        assert _wait_for(lambda: calls[a['url']][1] is not old_a)
        assert old_a['stop_event'].is_set()
        assert calls[b['url']][1] is old_b
        assert not old_b['stop_event'].is_set()
        assert next(row for row in attachments() if row['id'] == b['id']) == b
        assert len(attachments()) == 2
    finally:
        runtime.stop()


@pytest.fixture
def controlled_workers(node, monkeypatch):
    from types import SimpleNamespace

    links, _ = node
    workers = []

    class Worker:
        def __init__(self, **kwargs):
            self.alive = False
            self.stuck = False
            workers.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout == 5
            if not self.stuck:
                self.alive = False

    monkeypatch.setattr(links, 'threading', SimpleNamespace(
        RLock=threading.RLock, Event=threading.Event, Thread=Worker,
        current_thread=threading.current_thread))
    return links, workers


def test_unsuccessful_rejoin_retains_worker_and_preserves_other_link(controlled_workers,
                                                                   monkeypatch):
    links, workers = controlled_workers
    import lib.desktop_agent.attachments as attachments_mod
    from lib.desktop_agent.config import attachments

    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    monkeypatch.setattr(attachments_mod, '_redeem',
                        lambda *args, **kwargs: ({'token': 'tok-renewed', 'id': 'k2'}, ''))
    runtime = links.NodeLinks({}, lambda msg: None)
    runtime.start()
    old_a = runtime._links[a['id']]
    old_b = runtime._links[b['id']]
    old_a['thread'].stuck = True
    try:
        renewed = runtime.join(a['url'] + '/#tofu-pair=abcdefgh', node_id='node-1')
        assert renewed['secret'] == 'tok-renewed'
        assert runtime._links[a['id']] is old_a
        assert old_a['stop'].is_set()
        assert old_a['thread'].is_alive()
        assert len(workers) == 2
        assert runtime._links[b['id']] is old_b
        assert not old_b['stop'].is_set()
        assert next(row for row in attachments() if row['id'] == b['id']) == b

        old_a['thread'].alive = False
        runtime.start()
        assert runtime._links[a['id']] is not old_a
        assert runtime._links[a['id']]['secret'] == 'tok-renewed'
        assert runtime._links[b['id']] is old_b
        assert len(workers) == 3
        assert sum(worker.is_alive() for worker in workers) == 2
    finally:
        old_a['thread'].stuck = False
        runtime.stop()


@pytest.mark.parametrize('stuck_id', ['attachment', 'local'])
def test_unsuccessful_stop_start_retains_survivor_without_overlap(controlled_workers,
                                                                stuck_id):
    links, workers = controlled_workers
    from lib.desktop_agent.config import attachments

    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    runtime = links.NodeLinks({}, lambda msg: None)
    local = ('http://127.0.0.1:15432', 'process-cap')
    runtime.start(local=local)
    key = a['id'] if stuck_id == 'attachment' else links.LOCAL_LINK
    survivor = runtime._links[key]
    survivor['thread'].stuck = True
    try:
        assert runtime.stop() is False
        assert not runtime.running
        assert runtime._links == {key: survivor}
        assert survivor['stop'].is_set()
        assert sum(worker.is_alive() for worker in workers) == 1

        runtime.start(local=local)
        assert runtime._links[key] is survivor
        assert len(workers) == 5
        assert sum(worker.is_alive() for worker in workers) == 3
        assert next(row for row in attachments() if row['id'] == b['id']) == b
        other_states = {key: state for key, state in runtime._links.items()
                        if state is not survivor}

        survivor['thread'].alive = False
        runtime.start(local=local)
        assert runtime._links[key] is not survivor
        assert len(workers) == 6
        assert sum(worker.is_alive() for worker in workers) == 3
        assert all(runtime._links[key] is state for key, state in other_states.items())
    finally:
        survivor['thread'].stuck = False
        assert runtime.stop()


def test_unsuccessful_leave_retains_worker_until_stopped(controlled_workers):
    links, workers = controlled_workers
    from lib.desktop_agent.config import attachments

    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    runtime = links.NodeLinks({}, lambda msg: None)
    runtime.start()
    old_a = runtime._links[a['id']]
    old_b = runtime._links[b['id']]
    old_a['thread'].stuck = True
    try:
        assert runtime.leave(a['id']) is True
        assert runtime._links[a['id']] is old_a
        assert old_a['stop'].is_set()
        assert old_a['thread'].is_alive()
        assert attachments() == [b]
        runtime.start()
        assert len(workers) == 2
        assert runtime._links[b['id']] is old_b
        assert not old_b['stop'].is_set()

        old_a['thread'].stuck = False
        assert runtime.stop()
        assert runtime._links == {}
        assert not any(worker.is_alive() for worker in workers)
    finally:
        old_a['thread'].stuck = False
        runtime.stop()


def test_retiring_refusal_cannot_revoke_renewed_attachment(node, monkeypatch):
    links, calls = node
    from lib.desktop_agent import attachments as joining, config
    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    monkeypatch.setattr(joining, '_redeem',
                        lambda *args, **kwargs: ({'token': 'tok-renewed', 'id': 'k2'}, ''))
    runtime = links.NodeLinks({}, lambda msg: None)
    runtime.start()
    assert _wait_for(lambda: len(calls) == 2)
    old_a = calls[a['url']][1]
    old_b = calls[b['url']][1]
    real_stop = links.stop

    def delayed_refusal(state, log, **kwargs):
        if state.get('secret') == a['secret']:
            assert next(r for r in config.attachments() if r['id'] == a['id'])['secret'] == 'tok-renewed'
            old_a['on_status']({'state': 'auth'})
        return real_stop(state, log, **kwargs)

    monkeypatch.setattr(links, 'stop', delayed_refusal)
    try:
        renewed = runtime.join(a['url'] + '/#tofu-pair=abcdefgh', node_id='node-1')
        assert next(r for r in config.attachments() if r['id'] == a['id']) == renewed
        assert calls[b['url']][1] is old_b
        assert not old_b['stop_event'].is_set()
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
        assert runtime.stop()
        calls.clear()
        runtime.start()
        assert _wait_for(lambda: len(calls) == 2)
        assert calls[a['url']][1]['bridge_secret'] == 'tok-renewed'
        calls[a['url']][1]['on_status']({'state': 'auth'})
        assert next(r for r in config.attachments() if r['id'] == a['id'])['state'] == 'revoked'
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
    finally:
        runtime.stop()


def test_route_round_trip_survives_restart_without_losing_authorized_routes(node, monkeypatch):
    links, calls = node
    from lib.desktop_agent import _pair, config
    primary, alternate = 'http://127.0.0.1:19101', 'http://127.0.0.1:19102'
    record = _attach(primary, 'route-token')
    config.update_attachment(record['id'], candidates=[alternate])
    b = _attach('http://127.0.0.1:19103', 'other-token')
    monkeypatch.setattr(_pair, 'resume_attachment', resume_routes)
    live = {alternate}
    monkeypatch.setattr(_pair, 'probe_server', lambda url, **kwargs: (url in live, ''))
    runtime = links.NodeLinks({}, lambda msg: None)
    try:
        runtime.start()
        assert _wait_for(lambda: len(calls) == 2)
        b_worker = runtime._links[b['id']]['thread']
        repair = calls[primary][1]['route_repair']
        assert repair() == (alternate, 'route-token')
        saved = next(r for r in config.attachments() if r['id'] == record['id'])
        assert {saved['url'], *saved['candidates']} == {primary, alternate}
        live.clear()
        live.add(primary)
        assert repair() == (primary, 'route-token')
        assert runtime._links[b['id']]['thread'] is b_worker
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
        live.clear()
        live.add(alternate)
        assert repair() == (alternate, 'route-token')
        assert runtime.stop()
        calls.clear()
        runtime.start()
        assert _wait_for(lambda: alternate in calls)
        live.clear()
        live.add(primary)
        assert calls[alternate][1]['route_repair']() == (primary, 'route-token')
        saved = next(r for r in config.attachments() if r['id'] == record['id'])
        assert saved['secret'] == 'route-token'
        assert {saved['url'], *saved['candidates']} == {primary, alternate}
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
    finally:
        runtime.stop()


@pytest.mark.parametrize('newer_action', ['join', 'leave'])
def test_delayed_join_return_cannot_install_superseded_or_removed_record(node, monkeypatch,
                                                                       newer_action):
    links, calls = node
    from lib.desktop_agent import attachments as joining, config
    a = _attach('https://a.example', 'tok-a')
    b = _attach('https://b.example', 'tok-b')
    saved_first, release_first = threading.Event(), threading.Event()
    real_join = joining.join
    errors = []

    def redeem(url, body, timeout):
        return {'token': body['code'], 'id': body['code']}, ''

    def paused_join(text, **kwargs):
        record = real_join(text, **kwargs)
        if record['secret'] == 'credential-1':
            saved_first.set()
            assert release_first.wait(5)
        return record

    monkeypatch.setattr(joining, '_redeem', redeem)
    monkeypatch.setattr(joining, 'join', paused_join)
    runtime = links.NodeLinks({}, lambda msg: None)
    runtime.start()
    assert _wait_for(lambda: len(calls) == 2)
    b_state = runtime._links[b['id']]

    def first_join():
        try:
            runtime.join(a['url'] + '/#tofu-pair=credential-1', node_id='node-1')
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=first_join)
    worker.start()
    try:
        assert saved_first.wait(5)
        if newer_action == 'join':
            runtime.join(a['url'] + '/#tofu-pair=credential-2', node_id='node-1')
            current_worker = runtime._links[a['id']]['thread']
        else:
            assert runtime.leave(a['id'])
        release_first.set()
        worker.join(5)
        assert not worker.is_alive() and not errors
        if newer_action == 'join':
            assert runtime._links[a['id']]['secret'] == 'credential-2'
            assert runtime._links[a['id']]['thread'] is current_worker
            assert next(r for r in config.attachments() if r['id'] == a['id'])['secret'] == 'credential-2'
        else:
            assert a['id'] not in runtime._links
            assert config.attachments() == [b]
        assert runtime._links[b['id']] is b_state
        assert not b_state['stop'].is_set()
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
    finally:
        release_first.set()
        worker.join(5)
        runtime.stop()


@pytest.mark.parametrize('repair_first', [False, True])
def test_rejoin_through_authorized_route_keeps_stable_attachment_identity(node, monkeypatch,
                                                                        repair_first):
    links, calls = node
    from lib.desktop_agent import attachments as joining, config, _pair
    primary, alternate = 'http://127.0.0.1:19201', 'http://127.0.0.1:19202'
    redeemed = []

    def redeem(url, body, timeout):
        redeemed.append(url)
        return {'token': body['code'], 'id': body['code']}, ''

    monkeypatch.setattr(joining, '_redeem', redeem)
    monkeypatch.setattr(_pair, 'resume_attachment', resume_routes)
    monkeypatch.setattr(_pair, 'probe_server', lambda url, **kwargs: (url == primary, ''))
    runtime = links.NodeLinks({}, lambda msg: None)
    try:
        original = runtime.join(primary + '/#tofu-pair=credential-1&via=' + alternate,
                                node_id='node-1')
        assert original['url'] == alternate
        b = runtime.join('http://127.0.0.1:19203/#tofu-pair=credential-b', node_id='node-1')
        runtime.start()
        assert _wait_for(lambda: len(calls) == 2)
        old_a = runtime._links[original['id']]
        b_state = runtime._links[b['id']]
        if repair_first:
            assert calls[alternate][1]['route_repair']() == (primary, 'credential-1')
        renewed = runtime.join(primary + '/#tofu-pair=credential-2', node_id='node-1')
        assert renewed['id'] == original['id']
        assert len(config.attachments()) == len(runtime._links) == 2
        assert old_a['stop'].is_set() and not old_a['thread'].is_alive()
        assert runtime._links[original['id']]['secret'] == 'credential-2'
        assert runtime._links[b['id']] is b_state and not b_state['stop'].is_set()
        assert next(r for r in config.attachments() if r['id'] == b['id']) == b
        assert {renewed['url'], *renewed['candidates']} == {primary, alternate}
        assert runtime.stop()
        calls.clear()
        runtime.start()
        assert _wait_for(lambda: len(calls) == 2)
        assert runtime._links[original['id']]['secret'] == 'credential-2'
        assert runtime.leave(renewed['id'])
        assert config.attachments() == [b]
        assert set(runtime._links) == {b['id']}
        assert redeemed == [alternate, b['url'], primary]
    finally:
        runtime.stop()


def test_route_association_uses_only_saved_routes_and_refuses_ambiguous_renewal(node):
    from lib.desktop_agent import config
    a = _attach('http://127.0.0.1:19301', 'a')
    b = _attach('http://127.0.0.1:19302', 'b')
    incoming = dict(b, id='new-id', url='http://127.0.0.1:19303',
                    candidates=[a['url']])
    assert config.save_attachment(incoming, renew_route=True)['id'] == 'new-id'
    config.update_attachment(a['id'], candidates=[incoming['url']])
    before = config.attachments()
    with pytest.raises(ValueError, match='multiple saved attachments'):
        config.save_attachment(dict(incoming, secret='renewed'), renew_route=True)
    assert config.attachments() == before
