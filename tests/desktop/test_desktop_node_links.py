"""Node links: one outbound bridge loop per joined server plus the local server.

Contract: docs/modules/remote_execution.md ("Nodes and pairing",
"Client execution and connection lifecycle"); owner lib/desktop_agent/links.py.
"""
import subprocess
import sys
import threading
import time

import pytest

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
