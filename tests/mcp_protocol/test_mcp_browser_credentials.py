"""tests/mcp_protocol/test_mcp_browser_credentials.py — browser-held MCP credentials.

The Overleaf MCP server authenticates with a copy of the browser's
``overleaf_session2`` cookie (``OVERLEAF_SESSION``). The copy expires while
the browser keeps a live session. These specs pin the renewal contract:

  * the catalog declares which browser cookie backs ``OVERLEAF_SESSION``;
  * ``read_browser_env`` reads exactly that cookie, for the URL the server
    talks to, from the owner's most recently polling extension;
  * a probe that proves the live credential expired copies the browser value
    into the stored configuration and reconnects with it;
  * a call to a server whose credential is known expired (live or parked)
    renews first and runs on the renewed connection;
  * every reconnect uses the owner's stored row, so a credential saved by
    Settings or another process reaches this process;
  * a known-good credential never consults the browser, and no value ever
    reaches a log line or an audit record.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/mcp_protocol/test_mcp_browser_credentials.py -m unit
"""
from __future__ import annotations

import copy
import logging
import time
from urllib.parse import urlparse

import pytest

import lib.browser.access as browser_access
import lib.browser.queue as browser_queue
import lib.mcp.config as mcp_config
from lib.mcp.browser_credentials import read_browser_env
from lib.mcp.client import _bridge as bridge_module
from lib.mcp.registry import get_catalog_entry

pytestmark = pytest.mark.unit

OWNER = 7
STALE = 'stale-session-value-0000'
FRESH = 'fresh-session-value-1111'
GIT_TOKEN = 'git-token-value-2222'
EXPIRED_TEXT = ('Error fetching projects: Not authenticated — check '
                'OVERLEAF_SESSION cookie')
OK_TEXT = 'Your Overleaf projects (1):\n\n  • Thesis  [abc123]\n'
PROBE_TOOL = 'mcp__overleaf__list_projects'
OTHER_TOOL = 'mcp__overleaf__read_file'


def _row(session, **extra):
    return {
        'command': 'tofu-overleaf-mcp', 'args': [], 'transport': 'stdio',
        'env': {'OVERLEAF_SESSION': session, 'OVERLEAF_GIT_TOKEN': GIT_TOKEN,
                **extra},
    }


class _Browser:
    """One owner's extension devices; cookies follow Chrome's URL matching."""

    def __init__(self):
        self.clients = [
            {'client_id': 'older-device', 'last_poll': 1.0},
            {'client_id': 'live-device', 'last_poll': 2.0},
        ]
        self.cookies = [{'name': 'overleaf_session2', 'value': FRESH,
                         'domain': '.overleaf.com', 'path': '/',
                         'httpOnly': True}]
        self.error = None
        self.commands = []

    def connected(self, *, owner_user_id):
        assert owner_user_id == str(OWNER)
        return [dict(row) for row in self.clients]

    def send(self, cmd_type, params=None, timeout=30, *, client_id,
             owner_user_id, cancel_event=None):
        params = dict(params or {})
        self.commands.append({'type': cmd_type, 'params': params,
                              'client_id': client_id, 'owner': owner_user_id})
        if self.error:
            return None, self.error
        host = urlparse(params['url']).hostname or ''
        return [dict(cookie) for cookie in self.cookies
                if cookie['name'] == params.get('name')
                and ('.' + host).endswith(cookie['domain'])], None


@pytest.fixture
def browser(monkeypatch):
    fake = _Browser()
    monkeypatch.setattr(browser_queue, 'get_connected_clients', fake.connected)
    monkeypatch.setattr(browser_queue, 'send_browser_command', fake.send)
    monkeypatch.setattr(browser_access, 'is_read_allowed',
                        lambda user_id, url: True)
    return fake


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_config, '_CONFIG_DIR', str(tmp_path))
    mcp_config.upsert_server('overleaf', _row(STALE), user_id=OWNER)
    return tmp_path


@pytest.fixture
def audits(monkeypatch):
    events = []
    monkeypatch.setattr(bridge_module, 'audit_log',
                        lambda event, **fields: events.append((event, fields)))
    return events


def _stored_env():
    return mcp_config.load_mcp_config(user_id=OWNER)['overleaf']['env']


def _tool_row(namespaced, tool):
    return {
        'server_name': 'overleaf', 'tool_name': tool,
        'namespaced_name': namespaced, 'description': '',
        'input_schema': {'type': 'object', 'properties': {}},
        'openai_def': {}, 'read_only_hint': True, 'meta': {},
        'schema_hash': 'stable', 'catalog_version': 'v1',
    }


