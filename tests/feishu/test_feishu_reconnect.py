"""Feishu's reconnect loop waits on the shared network authority (docs/NETWORK.md).

The fake SDK client stands in for ``lark_oapi``'s WebSocket client; the shared
``RouteManager`` runs with a fake probe and a fake clock that the loop's sleep
advances, so every wait the loop takes is the shared route circuit's.
"""
from __future__ import annotations

import sys
import types

import pytest
import requests

import lib.feishu.startup as startup
import lib.network_routes as network_routes
from lib.network_routes import ProbeResult, Route, RouteManager

pytestmark = pytest.mark.unit

DIRECT = Route('direct', 'direct', 'direct')


class _Stop(Exception):
    pass


class FakeSession:
    """Scripted ``start()`` outcomes: an exception to raise, a callable to run, or None (clean end)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.starts = 0

    def start(self):
        self.starts += 1
        if not self.outcomes:
            raise KeyboardInterrupt
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome()


@pytest.fixture
def harness(monkeypatch):
    now = [1000.0]
    probes = []
    verdict = ['ok']

    def probe(url, route):
        probes.append(now[0])
        return ProbeResult(verdict[0], 1.0)

    routes = RouteManager(probe=probe, clock=lambda: now[0], jitter=lambda seconds: seconds)
    waits = []
    kinds = []

    def sleep(seconds):
        waits.append(round(seconds, 3))
        kinds.append(_route_state(routes)['failure_kind'])
        if len(waits) >= 12:
            raise _Stop
        now[0] += seconds

    monkeypatch.setattr(startup, '_session_route', lambda: DIRECT)
    monkeypatch.setattr(startup, '_connection', {'state': 'disabled', 'detail': ''})
    yield types.SimpleNamespace(routes=routes, waits=waits, kinds=kinds, probes=probes, verdict=verdict,
                                run=lambda session: startup._connection_loop(session, routes=routes, sleep=sleep))
    routes.close()


def _route_state(routes):
    return routes.status()['routes'][network_routes.origin_key(startup.FEISHU_OPEN_PLATFORM_URL)]['direct']


def test_repeated_session_failures_wait_the_shared_circuit_schedule(harness):
    session = FakeSession([RuntimeError('app_id or app_secret is invalid (code 10014)')] * 6)
    harness.run(session)
    assert session.starts == 7
    # The waits are exactly the shared authority's escalating circuit, nothing local.
    schedule = list(network_routes._NETWORK_BACKOFF_S)
    assert harness.waits == schedule + [schedule[-1]]
    assert set(harness.kinds) == {'session_failed'}


def test_a_delivered_event_forgives_the_backoff_debt(harness):
    failure = RuntimeError('connection lost')
    session = FakeSession([failure, failure, startup._event_delivered, failure])
    harness.run(session)
    first, second = network_routes._NETWORK_BACKOFF_S[:2]
    # Third session delivered an event, then ended: the next wait restarts the schedule.
    assert harness.waits == [first, second, first, second]


def test_connection_setup_failure_is_reported_as_network_failure(harness):
    session = FakeSession([requests.exceptions.ConnectTimeout('connect timed out')])
    harness.run(session)
    assert harness.waits == [network_routes._NETWORK_BACKOFF_S[0]]
    assert harness.kinds == ['network_fail']


def test_unreachable_route_never_starts_the_sdk(harness):
    harness.verdict[0] = 'network_fail'
    session = FakeSession([])

    with pytest.raises(_Stop):
        startup._connection_loop(session, routes=harness.routes,
                                 sleep=lambda seconds: (_ for _ in ()).throw(_Stop))
    assert session.starts == 0
    status = startup._connection
    assert status['state'] == 'disconnected' and 'network route' in status['detail']


def test_unreachable_route_waits_for_recovery_then_connects(harness):
    harness.verdict[0] = 'network_fail'
    session = FakeSession([])
    original = harness.routes.candidates
    calls = [0]

    def candidates(url, routes, **kwargs):
        calls[0] += 1
        if calls[0] == 3:
            harness.verdict[0] = 'ok'
        return original(url, routes, **kwargs)

    harness.routes.candidates = candidates
    harness.run(session)
    assert session.starts == 1
    assert harness.waits == list(network_routes._NETWORK_BACKOFF_S[:2])
    assert len(harness.probes) == 3


def test_session_route_follows_the_process_proxy_environment(monkeypatch):
    for name in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    assert startup._session_route().mode == 'direct'
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:3128')
    route = startup._session_route()
    assert route.route_id.startswith('env:') and route.proxy_url == 'http://proxy.invalid:3128'


def test_start_bot_runs_the_shared_loop_and_reports_events(monkeypatch):
    import lib.feishu._state as state
    monkeypatch.setattr(state, 'APP_ID', 'cli_fake')
    monkeypatch.setattr(state, 'APP_SECRET', 'fake-secret')
    monkeypatch.setattr(state, 'ENABLED', True)
    monkeypatch.setattr(startup, '_bot_thread', None)
    monkeypatch.setattr(startup, '_patch_websockets_ping_settings', lambda: None)

    registered = {}

    class Dispatcher:
        @staticmethod
        def builder(*args):
            return Dispatcher()

        def register_p2_im_message_receive_v1(self, handler):
            registered['message'] = handler
            return self

        def register_p1_application_bot_menu_v6(self, handler):
            registered['menu'] = handler
            return self

        def build(self):
            return self

    class Client:
        @staticmethod
        def builder(app_id, app_secret):
            return Client()

        def event_handler(self, handler):
            return self

        def log_level(self, level):
            return self

        def build(self):
            return self

    lark = types.SimpleNamespace(EventDispatcherHandler=Dispatcher, LogLevel=types.SimpleNamespace(INFO=1))
    monkeypatch.setitem(sys.modules, 'lark_oapi', lark)
    monkeypatch.setitem(sys.modules, 'lark_oapi.adapter', types.ModuleType('lark_oapi.adapter'))
    monkeypatch.setitem(sys.modules, 'lark_oapi.adapter.websocket', types.SimpleNamespace(WebSocket=Client))
    handled = []
    import lib.feishu.events as events
    monkeypatch.setattr(events, 'handle_message_event', lambda data: handled.append(('message', data)))
    monkeypatch.setattr(events, 'handle_menu_event', lambda data: handled.append(('menu', data)))
    looped = []
    monkeypatch.setattr(startup, '_connection_loop', lambda client: looped.append(client))
    delivered = []
    monkeypatch.setattr(startup, '_event_delivered', lambda: delivered.append(1))

    assert startup.start_bot()
    startup._bot_thread.join(5)
    assert len(looped) == 1 and isinstance(looped[0], Client)
    registered['message']('m')
    registered['menu']('u')
    assert handled == [('message', 'm'), ('menu', 'u')] and delivered == [1, 1]
