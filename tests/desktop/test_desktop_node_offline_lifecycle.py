"""Ordinary link lifecycle with local fake POST responses, never real transport.

Composes the Session.post seam from test_desktop_runtime_reliability with the
private config/relay seams from test_desktop_node_links. No restart is modeled.
"""
import builtins
import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def offline_boundary(tmp_path):
    from tests.conftest import _PYTEST_DATA_ROOT

    monkeypatch = pytest.MonkeyPatch()
    effects, pairing_calls, runtimes, gui_originals = [], [], [], []
    active = [True]
    # Cold package imports also use the existing disposable pytest data fixture.
    roots = (tmp_path.resolve(), Path(_PYTEST_DATA_ROOT).resolve())

    def refuse(name):
        effects.append(name)
        pytest.fail('offline link fixture forbids ' + name)

    def local_path(path):
        return isinstance(path, int) or any(root == path or root in path.parents for root in roots)

    def audit(event, args):
        if not active[0]:
            return
        if event.startswith('socket.') or event in {
                'subprocess.Popen', 'os.system', 'os.fork', 'os.forkpty',
                'os.posix_spawn', 'os.exec', 'os.kill', 'os.killpg', 'pty.spawn'}:
            refuse(event)
        paths = ()
        if event == 'open' and args[2] & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            paths = args[:1]
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod',
                       'os.truncate', 'os.utime'}:
            paths = args[:1]
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            paths = args[:2]
        for path in paths:
            if not local_path(path if isinstance(path, int) else Path(path).resolve()):
                refuse('write outside disposable fixture: ' + event)

    real_import = builtins.__import__

    class InertGUI:
        def __getattr__(self, name):
            refuse('GUI dependency use ' + name)

    inert_gui = InertGUI()

    def guarded_import(name, *args, **kwargs):
        if name in {'pyautogui', 'pyperclip'}:
            raise ModuleNotFoundError('optional GUI dependency disabled by fixture', name=name)
        if name.split('.')[0] in {
                'desktop', 'server', 'routes', 'tkinter', 'pystray',
                'webview', 'PIL'}:
            refuse('GUI/service import ' + name)
        return real_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        refuse('command, probe, or unhandled HTTP call')

    def pairing_forbidden(*args, **kwargs):
        pairing_calls.append(True)
        refuse('pairing')

    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    path = str(tmp_path / 'node' / 'agent.json')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', path)
    sys.addaudithook(audit)
    try:
        yield SimpleNamespace(patch=monkeypatch, effects=effects, pairing_calls=pairing_calls,
                              runtimes=runtimes, path=path, inert_gui=inert_gui,
                              gui_originals=gui_originals,
                              forbidden=forbidden, pairing_forbidden=pairing_forbidden)
    finally:
        survivors = [state['thread'] for runtime in runtimes
                     for state in runtime._links.values()
                     if state.get('thread') and state['thread'].is_alive()]
        if survivors:
            pytest.fail('poll worker survived cleanup; private config and effect guards retained')
        active[0] = False
        monkeypatch.undo()
        for module, mouse, clipboard in gui_originals:
            assert module.pyautogui is mouse and mouse is not inert_gui
            assert module.pyperclip is clipboard and clipboard is not inert_gui