def _overleaf_bridge(live_session, *, owner=OWNER, parked=False,
                     verdict=None):
    """A bridge whose live 'overleaf' answers by the session it was started with."""
    bridge = bridge_module.MCPBridge(owner_user_id=owner)
    config = _row(live_session)
    handle = bridge_module._MCPServerHandle('overleaf', copy.deepcopy(config))
    handle.session = None if parked else object()
    bridge._servers['overleaf'] = handle
    bridge._configs['overleaf'] = copy.deepcopy(config)
    bridge._started = True
    if parked:
        bridge._parked.add('overleaf')
    if verdict:
        bridge._cred_health['overleaf'] = {
            'status': verdict, 'checked_at': time.time(), 'detail': ''}
    bridge._tool_index[PROBE_TOOL] = _tool_row(PROBE_TOOL, 'list_projects')
    bridge._tool_index[OTHER_TOOL] = _tool_row(OTHER_TOOL, 'read_file')
    connects = []

    def _connect(name, srv_cfg):
        # The real connect_server tears the old transport down first: a live
        # server loses its credential verdict, a parked one keeps it.
        connects.append(copy.deepcopy(srv_cfg))
        fresh = bridge_module._MCPServerHandle(name, copy.deepcopy(srv_cfg))
        fresh.session = object()
        with bridge._lock:
            if name not in bridge._parked:
                bridge._cred_health.pop(name, None)
            bridge._servers[name] = fresh
            bridge._configs[name] = copy.deepcopy(srv_cfg)
            bridge._parked.discard(name)
        return []

    def _run_async(coro, timeout=None, **_kwargs):
        coro.close()
        session = bridge._servers['overleaf'].config['env'].get('OVERLEAF_SESSION')
        return OK_TEXT if session == FRESH else EXPIRED_TEXT

    bridge.connect_server = _connect
    bridge._run_async = _run_async
    return bridge, connects


def _assert_no_secret(audits, caplog):
    text = caplog.text + repr(audits)
    for secret in (STALE, FRESH, GIT_TOKEN):
        assert secret not in text


# ── Catalog declaration ──────────────────────────────────

def test_overleaf_session_declares_its_browser_cookie():
    specs = {spec['key']: spec for spec in get_catalog_entry('overleaf')['env_specs']}
    assert specs['OVERLEAF_SESSION']['browser_cookie'] == {
        'name': 'overleaf_session2',
        'url': 'https://www.overleaf.com',
        'url_env': 'OVERLEAF_BASE_URL',
    }
    assert 'browser_cookie' not in specs['OVERLEAF_GIT_TOKEN']


# ── Reading one cookie from the owner's browser ──────────

def test_reads_exactly_the_declared_cookie_from_the_latest_device(browser):
    assert read_browser_env('overleaf', _row(STALE), owner_user_id=OWNER) == {
        'OVERLEAF_SESSION': FRESH}
    assert browser.commands == [{
        'type': 'get_cookies',
        'params': {'url': 'https://www.overleaf.com', 'name': 'overleaf_session2'},
        'client_id': 'live-device', 'owner': str(OWNER)}]


def test_a_self_hosted_base_url_is_asked_for_its_own_cookie(browser):
    row = _row(STALE, OVERLEAF_BASE_URL='https://latex.example.edu')
    assert read_browser_env('overleaf', row, owner_user_id=OWNER) == {}
    assert browser.commands[0]['params']['url'] == 'https://latex.example.edu'


def test_servers_without_a_browser_source_never_reach_the_browser(browser):
    assert read_browser_env('my-custom', _row(STALE), owner_user_id=OWNER) == {}
    assert browser.commands == []


@pytest.mark.parametrize('case', [
    'no_device', 'read_denied', 'command_error', 'no_cookie', 'control_chars'])
def test_unusable_browser_state_yields_no_value(browser, monkeypatch, case):
    if case == 'no_device':
        browser.clients = []
    elif case == 'read_denied':
        monkeypatch.setattr(browser_access, 'is_read_allowed',
                            lambda user_id, url: False)
    elif case == 'command_error':
        browser.error = "Browser command 'get_cookies' timed out after 10s."
    elif case == 'no_cookie':
        browser.cookies = []
    else:
        browser.cookies[0]['value'] = 'abc\r\nInjected: header'
    assert read_browser_env('overleaf', _row(STALE), owner_user_id=OWNER) == {}
    if case in ('no_device', 'read_denied'):
        assert browser.commands == []


