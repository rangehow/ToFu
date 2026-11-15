"""Browser-assisted desktop transport for SSO-fronted Codelab URLs."""

import json
import threading
import time

import pytest
import requests

from lib.desktop_agent._browser_relay import BrowserRelay, RelayResponse, \
    origin_of

# The relay is bound on FIXED loopback port ranges (15280-15289, 15380-15389,
# 15480-15489) and each test makes real requests to 127.0.0.1. Those ranges
# collide with a dev browser-relay or a second pytest worker on the same host,
# so the whole file runs in the dedicated no-xdist serial lane. BrowserRelay
# does not yet resolve the actual bound port for a port-0/ephemeral request
# (start() sets ``self.port = port`` to the REQUESTED port), so ephemeral
# allocation is impossible without a source change; remove this mark once it is.
pytestmark = [pytest.mark.unit, pytest.mark.serial]


def test_origin_of_keeps_origin_and_drops_proxy_path():
    assert origin_of(
        'https://abc-vscode.example.com/proxy/15000/') == \
        'https://abc-vscode.example.com'
    assert origin_of('javascript:alert(1)') == ''


def _start_relay():
    relay = BrowserRelay(
        lambda: ['https://abc-vscode.example.com/proxy/15000/'],
        port_start=15280, port_end=15289)
    assert relay.start()
    return relay, 'http://127.0.0.1:%d' % relay.port


def test_loopback_http_rejects_an_unrelated_web_origin():
    relay, base = _start_relay()
    try:
        response = requests.get(base + '/v1/status', timeout=2,
                                headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403
        ok = requests.get(
            base + '/v1/status', timeout=2,
            headers={'Origin': 'https://abc-vscode.example.com'})
        assert ok.status_code == 200
        assert ok.json()['kind'] == 'tofu-agent-browser-relay'
        assert ok.headers['Access-Control-Allow-Origin'] == \
            'https://abc-vscode.example.com'
    finally:
        relay.close()


def test_browser_round_trip_returns_response_shape_without_cookies():
    relay, base = _start_relay()
    origin = {'Origin': 'https://abc-vscode.example.com'}
    try:
        # The status probe marks the browser fresh, enabling the agent side.
        assert requests.get(base + '/v1/status', headers=origin,
                            timeout=2).ok
        box = {}

        def agent_request():
            box['response'] = relay.request(
                'https://abc-vscode.example.com/proxy/15000/api/desktop/poll',
                {'results': [], 'agent': {'agent_id': 'a'}},
                {'X-Bridge-Secret': 'tofu_live_test',
                 'Cookie': 'must-never-cross'}, timeout=3)

        thread = threading.Thread(target=agent_request)
        thread.start()
        take = requests.get(base + '/v1/take', headers=origin, timeout=2)
        assert take.status_code == 200
        job = take.json()
        assert job['headers'] == {'X-Bridge-Secret': 'tofu_live_test'}
        assert 'Cookie' not in json.dumps(job)
        returned = requests.post(
            base + '/v1/result', headers={**origin,
                                          'Content-Type': 'application/json'},
            json={'id': job['id'], 'status': 200,
                  'body': '{"commands":[]}'}, timeout=2)
        assert returned.status_code == 200
        thread.join(timeout=2)
        assert not thread.is_alive()
        response = box['response']
        assert response.status_code == 200
        assert response.json() == {'commands': []}
    finally:
        relay.close()


def test_dynamic_origin_gate_follows_reconnect():
    urls = ['https://one.example/proxy/15000']
    relay = BrowserRelay(lambda: urls, port_start=15380, port_end=15389)
    assert relay.start()
    base = 'http://127.0.0.1:%d/v1/status' % relay.port
    try:
        assert requests.get(base, headers={'Origin': 'https://one.example'},
                            timeout=2).status_code == 200
        urls[:] = ['https://two.example/proxy/15000']
        # No service restart: the callback is evaluated per request.
        assert requests.get(base, headers={'Origin': 'https://one.example'},
                            timeout=2).status_code == 403
        assert requests.get(base, headers={'Origin': 'https://two.example'},
                            timeout=2).status_code == 200
    finally:
        relay.close()


def test_timed_out_job_cannot_be_completed_late():
    relay, _base = _start_relay()
    try:
        relay.note_browser()
        assert relay.request('https://abc-vscode.example.com/x', {}, {},
                             timeout=0.05) is None
        # Drain the stale queue id; it is skipped instead of leaking to page.
        started = time.monotonic()
        assert relay.take(timeout=0.08) is None
        assert time.monotonic() - started < 0.3
    finally:
        relay.close()


def test_no_origin_can_push_an_attachment():
    """Joining is pairing (lib/desktop_agent/attachments.py): the broker has
    no unauthenticated attach verb, and a foreign origin is refused before
    any route is looked up."""
    relay, base = _start_relay()
    try:
        foreign = requests.post(base + '/v1/attach', timeout=2, data='{}',
                                headers={'Origin': 'https://evil.example',
                                         'Content-Type': 'application/json'})
        assert foreign.status_code == 403
        own = requests.post(base + '/v1/attach', timeout=2, data='{}',
                            headers={'Origin': 'https://abc-vscode.example.com',
                                     'Content-Type': 'application/json'})
        assert own.status_code == 404
        assert 'attached' not in requests.get(
            base + '/v1/status', timeout=2,
            headers={'Origin': 'https://abc-vscode.example.com'}).json()
    finally:
        relay.close()


def test_run_agent_prefers_live_browser_transport(monkeypatch, tmp_path):
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))

    class FakeRelay:
        def browser_available(self):
            return True

        def request(self, url, payload, headers, timeout=0):
            assert url.endswith('/api/desktop/poll')
            assert headers['X-Bridge-Secret'] == 'secret'
            return RelayResponse(200, '{"commands":[]}')

    monkeypatch.setattr(
        _run.requests.Session, 'post',
        lambda *a, **k: pytest.fail('direct request used despite live browser'))
    stop = threading.Event()
    seen = []

    def status(item):
        seen.append(item)
        stop.set()

    _run.run_agent('https://abc.example/proxy/15000', {},
                   poll_interval=0.001, bridge_secret='secret',
                   stop_event=stop, on_status=status,
                   browser_relay=FakeRelay())
    assert seen == [{'state': 'ok', 'transport': 'browser'}]


