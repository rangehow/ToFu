"""The messaging platform lifecycle contract (docs/CHANNELS.md, "Messaging platform lifecycle").

One contract for every platform (企业微信, QQ, 微信, 飞书 here; 大象's half lives with its
adapter in bundles/yourprovider/tests/channels/test_dx_messaging_lifecycle.py): a connection another
server or client took over is reported as such and taken back with one owner
action; a refused credential and a missing vault entry report their real reason;
a save that fails partway never leaves the account naming a deleted secret; and a
server that takes over an account another server served (2026-11-15, the owner's
"zw" server) finds that server's command chat instead of creating a second one.
Fakes only: no platform endpoint is called.
"""
from __future__ import annotations

import sys
import types

import pytest

from lib.channels.model import ChannelError, ThreadKey
from lib.storage.errors import StorageError
from tests.channels.conftest import OWNER

pytestmark = pytest.mark.unit

BOTS = ('wecom', 'qq', 'wechat')


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr('lib.channels.bots.supervisor.notify_account_changed', lambda owner, channel: None)


# ── bot platforms: the same contract per channel ──────────────────────────

class FakeSession:
    made: list = []

    def __init__(self, owner, channel, *, store=None):
        from lib.channels.bots.session import account_binding
        self.binding = account_binding(store.account(channel) or {})
        self._alive = True
        FakeSession.made.append(self)

    def start(self):
        pass

    def alive(self):
        return self._alive

    def tick(self):
        pass

    def stop(self, timeout):
        self._alive = False
        return True


def _bot(store, vault, channel):
    name = f'channel.u{OWNER}.{channel}.' + ('token' if channel == 'wechat' else 'secret')
    vault.set_entry(name, 'fake-secret')
    store.update_account(channel, lambda account: {
        **account, 'enabled': True, 'appId': None if channel == 'wechat' else 'bot-1', 'frontierAtMs': 1,
        'credential': {'name': name, 'managed': True}})
    return name


def _reconcile(store, channel):
    from lib.channels.bots import supervisor
    return supervisor.reconcile_owner(OWNER, channel, store=store, session_factory=FakeSession)


@pytest.mark.parametrize('channel', BOTS)
def test_a_connection_taken_over_elsewhere_stays_down_until_one_reconnect_takes_it_back(
        channel, store, router, vault, quiet):
    from lib.channels.bots import account, supervisor
    FakeSession.made = []
    _bot(store, vault, channel)
    try:
        assert _reconcile(store, channel) == 'started'
        # The platform ended the session because another server's connection took the bot (SessionEnded).
        store.set_status(channel, 'kicked', 'Another connection of this bot took over')
        FakeSession.made[-1]._alive = False
        assert _reconcile(store, channel) == 'kicked'
        assert _reconcile(store, channel) == 'kicked'  # no restart war with the other server
        view = account.account_view(OWNER, channel, store=store)
        assert view['status'] == {**view['status'], 'state': 'kicked',
                                  'detail': 'Another connection of this bot took over'}
        view = account.update_account(OWNER, channel, {'enabled': True}, store=store)
        assert view['status']['state'] == 'starting'
        assert _reconcile(store, channel) == 'started'
    finally:
        supervisor.stop_bot_sessions()


@pytest.mark.parametrize('channel', BOTS)
def test_a_refused_credential_stays_down_with_its_reason(channel, store, router, vault, quiet):
    from lib.channels.bots import supervisor
    FakeSession.made = []
    _bot(store, vault, channel)
    try:
        assert _reconcile(store, channel) == 'started'
        store.set_status(channel, 'needs_login', 'The platform refused the credentials')
        FakeSession.made[-1]._alive = False
        assert _reconcile(store, channel) == 'needs_login'
        assert store.account(channel)['status']['detail'] == 'The platform refused the credentials'
    finally:
        supervisor.stop_bot_sessions()


@pytest.mark.parametrize('channel', BOTS)
def test_a_missing_vault_entry_is_named_in_the_status(channel, store, router, vault, quiet):
    from lib.channels.bots import supervisor
    name = _bot(store, vault, channel)
    vault.delete_entry(name)
    try:
        assert _reconcile(store, channel) == 'needs_login'
        status = store.account(channel)['status']
        assert status['state'] == 'needs_login' and name in status['detail'] and 'missing' in status['detail']
    finally:
        supervisor.stop_bot_sessions()