def test_an_unreachable_bridge_owner_yields_no_value(monkeypatch):
    from lib.browser.bridge_relay import RelayUnavailable

    def _unreachable(*, owner_user_id):
        raise RelayUnavailable('The browser bridge relay record is unreadable')

    monkeypatch.setattr(browser_queue, 'get_connected_clients', _unreachable)
    assert read_browser_env('overleaf', _row(STALE), owner_user_id=OWNER) == {}


# ── Renewal after a probe proves the credential expired ──

def test_expired_probe_copies_the_browser_cookie_and_reconnects(
        store, browser, audits, caplog):
    caplog.set_level(logging.DEBUG)
    bridge, connects = _overleaf_bridge(STALE)

    record = bridge._run_cred_probe('overleaf')

    assert record['status'] == 'ok'
    assert bridge.get_cred_health('overleaf')['status'] == 'ok'
    assert _stored_env() == {'OVERLEAF_SESSION': FRESH,
                             'OVERLEAF_GIT_TOKEN': GIT_TOKEN}
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert [event for event, _ in audits] == [
        'mcp_credential_expired', 'mcp_credential_refreshed', 'mcp_reconnect']
    assert audits[1][1] == {'server': 'overleaf', 'env_keys': ['OVERLEAF_SESSION'],
                            'source': 'browser_cookie'}
    _assert_no_secret(audits, caplog)


def test_expired_probe_without_a_newer_credential_changes_nothing(
        store, browser, audits):
    browser.cookies[0]['value'] = STALE
    before = (store / 'mcp_servers' / f'{OWNER}.json').read_bytes()
    bridge, connects = _overleaf_bridge(STALE)

    record = bridge._run_cred_probe('overleaf')

    assert record['status'] == 'expired'
    assert connects == []
    assert (store / 'mcp_servers' / f'{OWNER}.json').read_bytes() == before
    assert [event for event, _ in audits] == ['mcp_credential_expired']
    assert len(browser.commands) == 1


def test_a_credential_saved_elsewhere_renews_without_the_browser(
        store, browser, audits):
    browser.clients = []
    mcp_config.upsert_server('overleaf', _row(FRESH), user_id=OWNER)
    bridge, connects = _overleaf_bridge(STALE)

    record = bridge._run_cred_probe('overleaf')

    assert record['status'] == 'ok'
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert [event for event, _ in audits] == [
        'mcp_credential_expired', 'mcp_reconnect']


def test_a_disabled_stored_server_is_not_renewed(store, browser):
    mcp_config.patch_server('overleaf', {'enabled': False}, user_id=OWNER)
    bridge, connects = _overleaf_bridge(STALE)

    assert bridge._run_cred_probe('overleaf')['status'] == 'expired'
    assert connects == []


def test_a_failed_renewal_reconnect_keeps_the_expired_verdict(store, browser):
    bridge, _ = _overleaf_bridge(STALE)

    def _broken_connect(name, srv_cfg):
        raise RuntimeError('server failed to start')

    bridge.connect_server = _broken_connect

    assert bridge._run_cred_probe('overleaf')['status'] == 'expired'
    assert _stored_env()['OVERLEAF_SESSION'] == FRESH


# ── Renewal before a call ────────────────────────────────

@pytest.mark.parametrize('parked', [False, True])
def test_a_known_expired_server_renews_before_the_call(
        store, browser, audits, caplog, parked):
    caplog.set_level(logging.DEBUG)
    bridge, connects = _overleaf_bridge(STALE, parked=parked, verdict='expired')

    assert bridge.call_tool(OTHER_TOOL, {}) == OK_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert _stored_env()['OVERLEAF_SESSION'] == FRESH
    assert 'overleaf' not in bridge._parked
    _assert_no_secret(audits, caplog)


def test_a_known_good_credential_never_consults_the_browser(store, browser):
    bridge, connects = _overleaf_bridge(STALE, verdict='ok')

    assert bridge.call_tool(OTHER_TOOL, {}) == EXPIRED_TEXT

    assert connects == []
    assert browser.commands == []


def test_waking_a_parked_server_uses_the_stored_credential(store, browser):
    mcp_config.upsert_server('overleaf', _row(FRESH), user_id=OWNER)
    bridge, connects = _overleaf_bridge(STALE, parked=True)

    assert bridge.call_tool(OTHER_TOOL, {}) == OK_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert browser.commands == []


def test_an_unowned_bridge_wakes_with_its_last_known_config(browser):
    bridge, connects = _overleaf_bridge(STALE, owner=None, parked=True,
                                        verdict='expired')

    assert bridge.call_tool(OTHER_TOOL, {}) == EXPIRED_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [STALE]
    assert browser.commands == []