def test_gateway_401_does_not_drop_queued_command_results(
        monkeypatch, tmp_path):
    """The browser handoff often starts after one gateway 401.  Results
    offered on that rejected poll must be offered again, not discarded."""
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    sent = []
    responses = [
        RelayResponse(200, json.dumps({'commands': [
            {'id': 'cmd-1', 'type': 'definitely_unknown', 'params': {}}
        ]})),
        RelayResponse(401, '{"error":"Unauthorized"}'),
        RelayResponse(200, '{"commands":[]}'),
    ]

    def post(_session, _url, **kwargs):
        sent.append(kwargs['json'])
        assert responses, transitions
        return responses.pop(0)

    monkeypatch.setattr(_run.requests.Session, 'post', post)
    monkeypatch.setattr(_run.time, 'sleep', lambda _seconds: None)
    stop = threading.Event()
    transitions = []

    def status(item):
        transitions.append(item['state'])
        if len(transitions) >= 3:
            stop.set()

    _run.run_agent('https://abc.example/proxy/15000', {},
                   poll_interval=0.001, bridge_secret='secret',
                   stop_event=stop, on_status=status)
    assert transitions == ['ok', 'proxy', 'ok']
    assert len(sent[1]['results']) == 1
    assert sent[2]['results'] == sent[1]['results']


def test_relay_burst_exceeds_old_two_job_cap_and_close_wakes_owned_waiters():
    import time
    relay = BrowserRelay(lambda: ['https://fixture.test'])
    relay.note_browser()
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(
        relay.request('https://fixture.test/api/desktop/poll', {}, {}, timeout=30)))
        for _ in range(24)]
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 3
        while relay.pending_count() != len(threads):
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert len(relay._pending) == 24
        relay.close()
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive()
        assert responses == [None] * 24
        assert not relay._pending and relay.pending_count() == 0
        assert relay.take(timeout=30) is None
    finally:
        relay.close()
        for thread in threads:
            thread.join(1)


def test_relay_timeout_removes_unclaimed_job_from_handoff_fifo():
    relay = BrowserRelay(lambda: ['https://fixture.test'])
    relay.note_browser()
    try:
        assert relay.request('https://fixture.test/api/desktop/poll', {}, {}, timeout=.1) is None
        assert not relay._pending and relay.pending_count() == 0
    finally:
        relay.close()