@pytest.mark.parametrize('channel', BOTS)
def test_a_save_that_fails_partway_never_deletes_the_secret_the_account_names(
        channel, store, router, vault, quiet, monkeypatch):
    from lib.channels.bots import account
    name = _bot(store, vault, channel)
    commit = store.update_account

    def outage(channel_name, change):
        raise StorageError('database_timeout', 'storage did not answer')
    monkeypatch.setattr(store, 'update_account', outage)
    with pytest.raises(StorageError):
        account.update_account(OWNER, channel, {'credentialName': None}, store=store)
    assert store.account(channel)['credential']['name'] == name and vault.get_entry(name) == 'fake-secret'
    monkeypatch.setattr(store, 'update_account', commit)
    view = account.update_account(OWNER, channel, {'credentialName': None}, store=store)
    assert view['credential'] is None and vault.get_entry(name) is None


# ── 飞书: the long connection reports its real reason ────────────────────

@pytest.fixture
def feishu(monkeypatch):
    import lib.feishu._state as state
    import lib.feishu.startup as startup
    monkeypatch.setattr(state, 'APP_ID', 'cli_fake')
    monkeypatch.setattr(state, 'APP_SECRET', 'fake-secret')
    monkeypatch.setattr(state, 'ENABLED', True)
    monkeypatch.setattr(startup, '_bot_thread', None)
    monkeypatch.setattr(startup, '_connection', {'state': 'disabled', 'detail': ''})
    # The reconnect loop waits on the shared route authority: give it a
    # reachable fake route on a clock the test advances (``startup.test_clock``).
    import lib.network_routes as network_routes
    clock = [0.0]
    monkeypatch.setattr(network_routes, 'manager', network_routes.RouteManager(
        probe=lambda *_: network_routes.ProbeResult('ok', 1.0), clock=lambda: clock[0]))
    monkeypatch.setattr(startup, '_session_route', lambda: network_routes.Route('direct', 'direct', 'direct'))
    monkeypatch.setattr(startup, 'test_clock', clock, raising=False)
    return startup


def _join(startup):
    startup._bot_thread.join(5)
    assert not startup._bot_thread.is_alive()


def test_feishu_without_its_sdk_says_so(feishu, monkeypatch):
    monkeypatch.setitem(sys.modules, 'lark_oapi', None)
    assert feishu.start_bot()
    _join(feishu)
    status = feishu.connection_status()
    assert status['state'] == 'runtime_unavailable' and 'lark_oapi' in status['detail']


def test_feishu_refused_credentials_report_the_platforms_refusal(feishu, monkeypatch):
    class Refusing:
        @staticmethod
        def builder(app_id, app_secret):
            return Refusing()

        def event_handler(self, handler):
            return self

        def log_level(self, level):
            return self

        def build(self):
            return self

        calls = 0

        def start(self):
            Refusing.calls += 1
            if Refusing.calls > 1:
                raise KeyboardInterrupt  # the loop's own clean exit, so the test thread ends
            raise RuntimeError('app_id or app_secret is invalid (code 10014)')

    class Dispatcher:
        @staticmethod
        def builder(*args):
            return Dispatcher()

        def __getattr__(self, name):
            return lambda *args: self

    lark = types.SimpleNamespace(EventDispatcherHandler=Dispatcher, LogLevel=types.SimpleNamespace(INFO=1))
    monkeypatch.setitem(sys.modules, 'lark_oapi', lark)
    monkeypatch.setitem(sys.modules, 'lark_oapi.adapter', types.ModuleType('lark_oapi.adapter'))
    monkeypatch.setitem(sys.modules, 'lark_oapi.adapter.websocket', types.SimpleNamespace(WebSocket=Refusing))
    monkeypatch.setattr(feishu, '_patch_websockets_ping_settings', lambda: None)

    while_retrying = []

    def sleep(seconds):
        while_retrying.append(feishu.connection_status())
        feishu.test_clock[0] += seconds  # the wait the shared route circuit asked for

    monkeypatch.setattr(feishu.time, 'sleep', sleep)
    assert feishu.start_bot()
    _join(feishu)
    # What Settings reads while the loop waits to retry: the platform's own refusal.
    assert while_retrying[0]['state'] == 'disconnected' and '10014' in while_retrying[0]['detail']
    assert feishu.connection_status()['state'] == 'disconnected'


def test_feishu_unconfigured_is_disabled(feishu, monkeypatch):
    import lib.feishu._state as state
    monkeypatch.setattr(state, 'ENABLED', False)
    assert feishu.start_bot() is False
    assert feishu.connection_status() == {'state': 'disabled', 'detail': ''}
