"""tests/mcp_protocol/test_mcp_browser_credentials.py — browser-held MCP credentials.

Many MCP servers authenticate with a copy of the user's browser session: one
cookie's value (Overleaf ``OVERLEAF_SESSION``) or a whole Cookie header. These
specs pin the general contract (lib/mcp/browser_credentials.py and the MCP
bridge):

  * a curated catalog entry may declare its cookie; every other server learns
    which stored values copy browser cookies from an in-browser keyed-digest
    comparison, for single values and Cookie headers alike;
  * learning ignores values that cannot be session identifiers and cookies
    scripts may read unless they accompany an HttpOnly session cookie;
  * renewal reads the bound cookies from the device that answered, saves
    changed values atomically, and keeps the user's header order;
  * the bridge renews after an expired probe, after a call rejected the
    credential, before a call to a server known to be expired, and before
    reconnecting a server whose last connect failed; a read-only call is
    repeated once, any other gets guidance instead of a cookie request;
  * an env value that is a ``vault:<entry>`` reference is compared and renewed
    through that vault entry; the reference stays and no plaintext is written
    back to the config;
  * no value reaches a log line, an audit record or the browser bridge.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/mcp_protocol/test_mcp_browser_credentials.py -m unit
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import logging
import time
from urllib.parse import urlparse

import pytest

import lib.browser.access as browser_access
import lib.browser.queue as browser_queue
import lib.mcp.browser_credentials as browser_credentials
import lib.credentials_vault as credentials_vault
import lib.mcp.config as mcp_config
from lib.browser.dispatch import _clear_sticky_clients, select_owner_client
from lib.mcp.browser_credentials import (
    credential_bindings, learn_bindings, refresh_from_browser)
from lib.mcp.client import _bridge as bridge_module
from lib.mcp.registry import get_catalog_entry
from lib.mcp.result_content import MCPToolResult
from lib.mcp.stdio_environment import resolve_vault_env_references

pytestmark = pytest.mark.unit

OWNER = 7
STALE = 'stale-session-value-0000'
FRESH = 'fresh-session-value-1111'
GIT_TOKEN = 'git-token-value-2222'
NOTES = 'Zm9vYmFyLXNlc3Npb24tdmFsdWUtMTIzNDU2'
NOTES_ROTATED = 'Zm9vYmFyLXNlc3Npb24tdmFsdWUtOTg3NjU0'
A1 = '18f2a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8'
WEB_SESSION = '040069b1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7'
WEB_SESSION_ROTATED = '040069ffeeddccbbaa99887766554433221100aa'
SESSDATA = 'e5f1a2b3%2C1790000000%2C9c8d7*b1CjB'
BILI_JCT = '0123456789abcdef0123456789abcdef'
TRACKER = '9f8e7d6c5b4a39281706f5e4d3c2b1a0'
SECRETS = (STALE, FRESH, GIT_TOKEN, NOTES, NOTES_ROTATED, A1, WEB_SESSION,
           WEB_SESSION_ROTATED, SESSDATA, BILI_JCT, TRACKER)
EXPIRED_TEXT = ('Error fetching projects: Not authenticated — check '
                'OVERLEAF_SESSION cookie')
OK_TEXT = 'Your Overleaf projects (1):\n\n  • Thesis  [abc123]\n'
PROBE_TOOL = 'mcp__overleaf__list_projects'
READ_TOOL = 'mcp__overleaf__read_file'
WRITE_TOOL = 'mcp__overleaf__create_file'


def _cookie(name, value, domain, *, http_only=True, path='/', host_only=False):
    return {'name': name, 'value': value, 'domain': domain, 'path': path,
            'hostOnly': host_only, 'secure': True, 'httpOnly': http_only}


def _sent_to(cookie, host):
    domain = cookie['domain'].lstrip('.')
    if cookie['hostOnly']:
        return host == domain
    return host == domain or host.endswith('.' + domain)


class _Browser:
    """One owner's extension devices with Chrome's cookie semantics."""

    def __init__(self):
        self.devices = [
            {'client_id': 'older-device', 'last_poll': 1.0, 'capabilities': ['cookie_match']},
            {'client_id': 'live-device', 'last_poll': 2.0, 'capabilities': ['cookie_match']},
        ]
        self.jar = [_cookie('overleaf_session2', FRESH, '.overleaf.com')]
        self.error = None
        self.commands = []

    def connected(self, *, owner_user_id):
        assert owner_user_id == str(OWNER)
        return [dict(row) for row in self.devices]

    def send(self, cmd_type, params=None, timeout=30, *, client_id,
             owner_user_id, cancel_event=None):
        params = dict(params or {})
        self.commands.append({'type': cmd_type, 'params': params,
                              'client_id': client_id, 'owner': owner_user_id})
        if self.error:
            return None, self.error
        if cmd_type == 'match_cookies':
            return {'matches': self._match(params)}, None
        host = urlparse(params['url']).hostname or ''
        return [dict(cookie) for cookie in self.jar if _sent_to(cookie, host)
                and (not params.get('name') or cookie['name'] == params['name'])], None

    def _match(self, params):
        key = base64.b64decode(params['key'])
        matches = []
        for cookie in self.jar:
            mac = hmac.new(key, cookie['value'].encode(), hashlib.sha256).hexdigest()
            for candidate in params['candidates']:
                if (candidate['length'] == len(cookie['value']) and candidate['mac'] == mac
                        and candidate.get('name', cookie['name']) == cookie['name']):
                    matches.append({'id': candidate['id'], **{
                        k: v for k, v in cookie.items() if k != 'value'}})
        return matches

    def of(self, cmd_type):
        return [command for command in self.commands if command['type'] == cmd_type]


@pytest.fixture(autouse=True)
def _no_device_memory():
    _clear_sticky_clients()
    yield
    _clear_sticky_clients()


@pytest.fixture
def browser(monkeypatch):
    fake = _Browser()
    monkeypatch.setattr(browser_queue, 'get_connected_clients', fake.connected)
    monkeypatch.setattr(browser_queue, 'send_browser_command', fake.send)
    monkeypatch.setattr(browser_access, 'is_read_allowed', lambda user_id, url: True)
    return fake


def _overleaf_row(session, **extra):
    return {'command': 'tofu-overleaf-mcp', 'args': [], 'transport': 'stdio',
            'env': {'OVERLEAF_SESSION': session, 'OVERLEAF_GIT_TOKEN': GIT_TOKEN,
                    **extra}}


def _custom_row(**env):
    return {'command': 'custom-mcp', 'args': [], 'transport': 'stdio', 'env': env}


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_config, '_CONFIG_DIR', str(tmp_path))
    mcp_config.upsert_server('overleaf', _overleaf_row(STALE), user_id=OWNER)
    return tmp_path


@pytest.fixture
def audits(monkeypatch):
    events = []

    def _record(event, **fields):
        events.append((event, fields))

    monkeypatch.setattr(bridge_module, 'audit_log', _record)
    monkeypatch.setattr(browser_credentials, 'audit_log', _record)
    return events


@pytest.fixture
def vault(monkeypatch):
    """An in-memory credential vault; the real one is never touched."""
    entries, writes = {}, []

    def _set_entry(name, value, *, note=None):
        writes.append(name)
        entries[name] = value
        return {'name': name}

    monkeypatch.setattr(credentials_vault, 'get_entry', entries.get)
    monkeypatch.setattr(credentials_vault, 'set_entry', _set_entry)
    return entries, writes


def _row(name):
    return mcp_config.load_mcp_config(user_id=OWNER)[name]


def _assert_no_secret(browser, audits, caplog):
    text = caplog.text + repr(audits) + repr(browser.of('match_cookies'))
    for secret in SECRETS:
        assert secret not in text


# ── Declared cookies ─────────────────────────────────────

def test_overleaf_session_declares_its_browser_cookie():
    specs = {spec['key']: spec for spec in get_catalog_entry('overleaf')['env_specs']}
    assert specs['OVERLEAF_SESSION']['browser_cookie'] == {
        'name': 'overleaf_session2',
        'url': 'https://www.overleaf.com',
        'url_env': 'OVERLEAF_BASE_URL',
    }
    assert 'browser_cookie' not in specs['OVERLEAF_GIT_TOKEN']


def test_renewal_reads_exactly_the_declared_cookie(store, browser, audits, caplog):
    caplog.set_level(logging.INFO, logger='lib')
    outcome = refresh_from_browser('overleaf', owner_user_id=OWNER)

    assert (outcome.reason, outcome.changed) == ('renewed', ('OVERLEAF_SESSION',))
    assert browser.commands == [{
        'type': 'get_cookies',
        'params': {'url': 'https://www.overleaf.com', 'name': 'overleaf_session2'},
        'client_id': 'live-device', 'owner': str(OWNER)}]
    assert _row('overleaf')['env'] == {'OVERLEAF_SESSION': FRESH,
                                        'OVERLEAF_GIT_TOKEN': GIT_TOKEN}
    assert audits == [('mcp_credential_refreshed', {
        'server': 'overleaf', 'env_keys': ['OVERLEAF_SESSION'],
        'source': 'browser_cookie'})]
    assert 'Saved OVERLEAF_SESSION for overleaf' in caplog.text
    _assert_no_secret(browser, audits, caplog)


def test_renewal_through_a_vault_reference_updates_the_vault_and_keeps_the_reference(
        store, browser, audits, vault, caplog):
    caplog.set_level(logging.DEBUG)
    entries, writes = vault
    entries['overleaf_session'] = STALE
    mcp_config.upsert_server(
        'overleaf', _overleaf_row('vault:overleaf_session'), user_id=OWNER)
    before = _row('overleaf')

    outcome = refresh_from_browser('overleaf', owner_user_id=OWNER)

    assert (outcome.reason, outcome.changed) == ('renewed', ('OVERLEAF_SESSION',))
    assert entries['overleaf_session'] == FRESH and writes == ['overleaf_session']
    after = _row('overleaf')
    assert after['env'] == before['env'] == {
        'OVERLEAF_SESSION': 'vault:overleaf_session', 'OVERLEAF_GIT_TOKEN': GIT_TOKEN}
    config_text = (store / 'mcp_servers' / f'{OWNER}.json').read_text()
    assert FRESH not in config_text and STALE not in config_text
    assert audits == [('mcp_credential_refreshed', {
        'server': 'overleaf', 'env_keys': ['OVERLEAF_SESSION'],
        'source': 'browser_cookie'})]
    _assert_no_secret(browser, audits, caplog)

    # The resolved value is compared: a vault already holding the browser's
    # session is current and nothing is written.
    assert refresh_from_browser('overleaf', owner_user_id=OWNER).reason == 'current'
    assert writes == ['overleaf_session']


def test_plaintext_renewal_never_writes_the_vault(store, browser, vault):
    entries, writes = vault

    assert refresh_from_browser('overleaf', owner_user_id=OWNER).reason == 'renewed'

    assert _row('overleaf')['env']['OVERLEAF_SESSION'] == FRESH
    assert entries == {} and writes == []


def test_a_learned_binding_follows_the_vault_value(store, browser, vault):
    entries, writes = vault
    entries['notes_session'] = NOTES
    mcp_config.upsert_server(
        'notes', _custom_row(NOTES_SESSION='vault:notes_session'), user_id=OWNER)
    browser.jar = [_cookie('sid', NOTES, '.notes.example')]

    learn_bindings('notes', owner_user_id=OWNER)
    assert 'NOTES_SESSION' in credential_bindings('notes', _row('notes'))
    assert 'vault:' not in repr(browser.of('match_cookies'))

    browser.jar[0]['value'] = NOTES_ROTATED
    assert refresh_from_browser('notes', owner_user_id=OWNER).changed == ('NOTES_SESSION',)
    assert entries['notes_session'] == NOTES_ROTATED
    assert _row('notes')['env'] == {'NOTES_SESSION': 'vault:notes_session'}
    assert 'NOTES_SESSION' in credential_bindings('notes', _row('notes'))


def test_a_self_hosted_base_url_is_asked_for_its_own_cookie(store, browser):
    mcp_config.upsert_server('overleaf', _overleaf_row(
        STALE, OVERLEAF_BASE_URL='https://latex.example.edu'), user_id=OWNER)
    assert refresh_from_browser('overleaf', owner_user_id=OWNER).reason == 'no_session'
    assert browser.commands[0]['params']['url'] == 'https://latex.example.edu'


def test_servers_without_browser_credentials_never_reach_the_browser(store, browser):
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)
    assert refresh_from_browser('notes', owner_user_id=OWNER).reason == 'unbound'
    assert browser.commands == []


@pytest.mark.parametrize('case, reason', [
    ('no_device', 'no_browser'), ('read_denied', 'no_session'),
    ('command_error', 'no_session'), ('no_cookie', 'no_session'),
    ('control_chars', 'no_session')])
def test_unusable_browser_state_changes_nothing(store, browser, monkeypatch, case, reason):
    if case == 'no_device':
        browser.devices = []
    elif case == 'read_denied':
        monkeypatch.setattr(browser_access, 'is_read_allowed', lambda user_id, url: False)
    elif case == 'command_error':
        browser.error = "Browser command 'get_cookies' timed out after 10s."
    elif case == 'no_cookie':
        browser.jar = []
    else:
        browser.jar[0]['value'] = 'abc\r\nInjected: header'
    assert refresh_from_browser('overleaf', owner_user_id=OWNER).reason == reason
    assert _row('overleaf')['env']['OVERLEAF_SESSION'] == STALE
    if case in ('no_device', 'read_denied'):
        assert browser.commands == []


def test_an_unreachable_bridge_owner_counts_as_no_browser(store, monkeypatch):
    from lib.browser.bridge_relay import RelayUnavailable

    def _unreachable(*, owner_user_id):
        raise RelayUnavailable('The browser bridge relay record is unreadable')

    monkeypatch.setattr(browser_queue, 'get_connected_clients', _unreachable)
    assert refresh_from_browser('overleaf', owner_user_id=OWNER).reason == 'no_browser'


# ── Learning which values copy browser cookies ───────────

def test_any_server_learns_its_session_cookie_without_exposing_values(
        store, browser, audits, caplog):
    caplog.set_level(logging.INFO, logger='lib')
    mcp_config.upsert_server('notes', _custom_row(
        NOTES_SESSION=NOTES, NOTES_USER='alice'), user_id=OWNER)
    browser.jar = [_cookie('sid', NOTES, '.notes.example'),
                   _cookie('user', 'alice', '.notes.example', http_only=False)]

    answered = learn_bindings('notes', owner_user_id=OWNER)

    [command] = browser.of('match_cookies')
    assert command['client_id'] == 'live-device'
    assert len(command['params']['candidates']) == 1  # usernames are never asked about
    assert 'NOTES_SESSION' not in repr(command['params'])
    entry = _row('notes')['browser_credentials']['NOTES_SESSION']
    assert {k: entry[k] for k in ('kind', 'name', 'url', 'client_id')} == {
        'kind': 'cookie', 'name': 'sid', 'url': 'https://notes.example/',
        'client_id': 'live-device'}
    assert len(answered) == 1

    browser.jar[0]['value'] = NOTES_ROTATED
    outcome = refresh_from_browser('notes', owner_user_id=OWNER)
    assert outcome.changed == ('NOTES_SESSION',)
    assert _row('notes')['env']['NOTES_SESSION'] == NOTES_ROTATED
    assert 'NOTES_SESSION' in credential_bindings('notes', _row('notes'))
    _assert_no_secret(browser, audits, caplog)


def test_a_cookie_header_is_learned_and_renewed_in_the_users_order(store, browser):
    header = f'a1={A1}; webId=abc123; web_session={WEB_SESSION}'
    mcp_config.upsert_server('xhs', _custom_row(XHS_COOKIE=header), user_id=OWNER)
    browser.jar = [
        _cookie('web_session', WEB_SESSION, '.xiaohongshu.com'),
        _cookie('a1', A1, '.xiaohongshu.com', http_only=False),
        _cookie('webId', 'abc123', '.xiaohongshu.com', http_only=False),
    ]

    learn_bindings('xhs', owner_user_id=OWNER)

    binding = credential_bindings('xhs', _row('xhs'))['XHS_COOKIE']
    assert {k: binding[k] for k in ('kind', 'url', 'names', 'required')} == {
        'kind': 'cookie_header', 'url': 'https://xiaohongshu.com/',
        'names': ['a1', 'webId', 'web_session'], 'required': ['a1', 'web_session']}

    browser.jar = [_cookie('web_session', WEB_SESSION_ROTATED, '.xiaohongshu.com'),
                   _cookie('a1', A1, '.xiaohongshu.com', http_only=False)]
    assert refresh_from_browser('xhs', owner_user_id=OWNER).changed == ('XHS_COOKIE',)
    assert _row('xhs')['env']['XHS_COOKIE'] == f'a1={A1}; web_session={WEB_SESSION_ROTATED}'

    browser.jar = [_cookie('a1', A1, '.xiaohongshu.com', http_only=False)]  # signed out
    assert refresh_from_browser('xhs', owner_user_id=OWNER).reason == 'no_session'
    assert _row('xhs')['env']['XHS_COOKIE'] == f'a1={A1}; web_session={WEB_SESSION_ROTATED}'


def test_script_readable_cookies_bind_only_beside_a_session_cookie(store, browser):
    mcp_config.upsert_server('bili', _custom_row(
        SESSDATA=SESSDATA, BILI_JCT=BILI_JCT, TRACKER=TRACKER), user_id=OWNER)
    browser.jar = [
        _cookie('SESSDATA', SESSDATA, '.bilibili.com'),
        _cookie('bili_jct', BILI_JCT, '.bilibili.com', http_only=False),
        _cookie('_uuid', TRACKER, '.analytics.example', http_only=False),
    ]

    learn_bindings('bili', owner_user_id=OWNER)

    bindings = credential_bindings('bili', _row('bili'))
    assert sorted(bindings) == ['BILI_JCT', 'SESSDATA']
    assert bindings['BILI_JCT']['name'] == 'bili_jct'


def test_an_ambiguous_match_binds_nothing(store, browser):
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)
    browser.jar = [_cookie('sid', NOTES, '.a.example'), _cookie('token', NOTES, '.b.example')]

    assert learn_bindings('notes', owner_user_id=OWNER)
    assert credential_bindings('notes', _row('notes')) == {}


def test_a_value_changed_elsewhere_drops_its_learned_binding(store, browser):
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)
    browser.jar = [_cookie('sid', NOTES, '.notes.example')]
    learn_bindings('notes', owner_user_id=OWNER)
    row = _row('notes')
    row['env']['NOTES_SESSION'] = 'an-api-key-the-user-pasted-0000000000'
    mcp_config.upsert_server('notes', row, user_id=OWNER)

    assert credential_bindings('notes', _row('notes')) == {}


def test_learning_asks_only_capable_devices_and_renewal_returns_to_them(store, browser):
    browser.devices[1]['capabilities'] = []
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)
    browser.jar = [_cookie('sid', NOTES, '.notes.example')]

    learn_bindings('notes', owner_user_id=OWNER)
    browser.jar[0]['value'] = NOTES_ROTATED
    refresh_from_browser('notes', owner_user_id=OWNER)

    assert [c['client_id'] for c in browser.commands] == ['older-device', 'older-device']
    assert select_owner_client(str(OWNER), browser.devices) == 'live-device'


def test_without_a_capable_device_nothing_is_asked_or_remembered(store, browser):
    browser.devices = [{'client_id': 'old-extension', 'last_poll': 1.0, 'capabilities': []}]
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)

    assert learn_bindings('notes', owner_user_id=OWNER) == frozenset()
    assert browser.commands == []


# ── The MCP bridge ───────────────────────────────────────

def _tool_row(namespaced, tool, *, read_only=True, server='overleaf'):
    return {
        'server_name': server, 'tool_name': tool,
        'namespaced_name': namespaced, 'description': '',
        'input_schema': {'type': 'object', 'properties': {}},
        'openai_def': {}, 'read_only_hint': read_only, 'meta': {},
        'schema_hash': 'stable', 'catalog_version': 'v1',
    }


def _bridge(live_session, *, owner=OWNER, parked=False, verdict=None,
            errors=False, failing=False):
    """A bridge whose live 'overleaf' answers by the session it was started with."""
    bridge = bridge_module.MCPBridge(owner_user_id=owner)
    config = _overleaf_row(live_session)
    handle = bridge_module._MCPServerHandle('overleaf', copy.deepcopy(config))
    handle.session = None if parked else object()
    # The session a live child holds is the one resolved when it started.
    live = {'session': resolve_vault_env_references(config['env'])['OVERLEAF_SESSION']}
    bridge._servers['overleaf'] = handle
    bridge._configs['overleaf'] = copy.deepcopy(config)
    bridge._started = True
    if parked:
        bridge._parked.add('overleaf')
    if verdict:
        bridge._cred_health['overleaf'] = {
            'status': verdict, 'checked_at': time.time(), 'detail': ''}
    if failing:
        bridge._breaker['overleaf'] = (1, 0.0)
    for namespaced, tool, read_only in ((PROBE_TOOL, 'list_projects', True),
                                        (READ_TOOL, 'read_file', True),
                                        (WRITE_TOOL, 'create_file', False)):
        bridge._tool_index[namespaced] = _tool_row(namespaced, tool, read_only=read_only)
    connects, calls = [], []

    def _connect(name, srv_cfg):
        # The real connect_server tears the old transport down first: a live
        # server loses its credential verdict, a parked one keeps it.
        connects.append(copy.deepcopy(srv_cfg))
        fresh = bridge_module._MCPServerHandle(name, copy.deepcopy(srv_cfg))
        fresh.session = object()
        live['session'] = resolve_vault_env_references(
            srv_cfg['env'])['OVERLEAF_SESSION']
        with bridge._lock:
            if name not in bridge._parked:
                bridge._cred_health.pop(name, None)
            bridge._servers[name] = fresh
            bridge._configs[name] = copy.deepcopy(srv_cfg)
            bridge._parked.discard(name)
        return []

    def _run_async(coro, timeout=None, **_kwargs):
        coro.close()
        session = live['session']
        calls.append(session)
        if session == FRESH:
            return OK_TEXT
        if errors:
            return MCPToolResult(EXPIRED_TEXT, is_error=True, document_text=EXPIRED_TEXT)
        return EXPIRED_TEXT

    bridge.connect_server = _connect
    bridge._run_async = _run_async
    return bridge, connects, calls


def test_expired_probe_copies_the_browser_cookie_and_reconnects(
        store, browser, audits, caplog):
    caplog.set_level(logging.DEBUG)
    bridge, connects, _ = _bridge(STALE)

    record = bridge._run_cred_probe('overleaf')

    assert record['status'] == 'ok'
    assert bridge.get_cred_health('overleaf')['status'] == 'ok'
    assert _row('overleaf')['env'] == {'OVERLEAF_SESSION': FRESH,
                                        'OVERLEAF_GIT_TOKEN': GIT_TOKEN}
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert [event for event, _ in audits] == [
        'mcp_credential_expired', 'mcp_credential_refreshed', 'mcp_reconnect']
    _assert_no_secret(browser, audits, caplog)


@pytest.mark.parametrize('parked', [False, True])
def test_an_expired_vault_held_session_renews_and_reconnects(
        store, browser, audits, vault, caplog, parked):
    caplog.set_level(logging.DEBUG)
    entries, _ = vault
    entries['overleaf_session'] = STALE
    reference = 'vault:overleaf_session'
    mcp_config.upsert_server('overleaf', _overleaf_row(reference), user_id=OWNER)
    bridge, connects, calls = _bridge(reference, parked=parked, verdict='expired')

    assert bridge.call_tool(READ_TOOL, {}) == OK_TEXT

    assert calls == [FRESH] and entries['overleaf_session'] == FRESH
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [reference]
    assert _row('overleaf')['env']['OVERLEAF_SESSION'] == reference
    assert bridge.get_cred_health('overleaf') is None
    _assert_no_secret(browser, audits, caplog)


def test_an_expired_vault_held_session_renews_after_a_probe(
        store, browser, audits, vault):
    entries, _ = vault
    entries['overleaf_session'] = STALE
    reference = 'vault:overleaf_session'
    mcp_config.upsert_server('overleaf', _overleaf_row(reference), user_id=OWNER)
    bridge, connects, _ = _bridge(reference)

    assert bridge._run_cred_probe('overleaf')['status'] == 'ok'
    assert len(connects) == 1 and entries['overleaf_session'] == FRESH


def test_expired_probe_without_a_newer_credential_changes_nothing(store, browser, audits):
    browser.jar[0]['value'] = STALE
    before = (store / 'mcp_servers' / f'{OWNER}.json').read_bytes()
    bridge, connects, _ = _bridge(STALE)

    assert bridge._run_cred_probe('overleaf')['status'] == 'expired'
    assert connects == []
    assert (store / 'mcp_servers' / f'{OWNER}.json').read_bytes() == before
    assert [event for event, _ in audits] == ['mcp_credential_expired']


def test_a_credential_saved_elsewhere_renews_without_the_browser(store, browser, audits):
    browser.devices = []
    mcp_config.upsert_server('overleaf', _overleaf_row(FRESH), user_id=OWNER)
    bridge, connects, _ = _bridge(STALE)

    assert bridge._run_cred_probe('overleaf')['status'] == 'ok'
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert [event for event, _ in audits] == ['mcp_credential_expired', 'mcp_reconnect']


def test_a_disabled_stored_server_is_not_renewed(store, browser):
    mcp_config.patch_server('overleaf', {'enabled': False}, user_id=OWNER)
    bridge, connects, _ = _bridge(STALE)

    assert bridge._run_cred_probe('overleaf')['status'] == 'expired'
    assert connects == []


def test_a_failed_renewal_reconnect_keeps_the_expired_verdict(store, browser):
    bridge, _, _ = _bridge(STALE)

    def _broken_connect(name, srv_cfg):
        raise RuntimeError('server failed to start')

    bridge.connect_server = _broken_connect

    assert bridge._run_cred_probe('overleaf')['status'] == 'expired'
    assert _row('overleaf')['env']['OVERLEAF_SESSION'] == FRESH


@pytest.mark.parametrize('parked', [False, True])
def test_a_known_expired_server_renews_before_the_call(
        store, browser, audits, caplog, parked):
    caplog.set_level(logging.DEBUG)
    bridge, connects, _ = _bridge(STALE, parked=parked, verdict='expired')

    assert bridge.call_tool(READ_TOOL, {}) == OK_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert _row('overleaf')['env']['OVERLEAF_SESSION'] == FRESH
    assert 'overleaf' not in bridge._parked
    assert bridge.get_cred_health('overleaf') is None
    _assert_no_secret(browser, audits, caplog)


def test_a_working_credential_never_consults_the_browser(store, browser):
    mcp_config.upsert_server('overleaf', _overleaf_row(FRESH), user_id=OWNER)
    bridge, connects, _ = _bridge(FRESH, verdict='ok', errors=True)

    assert bridge.call_tool(READ_TOOL, {}) == OK_TEXT

    assert connects == [] and browser.commands == []


def test_waking_a_parked_server_uses_the_stored_credential(store, browser):
    mcp_config.upsert_server('overleaf', _overleaf_row(FRESH), user_id=OWNER)
    bridge, connects, _ = _bridge(STALE, parked=True)

    assert bridge.call_tool(READ_TOOL, {}) == OK_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert browser.commands == []


def test_an_unowned_bridge_wakes_with_its_last_known_config(browser):
    bridge, connects, _ = _bridge(STALE, owner=None, parked=True, verdict='expired')

    assert bridge.call_tool(READ_TOOL, {}) == EXPIRED_TEXT

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [STALE]
    assert browser.commands == []


def test_a_rejected_read_renews_from_the_browser_and_repeats_once(
        store, browser, audits, caplog):
    caplog.set_level(logging.DEBUG)
    bridge, connects, calls = _bridge(STALE, errors=True)

    assert bridge.call_tool(READ_TOOL, {}) == OK_TEXT

    assert calls == [STALE, FRESH]
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert [event for event, _ in audits] == [
        'mcp_credential_expired', 'mcp_credential_refreshed', 'mcp_reconnect']
    _assert_no_secret(browser, audits, caplog)


def test_a_rejected_write_is_not_repeated_but_says_to_repeat_it(store, browser):
    bridge, connects, calls = _bridge(STALE, errors=True)

    result = bridge.call_tool(WRITE_TOOL, {})

    assert calls == [STALE]
    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]
    assert result.is_error and result.startswith(EXPIRED_TEXT)
    assert 'reconnected; repeat the call' in result
    assert result.document_text.endswith('repeat the call.')


@pytest.mark.parametrize('devices, action', [
    (True, 'sign in to www.overleaf.com in the browser connected to Tofu'),
    (False, 'connect the Tofu browser extension and sign in to www.overleaf.com')])
def test_without_a_newer_session_the_agent_is_told_to_have_the_user_sign_in(
        store, browser, devices, action):
    browser.jar[0]['value'] = STALE
    if not devices:
        browser.devices = []
    bridge, connects, _ = _bridge(STALE, errors=True)

    result = bridge.call_tool(READ_TOOL, {})

    assert connects == []
    assert action in result and 'Do not ask the user for cookie values' in result
    assert bridge.get_cred_health('overleaf')['status'] == 'expired'


def test_auth_words_in_a_successful_result_are_not_a_failure(store, browser):
    bridge, connects, _ = _bridge(STALE)  # plain-text result, no error flag

    assert bridge.call_tool(READ_TOOL, {}) == EXPIRED_TEXT

    assert connects == [] and browser.commands == []
    assert bridge.get_cred_health('overleaf') is None


def test_servers_without_browser_credentials_keep_their_error(store, browser, monkeypatch):
    monkeypatch.setattr(browser_credentials, 'credential_bindings', lambda name, row: {})
    bridge, connects, _ = _bridge(STALE, errors=True)

    result = bridge.call_tool(READ_TOOL, {})

    assert str(result) == EXPIRED_TEXT and browser.commands == [] and connects == []


def test_a_failed_connect_renews_before_the_next_attempt(store, browser):
    bridge, connects, _ = _bridge(STALE, parked=True, failing=True)

    bridge._reconnect_server('overleaf')

    assert [row['env']['OVERLEAF_SESSION'] for row in connects] == [FRESH]


def test_learning_after_connect_asks_the_browser_once_per_value(store, browser):
    mcp_config.upsert_server('notes', _custom_row(NOTES_SESSION=NOTES), user_id=OWNER)
    browser.jar = [_cookie('user', NOTES, '.notes.example', http_only=False)]
    bridge = bridge_module.MCPBridge(owner_user_id=OWNER)

    asked = []
    for _ in range(2):
        assert bridge._learn_browser_credentials_async('notes')
        deadline = time.monotonic() + 5
        while bridge._credential_learning_inflight:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        asked.append(len(browser.of('match_cookies')))

    # Every capable device is asked once; the second pass asks nobody.
    assert asked == [2, 2]
    assert credential_bindings('notes', _row('notes')) == {}