@pytest.fixture
def offline_links(offline_boundary):
    boundary = offline_boundary
    monkeypatch = boundary.patch
    from lib.desktop_agent import _pair, _run, _runtime, attachments, config, links, _gui
    from lib.desktop_agent import _journal
    from lib.desktop_wire import protocol_version
    from lib.desktop_wire.node_protocol import attachment_id
    from lib.http_transport import outbound
    import lib.json_store as json_store

    condition = threading.Condition()
    effects, pairing_calls, closed_sessions = boundary.effects, boundary.pairing_calls, []
    forbidden, pairing_forbidden = boundary.forbidden, boundary.pairing_forbidden

    def refuse(name):
        effects.append(name)
        pytest.fail('offline link fixture forbids ' + name)

    real_close = _run.requests.Session.close

    def close_session(session):
        closed_sessions.append(session)
        real_close(session)

    # The fixed path also confines a delayed worker after outer env fixtures restore.
    monkeypatch.setattr(config, 'config_path', lambda: boundary.path)
    boundary.gui_originals.append((_gui, _gui.pyautogui, _gui.pyperclip))
    monkeypatch.setattr(_gui, 'pyautogui', boundary.inert_gui)
    monkeypatch.setattr(_gui, 'pyperclip', boundary.inert_gui)
    monkeypatch.setattr(config, '_host', lambda: (
        'synthetic-host', {'machine': 'fixture', 'hostname': 'fixture', 'container': ''},
        'synthetic-host'))
    monkeypatch.setattr(_run, '_system_facts', lambda: None)
    monkeypatch.setattr(_run.socket, 'gethostname', lambda: 'synthetic-host')
    monkeypatch.setattr(_runtime, 'outbox_memory_budget', lambda: 1024 * 1024)
    monkeypatch.setattr(links, 'ensure_relay', lambda *args: None)
    monkeypatch.setattr(links, 'resume', forbidden)
    monkeypatch.setattr(_pair, 'resume_attachment', forbidden)
    monkeypatch.setattr(_pair, 'probe_server', forbidden)
    monkeypatch.setattr(attachments, 'join', pairing_forbidden)
    monkeypatch.setattr(attachments, '_redeem', pairing_forbidden)
    monkeypatch.setattr(attachments, '_redeem_via_browser', pairing_forbidden)
    monkeypatch.setattr(_run, 'dispatch_command', forbidden)
    monkeypatch.setattr(_run, 'main', forbidden)
    monkeypatch.setattr(_run.requests.Session, 'request', forbidden)
    monkeypatch.setattr(_run.requests.Session, 'send', forbidden)
    monkeypatch.setattr(_run.requests.Session, 'close', close_session)
    quiet = logging.Logger('offline-link-fixture')
    for module in (_run, _runtime, _journal, config, json_store):
        monkeypatch.setattr(module, 'logger', quiet)

    records = {}
    services = {}
    runtime = links.NodeLinks({}, lambda message: None)
    boundary.runtimes.append(runtime)

    class Response:
        def __init__(self, code=200, body=None):
            self.status_code = code
            self.body = json.dumps(body if body is not None else {
                'commands': [], 'protocol_version': protocol_version(),
                'server_time': 1700000000.0})

        def json(self):
            body = json.loads(self.body)
            if self.status_code == 200 and body.get('commands') not in ([], {}):
                refuse('nonempty command response')
            return body

    def post(session, url, **kwargs):
        if url not in services:
            refuse('unhandled fake POST destination')
        service = services[url]
        record = service['record']
        payload = kwargs['json']
        assert kwargs['headers'] == {'X-Bridge-Secret': record['secret']}
        assert payload['results'] == payload['streams'] == []
        assert payload['agent']['agent_id'] == identity['node_id']
        assert payload['agent']['share_roots'] == []
        assert not any(payload['agent']['capabilities'].values())
        with condition:
            service['requests'].append((session, threading.current_thread(), payload))
            condition.notify_all()
            assert condition.wait_for(
                lambda: service['responses'] or service['closing'], timeout=10), (
                    'controller did not release fake POST')
            if service['closing']:
                return Response()
            return service['responses'].pop(0)

    def exchange(name, response):
        service = services[records[name]['url'] + '/api/desktop/poll']
        with condition:
            assert condition.wait_for(lambda: service['requests'], timeout=10), (
                'production worker did not reach fake POST')
            before = len(service['requests'])
            service['responses'].append(response)
            condition.notify_all()
            # The next POST is a barrier after production parsing/status handling.
            assert condition.wait_for(
                lambda: len(service['requests']) == before + 1, timeout=10), (
                    'production worker did not continue polling')

    def close(name):
        with condition:
            services[records[name]['url'] + '/api/desktop/poll']['closing'] = True
            condition.notify_all()

    try:
        identity = config.node_identity()
        for name in ('a', 'b'):
            url = 'https://%s.invalid' % name
            # Non-secret placeholders are consumed only by the fake POST above.
            record = config.save_attachment({
                'id': attachment_id(url), 'url': url, 'secret': 'synthetic-only-' + name,
                'candidates': [], 'label': name.upper(), 'state': 'active', 'paired_at': 123})
            records[name] = record
            services[url + '/api/desktop/poll'] = {
                'record': record, 'requests': [], 'responses': [], 'closing': False}
        monkeypatch.setattr(_run.requests.Session, 'post', post)
        yield SimpleNamespace(runtime=runtime, records=records, services=services,
                              exchange=exchange, close=close, response=Response,
                              identity=identity, effects=effects, pairing_calls=pairing_calls,
                              closed_sessions=closed_sessions)
    finally:
        for name in records:
            close(name)
        assert runtime.stop()
        for service in services.values():
            if service['requests']:
                session, worker, _ = service['requests'][0]
                assert not worker.is_alive()
                assert closed_sessions.count(session) == 1
        assert effects == pairing_calls == []


