"""The messaging platform lifecycle contract (docs/CHANNELS.md, "Messaging platform lifecycle").

One contract for every platform (大象, 企业微信, QQ, 微信, 飞书): a connection another
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
from tests.channels.test_dx_adapter import FakeBridge, FlakyBridge, SESSIONS_ABORTED

pytestmark = pytest.mark.unit

BOTS = ('wecom', 'qq', 'wechat')
OLD_GROUP = ThreadKey.parse('dx:gid:71158539432')
FRONTIER = 1_794_730_026_044


class ListingBridge(FakeBridge):
    """Sessions by listing window: the recent ones, and older groups only for a window before the frontier."""

    def __init__(self, recent, older):
        super().__init__(recent, {})
        self.older = older

    def request(self, op, **args):
        if op == 'sessions' and args['endMs'] <= FRONTIER:
            self.requests.append((op, args))
            return {'ok': True, 'result': {'sessions': self.older}}
        return super().request(op, **args)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    monkeypatch.setattr('lib.channels.bots.supervisor.notify_account_changed', lambda owner, channel: None)


def _taken_over(store, enabled):
    """This server enabled the account (frontier = enable time); the other server keeps its command chat."""
    enabled()
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': FRONTIER})


def _catch_up(store, bridge):
    from lib.channels.dx.adapter import DxAdapter
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter.catch_up()
    return adapter


# ── 大象: handover of the command chat another server created ──────────────

def test_a_taken_over_account_finds_the_other_servers_tofu_router_group(store, enabled, router, quiet):
    from lib.channels import service
    _taken_over(store, enabled)
    older = [{'type': 'group', 'id': '71158539432', 'name': 'Tofu Router', 'lastMessageServerTime': FRONTIER - 9},
             {'type': 'group', 'id': '600', 'name': 'Tofu Router', 'lastMessageServerTime': FRONTIER - 99},
             {'type': 'group', 'id': '700', 'name': 'Team', 'lastMessageServerTime': FRONTIER - 5}]
    bridge = ListingBridge([], older)
    _catch_up(store, bridge)
    # Only group sessions before the frontier are listed, for their names: nothing is replayed.
    listing = [args for op, args in bridge.requests if op == 'sessions' and args['endMs'] <= FRONTIER]
    assert listing == [{'startMs': 0, 'endMs': FRONTIER, 'limit': listing[0]['limit'], 'types': ['group']}]
    assert not any(op == 'history' for op, _ in bridge.requests)
    view = service.account_view(OWNER, store=store)
    assert view['commandChat'] is None and view['commandChatCandidate'] == str(OLD_GROUP)
    assert store.thread(OLD_GROUP)['title'] == 'Tofu Router'
    assert store.thread(ThreadKey.parse('dx:gid:700')) is None

    # One owner action adopts it; no second group is created.
    view = service.update_account(OWNER, {'commandChat': str(OLD_GROUP)}, store=store)
    assert view['commandChat'] == str(OLD_GROUP) and view['commandChatCandidate'] is None
    assert store.account('dx').get('commandChatCreation') is None


def test_the_handover_listing_runs_once_per_session_and_never_fails_the_catch_up(store, enabled, router):
    _taken_over(store, enabled)
    bridge = FlakyBridge([], {}, {})
    calls = []

    def request(op, **args):
        calls.append(args)
        if args.get('types') == ['group']:
            return dict(SESSIONS_ABORTED)
        return {'ok': True, 'result': {'sessions': []}}
    bridge.request = request
    adapter = _catch_up(store, bridge)
    assert not adapter._catch_up_owed and store.account('dx')['frontierAtMs'] > FRONTIER
    adapter.catch_up()
    assert len([args for args in calls if args.get('types') == ['group']]) == 1
    assert store.account('dx').get('commandChatCandidate') is None


def test_an_account_with_a_command_chat_lists_nothing_before_its_frontier(store, enabled, router):
    enabled(command_chat='dx:gid:900')
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': FRONTIER})
    bridge = ListingBridge([], [{'type': 'group', 'id': '1', 'name': 'Tofu Router'}])
    _catch_up(store, bridge)
    assert not any(args['endMs'] <= FRONTIER for op, args in bridge.requests if op == 'sessions')


def test_a_recent_group_named_tofu_router_is_offered_too_and_a_switched_account_drops_it(
        store, enabled, router, quiet):
    from lib.channels import service
    _taken_over(store, enabled)
    recent = [{'type': 'group', 'id': '71158539432', 'name': 'Tofu Router', 'lastMessageServerTime': FRONTIER + 5}]
    _catch_up(store, ListingBridge(recent, []))
    assert service.account_view(OWNER, store=store)['commandChatCandidate'] == str(OLD_GROUP)
    view = service.update_account(OWNER, {'mis': 'someone-else'}, store=store)
    assert view['commandChatCandidate'] is None


def test_the_account_view_with_a_candidate_matches_the_contract(store, enabled, router):
    jsonschema = pytest.importorskip('jsonschema')
    from lib.channels import service
    from lib.channels.model import CONTRACT
    _taken_over(store, enabled)
    store.update_account('dx', lambda account: {**account, 'commandChatCandidate': str(OLD_GROUP)})
    schema = {**CONTRACT['$defs']['ChannelAccountView'], '$defs': CONTRACT['$defs']}
    jsonschema.validate(service.account_view(OWNER, store=store), schema)


# ── 大象 notification robot: stale credentials, missing entry, partial save ──

def test_a_robot_whose_vault_entry_is_gone_reports_it_instead_of_configured(store, enabled, router, vault, quiet):
    from lib.channels import service
    enabled(command_chat='dx:gid:900')
    service.update_account(OWNER, {'robot': {'clientId': 'client-1', 'secret': 'fake-secret-1'}}, store=store,
                           robot_probe=lambda client_id, secret: None)
    assert service.account_view(OWNER, store=store)['robot']['error'] is None
    vault.delete_entry(f'channel.u{OWNER}.dx.robot')
    error = service.account_view(OWNER, store=store)['robot']['error']
    assert error['code'] == 'robot_credential_missing' and f'channel.u{OWNER}.dx.robot' in error['detail']


def test_a_refused_robot_secret_settles_as_a_typed_error_and_stores_nothing(store, enabled, router, vault, quiet):
    from lib.channels import service
    enabled(command_chat='dx:gid:900')

    def stale(client_id, secret):
        raise ChannelError('token_rejected', 'The token endpoint refused the client credentials')
    with pytest.raises(ChannelError) as refused:
        service.update_account(OWNER, {'robot': {'clientId': 'client-1', 'secret': 'old'}}, store=store,
                               robot_probe=stale)
    assert refused.value.code == 'token_rejected'
    assert not store.account('dx').get('robot') and vault.get_entry(f'channel.u{OWNER}.dx.robot') is None


def test_a_robot_save_that_fails_partway_keeps_the_secret_the_account_names(
        store, enabled, router, vault, quiet, monkeypatch):
    from lib.channels import service
    enabled(command_chat='dx:gid:900')
    own = f'channel.u{OWNER}.dx.robot'
    service.update_account(OWNER, {'robot': {'clientId': 'client-1', 'secret': 'pasted'}}, store=store,
                           robot_probe=lambda client_id, secret: None)
    vault.set_entry('team-robot', 'shared')
    commit = store.update_account

    def outage(channel, change):
        raise StorageError('database_timeout', 'storage did not answer')
    monkeypatch.setattr(store, 'update_account', outage)
    with pytest.raises(StorageError):
        service.update_account(OWNER, {'robot': {'clientId': 'client-1', 'credentialName': 'team-robot'}},
                               store=store, robot_probe=lambda client_id, secret: None)
    # The account still names the pasted entry, which still exists; a retry then succeeds.
    assert store.account('dx')['robot']['credential']['name'] == own and vault.get_entry(own) == 'pasted'
    monkeypatch.setattr(store, 'update_account', commit)
    view = service.update_account(OWNER, {'robot': {'clientId': 'client-1', 'credentialName': 'team-robot'}},
                                  store=store, robot_probe=lambda client_id, secret: None)
    assert view['robot']['credentialName'] == 'team-robot' and vault.get_entry(own) is None


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