def test_detached_a_stays_detached_while_same_b_link_recovers(offline_links):
    from lib.desktop_agent import config

    fixture = offline_links
    runtime = fixture.runtime
    a, b = fixture.records['a'], fixture.records['b']
    sa = fixture.services[a['url'] + '/api/desktop/poll']
    sb = fixture.services[b['url'] + '/api/desktop/poll']
    runtime.start()
    fixture.exchange('a', fixture.response())
    fixture.exchange('b', fixture.response())
    initial = {row['id']: row for row in runtime.statuses()}
    assert set(initial) == {a['id'], b['id']}
    assert all(row['state'] == 'active' and row['enabled']
               and row['last_status'] == {'state': 'ok', 'transport': 'direct'}
               for row in initial.values())
    a_link, b_link = runtime._links[a['id']], runtime._links[b['id']]
    b_worker, b_stop = b_link['thread'], b_link['stop']
    b_session = sb['requests'][0][0]
    b_boot = sb['requests'][0][2]['agent']['boot_id']

    fixture.close('a')
    assert runtime.leave(a['id'])
    assert a_link['stop'].is_set() and not a_link['thread'].is_alive()
    assert fixture.closed_sessions == [sa['requests'][0][0]]
    a_attempts = len(sa['requests'])
    saved_after_detach = config.load_config()

    def remaining_b():
        assert config.attachments() == [b]
        assert config.load_config() == saved_after_detach
        assert config.node_identity() == fixture.identity
        assert set(runtime._links) == {b['id']}
        assert runtime._links[b['id']] is b_link
        assert b_link['thread'] is b_worker and b_worker.is_alive()
        assert b_link['stop'] is b_stop and not b_stop.is_set()
        assert (b_link['url'], b_link['secret']) == (b['url'], b['secret'])
        assert b_session not in fixture.closed_sessions
        assert runtime.perms == {}
        assert len(sa['requests']) == a_attempts
        assert all(session is b_session and worker is b_worker
                   and payload['agent']['boot_id'] == b_boot
                   for session, worker, payload in sb['requests'])
        rows = runtime.statuses()
        assert len(rows) == 1
        row = rows[0]
        assert (row['id'], row['url'], row['state'], row['local'], row['enabled']) == (
            b['id'], b['url'], 'active', False, True)
        assert fixture.effects == fixture.pairing_calls == []
        return row

    assert remaining_b()['last_status'] == {'state': 'ok', 'transport': 'direct'}
    fixture.exchange('b', fixture.response(503, {}))
    assert remaining_b()['last_status'] == {'state': 'http', 'code': 503}

    def assert_recovered():
        assert remaining_b()['last_status'] == {
            'state': 'ok', 'transport': 'direct'}, 'B has not recovered'

    malformed = fixture.response().json()
    malformed['commands'] = {}
    fixture.exchange('b', fixture.response(200, malformed))
    assert remaining_b()['last_status'] == {'state': 'proxy'}
    with pytest.raises(AssertionError, match='B has not recovered'):
        assert_recovered()
    fixture.exchange('b', fixture.response())
    assert_recovered()
    assert len(sb['requests']) == 5
