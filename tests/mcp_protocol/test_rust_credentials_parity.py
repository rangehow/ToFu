"""Rust credential probes, renewal and browser credential learning vs the Python oracle.

The Rust owners (packages/tofu-mcp: ``health_probe.rs``, ``browser_credentials.rs``,
``bridge/credentials.rs``) are driven through ``tofu-mcp-host``; the state
Python owns (stored server rows, the credential vault, browser devices and
commands, the catalog) is reached through the host's binding calls, answered
by ``lib/mcp/rust_host.py``'s handlers, which call the same Python owners the
oracle calls. Every scenario runs twice in a fresh *world* (its own config
directory, fake browser, in-memory vault and audit record): once through the
oracle (``lib.mcp.browser_credentials`` / ``MCPBridge`` exactly as the oracle
tests set it up) and once through the Rust client, whose overleaf-like server
is a real stdio MCP child answering by the session it was started with. The
observations are compared, then the oracle file's own expectation is asserted
on the Rust one.

Comparison notes: the oracle bridge tests stub ``_run_async``, so their error
result is the bare text; a real projection prefixes ``MCP Error: `` (both
clients do), which is stripped before comparing. ``match_cookies`` requests
carry a fresh random key, so they are compared by device, candidate ids,
lengths and names; the fake browser only matches when the Rust HMAC is right.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

import lib.browser.access as browser_access
import lib.browser.queue as browser_queue
import lib.credentials_vault as credentials_vault
import lib.mcp.browser_credentials as browser_credentials
import lib.mcp.config as mcp_config
from lib.browser.dispatch import _clear_sticky_clients, select_owner_client
from lib.mcp.client import _bridge as bridge_module
from lib.mcp.health_probe import (HEALTH_PROBE_SCHEMA, classify_probe_result,
                                  validate_health_probe)
from lib.mcp.registry import get_catalog_entry
from tests.mcp_protocol import test_mcp_browser_credentials as oracle
from tests.mcp_protocol.rust_parity_support import (  # noqa: F401 - fixtures
    ROOT, mcp_host_binary, scene,
)
from tests.support.overlay import requires_yourprovider_overlay
__all__ = ['mcp_host_binary', 'scene']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {
    'test_mcp_health_probe_contract.py': [
        'test_validate_health_probe_matches_python',
        'test_classify_probe_result_matches_python',
        'test_schema_and_markers_match_python',
        'test_custom_server_config_declares_probe_on_both',
        'test_custom_server_without_probe_is_noop_on_both',
    ],
    'test_mcp_cred_health.py': [
        'test_overleaf_catalog_probe_spec_on_both',
        'test_probe_verdicts_on_both',
        'test_probe_noop_without_spec_or_connection_on_both',
        'test_cred_probe_due_gates_on_interval_on_both',
        'test_disconnect_drops_cred_health_on_both',
    ],
    'test_mcp_browser_credentials.py': [
        'test_overleaf_declares_its_browser_cookie_through_the_rust_catalog',
        'test_refresh_scenarios_on_both',
        'test_unusable_browser_state_on_both',
        'test_an_unreachable_bridge_owner_counts_as_no_browser_on_both',
        'test_learning_scenarios_on_both',
        'test_bridge_scenarios_on_both',
        'test_failed_renewal_reconnect_keeps_the_expired_verdict_on_both',
        'test_a_failed_connect_renews_before_the_next_attempt_on_both',
        'test_learning_pass_asks_once_per_value_on_both',
        'test_failure_notice_matches_python',
    ],
    'test_mcp_online_docs_catalog.py': [
        'test_catalog_probe_specs_on_both',
        'test_reviewed_read_only_rule_matches_python',
        'test_bridge_folds_the_review_into_the_read_partition_on_both',
    ],
    'test_shared_sso_configuration.py': [
        'test_internal_sso_entries_reach_the_rust_child_environment',
        'test_stdio_cache_identity_cannot_be_overridden_on_both',
    ],
    'test_mcp_registry_env_specs.py': [
        'test_internal_env_specs_read_through_the_rust_catalog',
    ],
    'test_stdio_vault_env_references.py': [
        'test_vault_references_resolve_like_python',
    ],
}

OWNER = oracle.OWNER
STALE, FRESH, GIT_TOKEN = oracle.STALE, oracle.FRESH, oracle.GIT_TOKEN
EXPIRED_TEXT, OK_TEXT = oracle.EXPIRED_TEXT, oracle.OK_TEXT
CREDENTIAL_EVENTS = ('mcp_credential_expired', 'mcp_credential_refreshed', 'mcp_reconnect')

SERVER = r'''
import json, os, sys
config = json.load(open(sys.argv[1], encoding='utf-8'))
log = sys.argv[2]
session = os.environ.get('OVERLEAF_SESSION', '')
def note(row):
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row) + '\n')
note({'event': 'start', 'session': session})
tools = [{'name': name, 'description': name, 'inputSchema': {'type': 'object', 'properties': {}},
          'annotations': {'readOnlyHint': read_only, 'destructiveHint': not read_only}}
         for name, read_only in config['tools']]
for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    if 'id' not in message:
        continue
    method, ident, params = message.get('method'), message['id'], message.get('params') or {}
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion') or '2025-06-18',
                  'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'cred-fake', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': tools}
    elif method == 'ping':
        result = {}
    elif method == 'tools/call':
        name = params.get('name')
        note({'event': 'call', 'tool': name, 'session': session})
        if name in config.get('raise', {}):
            print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {
                'code': -32603, 'message': config['raise'][name]}}), flush=True)
            continue
        if name in config.get('texts', {}):
            text, error = config['texts'][name], False
        elif session == config['fresh']:
            text, error = config['ok'], False
        else:
            text, error = config['expired'], bool(config.get('errors'))
        result = {'content': [{'type': 'text', 'text': text}], 'isError': error}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {
            'code': -32601, 'message': 'Method not found'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}), flush=True)
'''

OVERLEAF_TOOLS = [('list_projects', True), ('read_file', True), ('create_file', False)]


# ── Worlds: one oracle run and one Rust run, each with its own state ─────────


class World:
    """One run's config directory, browser, vault and audit record."""

    def __init__(self, root: Path, label: str, monkeypatch):
        self.dir = root / label
        self.dir.mkdir(parents=True)
        self.monkeypatch = monkeypatch
        self.browser = oracle._Browser()
        self.audits: list = []
        self.vault: dict[str, str] = {}
        self.vault_writes: list[str] = []
        self.read_allowed = True
        self.on_vault_write = None

    def _set_entry(self, name, value, *, note=None):
        self.vault_writes.append(name)
        self.vault[name] = value
        if self.on_vault_write is not None:
            self.on_vault_write()
        return {'name': name}

    def activate(self):
        mp = self.monkeypatch
        mp.setattr(mcp_config, '_CONFIG_DIR', str(self.dir))
        mp.setattr(browser_queue, 'get_connected_clients', self.browser.connected)
        mp.setattr(browser_queue, 'send_browser_command', self.browser.send)
        mp.setattr(browser_access, 'is_read_allowed', lambda user_id, url: self.read_allowed)
        mp.setattr(credentials_vault, 'get_entry', self.vault.get)
        mp.setattr(credentials_vault, 'set_entry', self._set_entry)
        record = lambda event, **fields: self.audits.append((event, fields))  # noqa: E731
        mp.setattr(bridge_module, 'audit_log', record)
        mp.setattr(browser_credentials, 'audit_log', record)
        _clear_sticky_clients()
        return self

    def upsert(self, name, row):
        mcp_config.upsert_server(name, row, user_id=OWNER)

    def row(self, name):
        return mcp_config.load_mcp_config(user_id=OWNER).get(name)

    def config_text(self):
        path = self.dir / 'mcp_servers' / f'{OWNER}.json'
        return path.read_text(encoding='utf-8') if path.exists() else ''


class RustSide:
    """A ``tofu-mcp-host`` whose binding answers from the active world."""

    def __init__(self, binary, scene, tmp: Path, world: World, *, interval='-1', owner=OWNER):
        from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
        self.world, self.tmp, self.scene, self.owner = world, tmp, scene, owner
        env = {**os.environ, 'TOFU_MCP_CRED_PROBE_INTERVAL': interval}
        self.host = RustMCPHost(binary, env=env)
        self.configure()
        world.on_vault_write = self.configure
        self.bridge = RustMCPBridge(self.host, owner)
        self.log = tmp / 'server.log'
        self.server_config = tmp / 'server.json'
        self.shim = tmp / 'overleaf-fake'
        self.shim.write_text(f'#!/bin/sh\nexec {sys.executable} {tmp / "server.py"} '
                             f'{self.server_config} {self.log}\n', encoding='utf-8')
        self.shim.chmod(0o755)
        (tmp / 'server.py').write_text(SERVER, encoding='utf-8')
        self.serve()

    def configure(self):
        # The child environment resolves vault references from the host's
        # configured vault snapshot (stdio_env.rs); keep it equal to the world's.
        self.host.call('configure', {**self.scene.host_settings(), 'vault': dict(self.world.vault)},
                       timeout=30)

    def serve(self, *, tools=OVERLEAF_TOOLS, errors=False, texts=None, raises=None):
        self.server_config.write_text(json.dumps({
            'tools': tools, 'fresh': FRESH, 'ok': OK_TEXT, 'expired': EXPIRED_TEXT,
            'errors': errors, 'texts': texts or {}, 'raise': raises or {}}), encoding='utf-8')

    def row(self, session, **extra):
        row = oracle._overleaf_row(session, **extra)
        row['command'] = str(self.shim)
        return row

    def credentials(self, method, **params):
        return self.host.call(f'credentials.{method}', {'owner_user_id': self.owner, **params}, timeout=60)

    def events(self):
        return [row for row in (json.loads(line) for line in
                                self.log.read_text(encoding='utf-8').splitlines())] if self.log.exists() else []

    def starts(self):
        return [row['session'] for row in self.events() if row['event'] == 'start']

    def calls(self):
        return [row['session'] for row in self.events() if row['event'] == 'call']

    def audits(self):
        return [(event, fields) for event, fields in self.host.audit if event in CREDENTIAL_EVENTS]

    def settle(self):
        deadline = time.monotonic() + 30
        while True:
            busy = self.credentials('inflight')
            if not busy['probes'] and not busy['learning']:
                return
            assert time.monotonic() < deadline, busy
            time.sleep(0.02)

    def close(self):
        self.host.close()


def _match_shape(browser):
    return [(c['client_id'], [(e['id'], e['length'], e.get('name')) for e in c['params']['candidates']])
            for c in browser.of('match_cookies')]


def _commands(browser):
    return [c for c in browser.commands if c['type'] != 'match_cookies']


def _no_secret(text):
    for secret in oracle.SECRETS:
        assert secret not in text


@pytest.fixture
def worlds(tmp_path, monkeypatch):
    made = {}

    def make(label):
        made[label] = World(tmp_path / 'worlds', label, monkeypatch)
        return made[label]
    return make


@pytest.fixture
def rust_side(mcp_host_binary, scene, tmp_path):
    sides = []

    def make(world, **kwargs):
        directory = tmp_path / f'rust-{len(sides)}'
        directory.mkdir()
        side = RustSide(mcp_host_binary, scene, directory, world, **kwargs)
        sides.append(side)
        return side
    yield make
    for side in sides:
        side.close()


# ── The health-probe contract (pure) ─────────────────────────────────────────

VALIDATE_INPUTS = [
    {'tool': ' whoami ', 'fail_patterns': ['Custom Boom', 'custom boom']},
    {'args': {}},
    {'tool': '   '},
    None,
    'nope',
    123,
    {'tool': 'ping', 'args': 'oops', 'fail_patterns': 'oops'},
    {'tool': 'ping', 'args': None, 'fail_patterns': ['', '  ', 5, 'X Y']},
    {'tool': 'status', 'args': {'limit': 1}, 'fail_patterns': ['boom expired']},
    [1, 2],
]


@pytest.fixture
def host(mcp_host_binary):
    from lib.mcp.rust_host import RustMCPHost
    started = RustMCPHost(mcp_host_binary, env=dict(os.environ))
    yield started
    started.close()


@pytest.mark.parametrize('spec', VALIDATE_INPUTS, ids=repr)
def test_validate_health_probe_matches_python(host, spec, caplog):
    caplog.set_level(logging.WARNING)
    python = validate_health_probe(spec, server='x')
    warnings = [r.getMessage() for r in caplog.records if r.name.endswith('health_probe')]
    rust = host.call('credentials.validate_health_probe', {'spec': spec, 'server': 'x'}, timeout=30)
    assert rust == {'spec': python, 'warnings': warnings}


def _norm(explicit=None):
    return validate_health_probe({'tool': 't', 'fail_patterns': explicit or []}, server='x')


CLASSIFY_CASES = [
    ('{"ok": false, "login_required": true, "error_hint": "run login"}', _norm()),
    ('MCP Error: Not authenticated.', _norm()),
    ('错误：登录已过期，请重新登录', _norm()),
    ('Error fetching projects: HTTP 302 redirect to /login', _norm(['error fetching projects'])),
    ('Your projects (2):\n  • Thesis\n  • Notes', _norm(['error fetching projects'])),
    ('Your projects (1):\n  • login-service-docs [abc]', _norm()),
    ('MCP Error: not authenticated', {'tool': 't', 'args': {}, 'fail_patterns': []}),
    ('{"ok": false, "login_required": true}', validate_health_probe({'tool': 't'})),
    ('{"ok": false, "login_required":true}', {'tool': 't', 'fail_patterns': ['never']}),
    ("\n\n  {'login_required': True}\n" + 'x' * 300, {}),
    ('\n   \n' + 'Ü' * 250 + ' session expired', {}),
    ('{"code":400006,"message":"Token 无效，请检查 Authorization 请求头中的 Bearer Token"}',
     validate_health_probe(get_catalog_entry('kdocs')['health_probe'], server='kdocs')),
    ('', {}),
]


@pytest.mark.parametrize('text, spec', CLASSIFY_CASES)
def test_classify_probe_result_matches_python(host, text, spec):
    rust = host.call('credentials.classify_probe_result', {'text': text, 'spec': spec}, timeout=30)
    assert tuple(rust) == classify_probe_result(text, spec)


def test_schema_and_markers_match_python(host):
    """The advertised contract (and so the structured markers that alone catch
    the underscore ``login_required`` form) are the oracle's."""
    assert host.call('credentials.health_probe_schema', {}, timeout=30) == HEALTH_PROBE_SCHEMA
    spec = {'tool': 't', 'fail_patterns': HEALTH_PROBE_SCHEMA['default_fail_patterns']}
    assert not any(p in '{"ok": false, "login_required": true}' for p in spec['fail_patterns'])
    assert host.call('credentials.classify_probe_result', {
        'text': '{"ok": false, "login_required": true}', 'spec': spec}, timeout=30)[0] == 'expired'


# ── Bridge probes (test_mcp_cred_health / custom servers) ─────────────────────


def _python_probe_bridge(name, config, tool, outcome):
    """The oracle tests' bridge: a fake connected server whose call_tool returns ``outcome``."""
    import lib.mcp.client as mc
    bridge = bridge_module.MCPBridge()
    handle = mc._MCPServerHandle(name, config)
    handle.session = object()
    bridge._servers[name] = handle
    seen = {}

    def _fake_call_tool(namespaced, arguments, *, timeout_override=None):
        seen['timeout'] = timeout_override
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    bridge.call_tool = _fake_call_tool
    return bridge, seen


def _rust_probe_bridge(rust, name, config, tool, outcome):
    tools = [(tool, True)]
    if isinstance(outcome, Exception):
        rust.serve(tools=tools, raises={tool: str(outcome)})
    else:
        rust.serve(tools=tools, texts={tool: outcome})
    rust.bridge.connect_server(name, {**config, 'command': str(rust.shim), 'args': []})


def _verdict(record):
    return None if record is None else (record['status'], record['detail'])


def test_overleaf_catalog_probe_spec_on_both(worlds, rust_side):
    rust = rust_side(worlds('rust').activate(), owner=None)
    rust.bridge.connect_server('overleaf', rust.row(STALE))
    python = validate_health_probe(get_catalog_entry('overleaf')['health_probe'], server='overleaf')
    spec = rust.credentials('cred_probe_spec', name='overleaf')
    assert spec == python
    assert 'overleaf_session' in spec['fail_patterns']
    assert 'session cookie has expired' in spec['fail_patterns']


PROBE_OUTCOMES = [
    'Error fetching projects: HTTP 302\n\nIf your session cookie has expired (~30 days), '
    'update the OVERLEAF_SESSION environment variable with a fresh value.',
    'Your Overleaf projects (3):\n\n  • Thesis  [abc123]\n',
    RuntimeError('connection reset'),
]


@pytest.mark.parametrize('outcome', PROBE_OUTCOMES, ids=['expired', 'ok', 'raised'])
def test_probe_verdicts_on_both(worlds, rust_side, outcome):
    from lib.mcp.types import MCP_CRED_PROBE_TIMEOUT
    python_bridge, seen = _python_probe_bridge('overleaf', {}, 'list_projects', outcome)
    python = _verdict(python_bridge._run_cred_probe('overleaf'))
    assert seen['timeout'] == MCP_CRED_PROBE_TIMEOUT
    rust = rust_side(worlds('rust').activate(), owner=None)
    _rust_probe_bridge(rust, 'overleaf', {'env': {'OVERLEAF_SESSION': STALE}}, 'list_projects', outcome)
    record = rust.credentials('run_cred_probe', name='overleaf')
    assert _verdict(record) == python
    assert _verdict(rust.credentials('get_cred_health', name='overleaf')) == python
    assert record['status'] == {0: 'expired', 1: 'ok', 2: 'unknown'}[PROBE_OUTCOMES.index(outcome)]


def test_probe_noop_without_spec_or_connection_on_both(worlds, rust_side):
    python_bridge, _ = _python_probe_bridge('my-custom', {}, 'whoami', 'ok')
    assert python_bridge._cred_probe_spec('my-custom') is None
    rust = rust_side(worlds('rust').activate(), owner=None)
    # The catalog spec resolves, but nothing is connected to probe.
    assert rust.credentials('cred_probe_spec', name='overleaf') == \
        bridge_module.MCPBridge()._cred_probe_spec('overleaf')
    for method in ('run_cred_probe', 'get_cred_health'):
        assert rust.credentials(method, name='overleaf') is None
    assert bridge_module.MCPBridge()._run_cred_probe('overleaf') is None
    _rust_probe_bridge(rust, 'my-custom', {}, 'whoami', 'ok')
    for method in ('cred_probe_spec', 'run_cred_probe', 'get_cred_health'):
        assert rust.credentials(method, name='my-custom') is None
    assert rust.calls() == []


def test_custom_server_config_declares_probe_on_both(worlds, rust_side):
    config = {'health_probe': {'tool': 'status', 'fail_patterns': ['boom expired']}}
    python_bridge, _ = _python_probe_bridge('mycustom', {'command': 'my-cli', **config}, 'status',
                                            'service reports: BOOM EXPIRED token')
    python_spec = python_bridge._cred_probe_spec('mycustom')
    python = _verdict(python_bridge._run_cred_probe('mycustom'))
    rust = rust_side(worlds('rust').activate(), owner=None)
    _rust_probe_bridge(rust, 'mycustom', config, 'status', 'service reports: BOOM EXPIRED token')
    assert rust.credentials('cred_probe_spec', name='mycustom') == python_spec
    assert _verdict(rust.credentials('run_cred_probe', name='mycustom')) == python
    assert python[0] == 'expired'


def test_custom_server_without_probe_is_noop_on_both(worlds, rust_side):
    python_bridge, _ = _python_probe_bridge('plain', {'command': 'x'}, 'status', 'ok')
    assert python_bridge._cred_probe_spec('plain') is None
    assert python_bridge._run_cred_probe('plain') is None
    rust = rust_side(worlds('rust').activate(), owner=None)
    _rust_probe_bridge(rust, 'plain', {}, 'status', 'ok')
    assert rust.credentials('cred_probe_spec', name='plain') is None
    assert rust.credentials('run_cred_probe', name='plain') is None


def test_cred_probe_due_gates_on_interval_on_both(worlds, rust_side, monkeypatch):
    from lib.mcp.types import MCP_CRED_PROBE_INTERVAL
    python_bridge, _ = _python_probe_bridge('overleaf', {}, 'list_projects', 'ok')
    python = [python_bridge._cred_probe_due('overleaf')]
    python_bridge._run_cred_probe('overleaf')
    python.append(python_bridge._cred_probe_due('overleaf'))
    python_bridge._cred_probe_ts['overleaf'] = time.time() - (MCP_CRED_PROBE_INTERVAL + 1)
    python.append(python_bridge._cred_probe_due('overleaf'))

    rust = rust_side(worlds('rust').activate(), owner=None, interval=str(MCP_CRED_PROBE_INTERVAL))
    _rust_probe_bridge(rust, 'overleaf', {'env': {'OVERLEAF_SESSION': STALE}}, 'list_projects', 'ok')
    rust.settle()  # the connect-time probe
    rust.credentials('age_cred_probe', name='overleaf', seconds=MCP_CRED_PROBE_INTERVAL + 1)
    observed = [rust.credentials('cred_probe_due', name='overleaf')]
    assert rust.credentials('sweep', name='overleaf') is True  # one sweep pass probes
    observed.append(rust.credentials('cred_probe_due', name='overleaf'))
    assert rust.credentials('sweep', name='overleaf') is False
    rust.credentials('age_cred_probe', name='overleaf', seconds=MCP_CRED_PROBE_INTERVAL + 1)
    observed.append(rust.credentials('cred_probe_due', name='overleaf'))
    assert observed == python == [True, False, True]
    assert rust.calls() == [STALE, STALE]  # connect-time probe + one sweep probe


def test_disconnect_drops_cred_health_on_both(worlds, rust_side):
    python_bridge, _ = _python_probe_bridge('overleaf', {}, 'list_projects',
                                            'Error fetching projects — session cookie has expired')
    python_bridge._run_cred_probe('overleaf')
    python_bridge._disconnect_one('overleaf', forget=True)
    rust = rust_side(worlds('rust').activate(), owner=None)
    _rust_probe_bridge(rust, 'overleaf', {'env': {'OVERLEAF_SESSION': STALE}}, 'list_projects',
                       'Error fetching projects — session cookie has expired')
    assert rust.credentials('run_cred_probe', name='overleaf')['status'] == 'expired'
    rust.host.call('disconnect_server', {'owner_user_id': None, 'name': 'overleaf', 'forget': True},
                   timeout=30)
    assert rust.credentials('get_cred_health', name='overleaf') is None
    assert python_bridge.get_cred_health('overleaf') is None
    assert 'overleaf' not in python_bridge._cred_probe_ts


# ── Browser credentials: declared cookies, renewal, learning ─────────────────


def test_overleaf_declares_its_browser_cookie_through_the_rust_catalog(worlds, rust_side):
    world = worlds('rust').activate()
    rust = rust_side(world)
    world.upsert('overleaf', oracle._overleaf_row(STALE))
    assert rust.credentials('credential_bindings', name='overleaf') == {
        'OVERLEAF_SESSION': {'kind': 'cookie', 'url': 'https://www.overleaf.com',
                             'name': 'overleaf_session2'}}
    assert credential_bindings_python(world, 'overleaf') == rust.credentials(
        'credential_bindings', name='overleaf')


def credential_bindings_python(world, name):
    return browser_credentials.credential_bindings(name, world.row(name))


class PythonApi:
    def __init__(self, world):
        self.world = world

    def refresh(self, name):
        outcome = browser_credentials.refresh_from_browser(name, owner_user_id=OWNER)
        return {'row': outcome.row, 'changed': list(outcome.changed), 'reason': outcome.reason}

    def learn(self, name):
        return sorted(browser_credentials.learn_bindings(name, owner_user_id=OWNER))

    def bindings(self, name):
        row = self.world.row(name)
        return browser_credentials.credential_bindings(name, row) if row else {}

    def audits(self):
        return list(self.world.audits)


class RustApi:
    def __init__(self, side):
        self.side = side

    def refresh(self, name):
        return self.side.credentials('refresh_from_browser', name=name)

    def learn(self, name):
        return sorted(self.side.credentials('learn_bindings', name=name))

    def bindings(self, name):
        return self.side.credentials('credential_bindings', name=name)

    def audits(self):
        return self.side.audits()


def _scenario_declared(world, api):
    world.upsert('overleaf', oracle._overleaf_row(STALE))
    outcome = api.refresh('overleaf')
    return {'outcome': (outcome['reason'], outcome['changed']), 'commands': world.browser.commands,
            'env': world.row('overleaf')['env'], 'audits': api.audits()}


def _scenario_vault(world, api):
    world.vault['overleaf_session'] = STALE
    world.upsert('overleaf', oracle._overleaf_row('vault:overleaf_session'))
    first = api.refresh('overleaf')
    second = api.refresh('overleaf')
    assert FRESH not in world.config_text() and STALE not in world.config_text()
    return {'first': (first['reason'], first['changed']), 'second': second['reason'],
            'vault': dict(world.vault), 'writes': list(world.vault_writes),
            'env': world.row('overleaf')['env'], 'audits': api.audits()}


def _scenario_plaintext(world, api):
    world.upsert('overleaf', oracle._overleaf_row(STALE))
    reason = api.refresh('overleaf')['reason']
    return {'reason': reason, 'session': world.row('overleaf')['env']['OVERLEAF_SESSION'],
            'vault': dict(world.vault), 'writes': list(world.vault_writes)}


def _scenario_learned_vault(world, api):
    world.vault['notes_session'] = oracle.NOTES
    world.upsert('notes', oracle._custom_row(NOTES_SESSION='vault:notes_session'))
    world.browser.jar = [oracle._cookie('sid', oracle.NOTES, '.notes.example')]
    api.learn('notes')
    bound = sorted(api.bindings('notes'))
    leaked = 'vault:' in repr(world.browser.of('match_cookies'))
    world.browser.jar[0]['value'] = oracle.NOTES_ROTATED
    changed = api.refresh('notes')['changed']
    return {'bound': bound, 'leaked': leaked, 'changed': changed, 'vault': dict(world.vault),
            'env': world.row('notes')['env'], 'after': sorted(api.bindings('notes')),
            'stored': world.row('notes')['browser_credentials']}


def _scenario_self_hosted(world, api):
    world.upsert('overleaf', oracle._overleaf_row(STALE, OVERLEAF_BASE_URL='https://latex.example.edu'))
    return {'reason': api.refresh('overleaf')['reason'], 'commands': world.browser.commands}


def _scenario_unbound(world, api):
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
    return {'reason': api.refresh('notes')['reason'], 'commands': world.browser.commands}


REFRESH_SCENARIOS = {
    'declared': _scenario_declared, 'vault': _scenario_vault, 'plaintext': _scenario_plaintext,
    'learned_vault': _scenario_learned_vault, 'self_hosted': _scenario_self_hosted,
    'unbound': _scenario_unbound,
}


def _both(worlds, rust_side, scenario):
    python_world = worlds('python').activate()
    python = scenario(python_world, PythonApi(python_world))
    rust_world = worlds('rust').activate()
    rust = scenario(rust_world, RustApi(rust_side(rust_world)))
    return python, rust


@pytest.mark.parametrize('name', sorted(REFRESH_SCENARIOS))
def test_refresh_scenarios_on_both(worlds, rust_side, name, caplog):
    python, rust = _both(worlds, rust_side, REFRESH_SCENARIOS[name])
    assert rust == python
    _no_secret(repr(rust.get('audits', [])))
    expected = {
        'declared': lambda r: r['outcome'] == ('renewed', ['OVERLEAF_SESSION']) and r['commands'] == [{
            'type': 'get_cookies', 'params': {'url': 'https://www.overleaf.com', 'name': 'overleaf_session2'},
            'client_id': 'live-device', 'owner': str(OWNER)}] and r['env'] == {
            'OVERLEAF_SESSION': FRESH, 'OVERLEAF_GIT_TOKEN': GIT_TOKEN} and r['audits'] == [(
                'mcp_credential_refreshed', {'server': 'overleaf', 'env_keys': ['OVERLEAF_SESSION'],
                                             'source': 'browser_cookie'})],
        'vault': lambda r: r['first'] == ('renewed', ['OVERLEAF_SESSION']) and r['second'] == 'current'
        and r['vault'] == {'overleaf_session': FRESH} and r['writes'] == ['overleaf_session']
        and r['env']['OVERLEAF_SESSION'] == 'vault:overleaf_session',
        'plaintext': lambda r: r == {'reason': 'renewed', 'session': FRESH, 'vault': {}, 'writes': []},
        'learned_vault': lambda r: r['bound'] == ['NOTES_SESSION'] and not r['leaked']
        and r['changed'] == ['NOTES_SESSION'] and r['vault'] == {'notes_session': oracle.NOTES_ROTATED}
        and r['env'] == {'NOTES_SESSION': 'vault:notes_session'} and r['after'] == ['NOTES_SESSION'],
        'self_hosted': lambda r: r['reason'] == 'no_session'
        and r['commands'][0]['params']['url'] == 'https://latex.example.edu',
        'unbound': lambda r: r == {'reason': 'unbound', 'commands': []},
    }[name]
    assert expected(rust), rust


@pytest.mark.parametrize('case, reason', [
    ('no_device', 'no_browser'), ('read_denied', 'no_session'),
    ('command_error', 'no_session'), ('no_cookie', 'no_session'),
    ('control_chars', 'no_session')])
def test_unusable_browser_state_on_both(worlds, rust_side, case, reason):
    def scenario(world, api):
        world.upsert('overleaf', oracle._overleaf_row(STALE))
        if case == 'no_device':
            world.browser.devices = []
        elif case == 'read_denied':
            world.read_allowed = False
        elif case == 'command_error':
            world.browser.error = "Browser command 'get_cookies' timed out after 10s."
        elif case == 'no_cookie':
            world.browser.jar = []
        else:
            world.browser.jar[0]['value'] = 'abc\r\nInjected: header'
        return {'reason': api.refresh('overleaf')['reason'],
                'session': world.row('overleaf')['env']['OVERLEAF_SESSION'],
                'commands': world.browser.commands}
    python, rust = _both(worlds, rust_side, scenario)
    assert rust == python
    assert rust['reason'] == reason and rust['session'] == STALE
    if case in ('no_device', 'read_denied'):
        assert rust['commands'] == []


def test_an_unreachable_bridge_owner_counts_as_no_browser_on_both(worlds, rust_side):
    from lib.browser.bridge_relay import RelayUnavailable

    def _unreachable(*, owner_user_id):
        raise RelayUnavailable('The browser bridge relay record is unreadable')

    def scenario(world, api):
        world.upsert('overleaf', oracle._overleaf_row(STALE))
        world.monkeypatch.setattr(browser_queue, 'get_connected_clients', _unreachable)
        return api.refresh('overleaf')['reason']
    python, rust = _both(worlds, rust_side, scenario)
    assert rust == python == 'no_browser'


def _learn_session_cookie(world, api):
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES, NOTES_USER='your-username'))
    world.browser.jar = [oracle._cookie('sid', oracle.NOTES, '.notes.example'),
                         oracle._cookie('user', 'your-username', '.notes.example', http_only=False)]
    answered = api.learn('notes')
    named = 'NOTES_SESSION' in repr(world.browser.of('match_cookies'))
    entry = dict(world.row('notes')['browser_credentials']['NOTES_SESSION'])
    world.browser.jar[0]['value'] = oracle.NOTES_ROTATED
    outcome = api.refresh('notes')
    return {'answered': answered, 'match': _match_shape(world.browser), 'named': named, 'entry': entry,
            'changed': outcome['changed'], 'env': world.row('notes')['env'],
            'bound': sorted(api.bindings('notes'))}


def _learn_header(world, api):
    header = f'a1={oracle.A1}; webId=abc123; web_session={oracle.WEB_SESSION}'
    world.upsert('xhs', oracle._custom_row(XHS_COOKIE=header))
    world.browser.jar = [
        oracle._cookie('web_session', oracle.WEB_SESSION, '.xiaohongshu.com'),
        oracle._cookie('a1', oracle.A1, '.xiaohongshu.com', http_only=False),
        oracle._cookie('webId', 'abc123', '.xiaohongshu.com', http_only=False)]
    api.learn('xhs')
    binding = api.bindings('xhs')['XHS_COOKIE']
    world.browser.jar = [oracle._cookie('web_session', oracle.WEB_SESSION_ROTATED, '.xiaohongshu.com'),
                         oracle._cookie('a1', oracle.A1, '.xiaohongshu.com', http_only=False)]
    renewed = api.refresh('xhs')['changed']
    after = world.row('xhs')['env']['XHS_COOKIE']
    world.browser.jar = [oracle._cookie('a1', oracle.A1, '.xiaohongshu.com', http_only=False)]
    signed_out = api.refresh('xhs')['reason']
    return {'binding': binding, 'renewed': renewed, 'after': after, 'signed_out': signed_out,
            'final': world.row('xhs')['env']['XHS_COOKIE'], 'match': _match_shape(world.browser)}


def _learn_companions(world, api):
    world.upsert('bili', oracle._custom_row(SESSDATA=oracle.SESSDATA, BILI_JCT=oracle.BILI_JCT,
                                            TRACKER=oracle.TRACKER))
    world.browser.jar = [oracle._cookie('SESSDATA', oracle.SESSDATA, '.bilibili.com'),
                         oracle._cookie('bili_jct', oracle.BILI_JCT, '.bilibili.com', http_only=False),
                         oracle._cookie('_uuid', oracle.TRACKER, '.analytics.example', http_only=False)]
    api.learn('bili')
    return {'bindings': api.bindings('bili'), 'match': _match_shape(world.browser)}


def _learn_ambiguous(world, api):
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
    world.browser.jar = [oracle._cookie('sid', oracle.NOTES, '.a.example'),
                         oracle._cookie('token', oracle.NOTES, '.b.example')]
    return {'answered': bool(api.learn('notes')), 'bindings': api.bindings('notes')}


def _learn_changed_elsewhere(world, api):
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
    world.browser.jar = [oracle._cookie('sid', oracle.NOTES, '.notes.example')]
    api.learn('notes')
    before = api.bindings('notes')
    row = world.row('notes')
    row['env']['NOTES_SESSION'] = 'an-api-key-the-user-pasted-0000000000'
    world.upsert('notes', row)
    return {'before': before, 'after': api.bindings('notes')}


def _learn_capable_only(world, api):
    world.browser.devices[1]['capabilities'] = []
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
    world.browser.jar = [oracle._cookie('sid', oracle.NOTES, '.notes.example')]
    api.learn('notes')
    world.browser.jar[0]['value'] = oracle.NOTES_ROTATED
    api.refresh('notes')
    return {'clients': [c['client_id'] for c in world.browser.commands],
            'sticky': select_owner_client(str(OWNER), world.browser.devices)}


def _learn_no_capable(world, api):
    world.browser.devices = [{'client_id': 'old-extension', 'last_poll': 1.0, 'capabilities': []}]
    world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
    return {'answered': api.learn('notes'), 'commands': world.browser.commands}


LEARNING_SCENARIOS = {
    'session_cookie': _learn_session_cookie, 'cookie_header': _learn_header,
    'companions': _learn_companions, 'ambiguous': _learn_ambiguous,
    'changed_elsewhere': _learn_changed_elsewhere, 'capable_only': _learn_capable_only,
    'no_capable_device': _learn_no_capable,
}


@pytest.mark.parametrize('name', sorted(LEARNING_SCENARIOS))
def test_learning_scenarios_on_both(worlds, rust_side, name, caplog):
    caplog.set_level(logging.DEBUG)
    python, rust = _both(worlds, rust_side, LEARNING_SCENARIOS[name])
    assert rust == python
    expected = {
        'session_cookie': lambda r: len(r['match']) == 1 and len(r['match'][0][1]) == 1
        and r['match'][0][0] == 'live-device' and not r['named'] and len(r['answered']) == 1
        and {k: r['entry'][k] for k in ('kind', 'name', 'url', 'client_id')} == {
            'kind': 'cookie', 'name': 'sid', 'url': 'https://notes.example/', 'client_id': 'live-device'}
        and r['changed'] == ['NOTES_SESSION'] and r['env']['NOTES_SESSION'] == oracle.NOTES_ROTATED
        and r['bound'] == ['NOTES_SESSION'],
        'cookie_header': lambda r: {k: r['binding'][k] for k in ('kind', 'url', 'names', 'required')} == {
            'kind': 'cookie_header', 'url': 'https://xiaohongshu.com/',
            'names': ['a1', 'webId', 'web_session'], 'required': ['a1', 'web_session']}
        and r['renewed'] == ['XHS_COOKIE']
        and r['after'] == f'a1={oracle.A1}; web_session={oracle.WEB_SESSION_ROTATED}'
        and r['signed_out'] == 'no_session' and r['final'] == r['after'],
        'companions': lambda r: sorted(r['bindings']) == ['BILI_JCT', 'SESSDATA']
        and r['bindings']['BILI_JCT']['name'] == 'bili_jct',
        'ambiguous': lambda r: r == {'answered': True, 'bindings': {}},
        'changed_elsewhere': lambda r: 'NOTES_SESSION' in r['before'] and r['after'] == {},
        'capable_only': lambda r: r == {'clients': ['older-device', 'older-device'], 'sticky': 'live-device'},
        'no_capable_device': lambda r: r == {'answered': [], 'commands': []},
    }[name]
    assert expected(rust), rust
    _no_secret(caplog.text)


def test_failure_notice_matches_python(worlds, rust_side):
    world = worlds('rust').activate()
    rust = rust_side(world)
    for row in (oracle._overleaf_row(STALE), oracle._overleaf_row(STALE, OVERLEAF_BASE_URL='https://latex.example.edu'),
                oracle._custom_row(NOTES_SESSION=oracle.NOTES)):
        for renewed, reason in ((True, 'renewed'), (False, 'no_browser'), (False, 'no_session')):
            name = 'overleaf' if 'OVERLEAF_SESSION' in row['env'] else 'notes'
            assert rust.credentials('failure_notice', name=name, row=row, renewed=renewed, reason=reason) == \
                browser_credentials.failure_notice(name, row, renewed=renewed, reason=reason)


# ── The bridge: renew after a probe, a rejected call, before a call ──────────


def _strip_error(text):
    return text[len('MCP Error: '):] if text.startswith('MCP Error: ') else text


def _python_bridge_run(world, setup):
    """Run one bridge scenario through the oracle tests' stubbed bridge."""
    from lib.mcp.stdio_environment import resolve_vault_env_references
    world.make_row = oracle._overleaf_row
    spec = setup(world)
    bridge, _, calls = oracle._bridge(spec['live'], **spec['bridge'])
    connects, fake_connect = [], bridge.connect_server

    def _connect(name, srv_cfg):
        # The session the started child holds (what the Rust side's server logs).
        connects.append(resolve_vault_env_references(srv_cfg['env'])['OVERLEAF_SESSION'])
        return fake_connect(name, srv_cfg)
    bridge.connect_server = _connect
    observed = {}
    if spec['action'] == 'probe':
        observed['result'] = bridge._run_cred_probe('overleaf')['status']
    elif spec['action'] == 'reconnect':
        bridge._reconnect_server('overleaf')
    else:
        result = bridge.call_tool(spec['action'], {})
        observed['result'] = (bool(getattr(result, 'is_error', False)), _strip_error(str(result)))
    health = bridge.get_cred_health('overleaf')
    observed.update(
        connects=connects,
        calls=list(calls), audits=[e for e, _ in world.audits],
        stored=(world.row('overleaf') or {}).get('env'),
        health=None if health is None else health['status'],
        get_cookies=_commands(world.browser), vault=dict(world.vault))
    return observed


def _rust_bridge_run(world, rust, setup):
    world.make_row = rust.row
    spec = setup(world)
    options = spec['bridge']
    rust.configure()
    rust.serve(errors=options.get('errors', False))
    rust.bridge.connect_server('overleaf', rust.row(spec['live']))
    rust.settle()
    params = {'owner_user_id': rust.owner, 'name': 'overleaf'}
    if options.get('parked'):
        rust.host.call('park', params, timeout=30)
    if options.get('verdict'):
        rust.credentials('record_health', name='overleaf', status=options['verdict'])
    starts_before = len(rust.starts())
    observed = {}
    if spec['action'] == 'probe':
        observed['result'] = rust.credentials('run_cred_probe', name='overleaf')['status']
    elif spec['action'] == 'reconnect':
        rust.host.call('reconnect_server', params, timeout=60)
    else:
        result = rust.bridge.call_tool(spec['action'], {})
        observed['result'] = (bool(getattr(result, 'is_error', False)), _strip_error(str(result)))
    rust.settle()
    health = rust.credentials('get_cred_health', name='overleaf')
    observed.update(
        connects=rust.starts()[starts_before:], calls=rust.calls(),
        audits=[e for e, _ in rust.audits()], stored=(world.row('overleaf') or {}).get('env'),
        health=None if health is None else health['status'],
        get_cookies=_commands(world.browser), vault=dict(world.vault))
    return observed


def _reference(world):
    world.vault['overleaf_session'] = STALE
    reference = 'vault:overleaf_session'
    world.upsert('overleaf', world.make_row(reference))
    return reference


def _bridge_scenarios():
    def store(session=STALE, **extra):
        def apply(world):
            world.upsert('overleaf', world.make_row(session, **extra))
        return apply

    def scenario(action, live=STALE, before=None, **bridge):
        def setup(world):
            store()(world)
            if before:
                before(world)
            live_session = live(world) if callable(live) else live
            return {'action': action, 'live': live_session, 'bridge': bridge}
        return setup

    def stale_browser(world):
        world.browser.jar[0]['value'] = STALE

    def no_devices(world):
        world.browser.devices = []

    def saved_elsewhere(world):
        world.browser.devices = []
        store(FRESH)(world)

    def disabled(world):
        mcp_config.patch_server('overleaf', {'enabled': False}, user_id=OWNER)

    def stale_and_signed_out(world):
        stale_browser(world)
        no_devices(world)

    return {
        'expired_probe_renews': scenario('probe'),
        'vault_session_renews_live': scenario(oracle.READ_TOOL, live=_reference, verdict='expired'),
        'vault_session_renews_parked': scenario(oracle.READ_TOOL, live=_reference, verdict='expired', parked=True),
        'vault_session_renews_after_probe': scenario('probe', live=_reference),
        'no_newer_credential': scenario('probe', before=stale_browser),
        'saved_elsewhere': scenario('probe', before=saved_elsewhere),
        'disabled_row': scenario('probe', before=disabled),
        'known_expired_live': scenario(oracle.READ_TOOL, verdict='expired'),
        'known_expired_parked': scenario(oracle.READ_TOOL, verdict='expired', parked=True),
        'working_credential': scenario(oracle.READ_TOOL, live=FRESH, before=store(FRESH), verdict='ok',
                                       errors=True),
        'wake_uses_stored': scenario(oracle.READ_TOOL, before=store(FRESH), parked=True),
        'rejected_read_repeats': scenario(oracle.READ_TOOL, errors=True),
        'rejected_write_notice': scenario(oracle.WRITE_TOOL, errors=True),
        'no_newer_session_notice': scenario(oracle.READ_TOOL, before=stale_browser, errors=True),
        'no_browser_notice': scenario(oracle.READ_TOOL, before=stale_and_signed_out, errors=True),
        'auth_words_in_success': scenario(oracle.READ_TOOL),
    }


BRIDGE_SCENARIOS = _bridge_scenarios()


@pytest.mark.parametrize('name', sorted(BRIDGE_SCENARIOS))
def test_bridge_scenarios_on_both(worlds, rust_side, monkeypatch, name, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(bridge_module, 'MCP_CRED_PROBE_INTERVAL', -1)
    python_world = worlds('python').activate()
    python = _python_bridge_run(python_world, BRIDGE_SCENARIOS[name])
    rust_world = worlds('rust').activate()
    rust = _rust_bridge_run(rust_world, rust_side(rust_world), BRIDGE_SCENARIOS[name])
    rust_calls = rust.pop('calls')
    python_calls = python.pop('calls')
    assert rust == python
    assert rust_calls == python_calls
    _no_secret(caplog.text + repr(rust_world.audits))
    renewed = ['mcp_credential_expired', 'mcp_credential_refreshed', 'mcp_reconnect']
    ok_read = (False, OK_TEXT)
    expected = {
        'expired_probe_renews': dict(result='ok', connects=[FRESH], audits=renewed, health='ok'),
        'vault_session_renews_live': dict(result=ok_read, connects=[FRESH], health=None),
        'vault_session_renews_parked': dict(result=ok_read, connects=[FRESH], health=None),
        'vault_session_renews_after_probe': dict(result='ok', connects=[FRESH]),
        'no_newer_credential': dict(result='expired', connects=[], audits=['mcp_credential_expired']),
        'saved_elsewhere': dict(result='ok', connects=[FRESH],
                                audits=['mcp_credential_expired', 'mcp_reconnect']),
        'disabled_row': dict(result='expired', connects=[]),
        'known_expired_live': dict(result=ok_read, connects=[FRESH], health=None),
        'known_expired_parked': dict(result=ok_read, connects=[FRESH], health=None),
        'working_credential': dict(result=ok_read, connects=[], get_cookies=[]),
        'wake_uses_stored': dict(result=ok_read, connects=[FRESH], get_cookies=[]),
        'rejected_read_repeats': dict(result=ok_read, connects=[FRESH], audits=renewed),
        'rejected_write_notice': dict(connects=[FRESH]),
        'no_newer_session_notice': dict(connects=[], health='expired'),
        'no_browser_notice': dict(connects=[], health='expired'),
        'auth_words_in_success': dict(result=(False, EXPIRED_TEXT), connects=[], get_cookies=[], health=None),
    }[name]
    assert {k: rust[k] for k in expected} == expected
    if name == 'rejected_read_repeats':
        assert rust_calls == [STALE, FRESH]
    if name == 'rejected_write_notice':
        assert rust_calls == [STALE] and rust['result'][0]
        assert rust['result'][1].startswith(EXPIRED_TEXT) and rust['result'][1].endswith('repeat the call.')
    if name in ('no_newer_session_notice', 'no_browser_notice'):
        action = ('sign in to www.overleaf.com in the browser connected to Tofu' if name == 'no_newer_session_notice'
                  else 'connect the Tofu browser extension and sign in to www.overleaf.com')
        assert action in rust['result'][1] and 'Do not ask the user for cookie values' in rust['result'][1]
    if name.startswith('vault_session'):
        assert rust['stored']['OVERLEAF_SESSION'] == 'vault:overleaf_session'
        assert rust['vault'] == {'overleaf_session': FRESH}


def test_failed_renewal_reconnect_keeps_the_expired_verdict_on_both(worlds, rust_side, monkeypatch):
    monkeypatch.setattr(bridge_module, 'MCP_CRED_PROBE_INTERVAL', -1)
    python_world = worlds('python').activate()
    python_world.upsert('overleaf', oracle._overleaf_row(STALE))
    bridge, _, _ = oracle._bridge(STALE)

    def _broken_connect(name, srv_cfg):
        raise RuntimeError('server failed to start')
    bridge.connect_server = _broken_connect
    python = (bridge._run_cred_probe('overleaf')['status'],
              python_world.row('overleaf')['env']['OVERLEAF_SESSION'])

    rust_world = worlds('rust').activate()
    rust = rust_side(rust_world)
    rust_world.upsert('overleaf', rust.row(STALE))
    rust.bridge.connect_server('overleaf', rust.row(STALE))
    rust.settle()
    rust.shim.write_text('#!/bin/sh\nexit 3\n', encoding='utf-8')
    # The renewed row's launcher cannot start: the reconnect fails.
    observed = (rust.credentials('run_cred_probe', name='overleaf')['status'],
                rust_world.row('overleaf')['env']['OVERLEAF_SESSION'])
    assert observed == python == ('expired', FRESH)


def test_a_failed_connect_renews_before_the_next_attempt_on_both(worlds, rust_side, monkeypatch):
    monkeypatch.setattr(bridge_module, 'MCP_CRED_PROBE_INTERVAL', -1)
    python_world = worlds('python').activate()
    python_world.upsert('overleaf', oracle._overleaf_row(STALE))
    bridge, connects, _ = oracle._bridge(STALE, parked=True, failing=True)
    bridge._reconnect_server('overleaf')
    python = [row['env']['OVERLEAF_SESSION'] for row in connects]

    rust_world = worlds('rust').activate()
    rust = rust_side(rust_world)
    rust_world.upsert('overleaf', rust.row(STALE))
    rust.bridge.connect_server('overleaf', rust.row(STALE))
    rust.settle()
    params = {'owner_user_id': OWNER, 'name': 'overleaf'}
    rust.host.call('park', params, timeout=30)
    script = rust.shim.read_text(encoding='utf-8')
    rust.shim.write_text('#!/bin/sh\nexit 3\n', encoding='utf-8')
    from lib.mcp.rust_host import RustHostError
    with pytest.raises(RustHostError):
        rust.host.call('reconnect_server', params, timeout=60)
    assert rust.host.call('breaker_state', params, timeout=30)['failures'] == 1
    assert _commands(rust_world.browser) == []  # a healthy credential is not refreshed
    rust.shim.write_text(script, encoding='utf-8')
    starts = len(rust.starts())
    rust.host.call('reconnect_server', params, timeout=60)
    rust.settle()
    assert rust.starts()[starts:] == python == [FRESH]
    assert rust_world.row('overleaf')['env']['OVERLEAF_SESSION'] == FRESH


def test_learning_pass_asks_once_per_value_on_both(worlds, rust_side):
    def scenario(world, side):
        world.upsert('notes', oracle._custom_row(NOTES_SESSION=oracle.NOTES))
        world.browser.jar = [oracle._cookie('user', oracle.NOTES, '.notes.example', http_only=False)]
        asked = []
        if side is None:
            bridge = bridge_module.MCPBridge(owner_user_id=OWNER)
            for _ in range(2):
                assert bridge._learn_browser_credentials_async('notes')
                deadline = time.monotonic() + 5
                while bridge._credential_learning_inflight:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                asked.append(len(world.browser.of('match_cookies')))
        else:
            for _ in range(2):
                assert side.credentials('learn', name='notes') is True
                asked.append(len(world.browser.of('match_cookies')))
        return {'asked': asked, 'bindings': credential_bindings_python(world, 'notes')}
    python_world = worlds('python').activate()
    python = scenario(python_world, None)
    rust_world = worlds('rust').activate()
    rust = scenario(rust_world, rust_side(rust_world))
    assert rust == python == {'asked': [2, 2], 'bindings': {}}


# ── Catalog facts: online docs entries and the reviewed-read override ────────


def test_catalog_probe_specs_on_both(worlds, rust_side):
    rust = rust_side(worlds('rust').activate(), owner=None)
    for server in ('kdocs', 'tencent-docs', 'overleaf'):
        rust.bridge.connect_server(server, {'command': str(rust.shim), 'args': []})
        expected = validate_health_probe((get_catalog_entry(server) or {}).get('health_probe'), server=server)
        assert rust.credentials('cred_probe_spec', name=server) == expected
    assert rust.credentials('cred_probe_spec', name='tencent-docs') is None
    assert rust.credentials('cred_probe_spec', name='kdocs')['tool'] == 'list_latest_items'


def _kdocs_config(**overrides):
    from lib.mcp import registry as reg
    config = reg.build_server_config('kdocs', {'KDOCS_TOKEN': 'not-a-real-token-0000'})
    config.update(overrides)
    return config


def test_reviewed_read_only_rule_matches_python(host):
    from lib.mcp import registry as reg
    cases = [('kdocs', _kdocs_config()), ('kdocs', _kdocs_config(url='https://example.invalid/mcp')),
             ('kdocs', _kdocs_config(transport='sse')), ('kdocs', _kdocs_config(transport='http')),
             ('tencent-docs', reg.build_server_config('tencent-docs', {'TENCENT_DOCS_TOKEN': 'x'})),
             ('no-such-server', {}), ('kdocs', 'not-a-dict')]
    for server, config in cases:
        rust = host.call('credentials.reviewed_read_only', {'owner_user_id': None, 'name': server,
                                                             'config': config}, timeout=30)
        assert set(rust) == set(reg.reviewed_read_only_tools(server, config)), (server, config)
    assert 'sheet.get_range_data' in host.call('credentials.reviewed_read_only', {
        'owner_user_id': None, 'name': 'kdocs', 'config': _kdocs_config()}, timeout=30)


def test_bridge_folds_the_review_into_the_read_partition_on_both(host, tmp_path, monkeypatch):
    """Both clients classify the catalog a kdocs endpoint lists: the review adds
    read-only classification only at the catalog endpoint, and server
    annotations stay the vendor's own evidence."""
    from types import SimpleNamespace

    from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle

    def tool(name, read_only=False):
        return SimpleNamespace(name=name, description=name,
                               inputSchema={'type': 'object', 'properties': {}},
                               annotations={'readOnlyHint': read_only, 'destructiveHint': True})
    tools = [tool('sheet.get_range_data'), tool('sheet.update_range_data'),
             tool('server_declared_read', read_only=True)]
    for config in (_kdocs_config(), _kdocs_config(url='https://example.invalid/mcp')):
        python = MCPBridge()
        handle = _MCPServerHandle('kdocs', config)
        python._servers['kdocs'] = handle
        python._replace_server_catalog('kdocs', handle, tools)
        expected = python.get_tool_safety()
        rows = [host.call('index_tool', {'server': 'kdocs', 'tool': {
            'name': t.name, 'description': t.description, 'inputSchema': t.inputSchema,
            'annotations': t.annotations}, 'catalog_version': ''}, timeout=30) for t in tools]
        reviewed = set(host.call('credentials.reviewed_read_only', {
            'owner_user_id': None, 'name': 'kdocs', 'config': config}, timeout=30))
        rust = {row['namespaced_name']: row['read_only_hint'] or row['tool_name'] in reviewed for row in rows}
        assert rust == expected, config
        assert all(row['annotations']['readOnlyHint'] is (row['tool_name'] == 'server_declared_read')
                   for row in rows)


# ── Registry env_specs, shared SSO and vault references reach the child ──────


INTERNAL = ('hope', 'llm', 'xuecheng')


@requires_yourprovider_overlay
def test_internal_env_specs_read_through_the_rust_catalog(host, scene):
    from lib.mcp import registry as reg
    if reg.is_opensource_build():
        pytest.skip('internal MCP launchers are stripped from opensource builds')
    for server in INTERNAL:
        rust = host.call('registry.catalog_entry', {'server_id': server}, timeout=30)
        assert json.loads(json.dumps(rust)) == json.loads(json.dumps(reg.get_catalog_entry(server)))
    specs = {s['key']: s for s in host.call('registry.catalog_entry', {'server_id': 'hope'}, timeout=30)['env_specs']}
    assert {o['value'] for o in specs['HOPE_MCP_TOOL_PROFILE']['options']} == {'core', 'extended', 'debug'}
    assert specs['HOPE_MCP_TOOL_PROFILE']['type'] == 'select'
    for server, values in (('hope', {'HOPE_USERNAME': 'u', 'HOPE_MCP_TOOL_PROFILE': 'debug',
                                     'HOPE_MCP_LOG_LEVEL': 'debug'}),
                           ('llm', {'LLM_MIS': 'u', 'LLM_MCP_CA_BUNDLE': '/tmp/internal-ca.pem'}),
                           ('xuecheng', {'XUECHENG_MIS': 'u', 'XUECHENG_TOOLSET': 'all',
                                         'HAND_ADDED_FUTURE_KEY': '1'})):
        config = reg.build_server_config(server, values)
        env = host.call('stdio_environment', {**scene.host_settings(), 'config': config,
                                              'owner_user_id': 7, 'process_env': {}}, timeout=30)
        for key, value in values.items():
            assert env[key] == value, (server, key)


@requires_yourprovider_overlay
def test_internal_sso_entries_reach_the_rust_child_environment(host, scene):
    from lib.mcp import registry as reg
    if reg.is_opensource_build():
        pytest.skip('internal providers are not exported')
    values = {'MTSSO_AUTH_MODE': 'shared', 'MTSSO_MIS': 'alice',
              'MTSSO_SESSION_SDK': '/installed/auth.js', 'MTSSO_SESSION_TOKEN_FILE': '/private/session.json'}
    for server in INTERNAL:
        specs = {s['key']: s for s in host.call('registry.catalog_entry', {'server_id': server},
                                                timeout=30)['env_specs']}
        assert {o['value'] for o in specs['MTSSO_AUTH_MODE']['options']} == {'auto', 'official', 'shared', 'legacy'}
        env = host.call('stdio_environment', {**scene.host_settings(), 'config': reg.build_server_config(
            server, values), 'owner_user_id': 7, 'process_env': {}}, timeout=30)
        for key, value in values.items():
            assert env[key] == value and specs[key]['secret'] is False and specs[key]['required'] is False


def test_stdio_cache_identity_cannot_be_overridden_on_both(host, scene, monkeypatch, tmp_path):
    from lib.mcp.rust_host import RustHostError
    from lib.mcp.stdio_environment import build_stdio_environment
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOFU_MCP_OWNER_ID', 'inherited-identity')
    monkeypatch.setattr('lib.mcp.client._vendor._ensure_writable_caches', lambda *a, **kw: None)
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_proxy_env', lambda *a, **kw: None)

    def rust(config, owner):
        return host.call('stdio_environment', {**scene.host_settings(), 'config': config,
                                               'owner_user_id': owner, 'process_env': dict(os.environ),
                                               'prepare_directories': False}, timeout=30)
    config = {'env': {'TOFU_MCP_OWNER_ID': 'wrong-owner', 'MTSSO_MIS': 'alice'}}
    python = build_stdio_environment(config, owner_user_id=7, prepare_directories=False)
    assert rust(config, 7)['TOFU_MCP_OWNER_ID'] == python['TOFU_MCP_OWNER_ID'] == 'tofu:7'
    assert 'TOFU_MCP_OWNER_ID' not in rust({}, None)
    with pytest.raises(ValueError) as python_error:
        build_stdio_environment(config, prepare_directories=False)
    with pytest.raises(RustHostError) as rust_error:
        rust(config, None)
    assert str(rust_error.value) == str(python_error.value)
    for owner in (True, 0, -1, '7'):
        with pytest.raises(ValueError):
            build_stdio_environment({}, owner_user_id=owner, prepare_directories=False)
        with pytest.raises(RustHostError) as refused:
            rust({}, owner)
        assert refused.value.kind == 'invalid'


def test_vault_references_resolve_like_python(host, scene, monkeypatch):
    from lib.mcp.rust_host import RustHostError
    from lib.mcp.stdio_environment import resolve_vault_env_references
    vault = {'github_token': 'secret-token-value'}
    monkeypatch.setattr(credentials_vault, 'get_entry', vault.get)
    monkeypatch.setenv('HOME', '/tmp')

    def rust(env):
        return host.call('stdio_environment', {**scene.host_settings(), 'vault': vault,
                                               'config': {'command': 'npx', 'env': env}, 'owner_user_id': None,
                                               'process_env': {'HOME': '/tmp'}, 'prepare_directories': False},
                         timeout=30)
    overlay = {'A': 'literal', 'B': 'not-a-reference:vault'}
    assert resolve_vault_env_references(overlay) is overlay
    assert {k: rust(overlay)[k] for k in overlay} == overlay
    resolved = {'GITHUB_PERSONAL_ACCESS_TOKEN': 'vault:github_token', 'UNCHANGED': 'literal'}
    python = resolve_vault_env_references(resolved)
    assert {k: rust(resolved)[k] for k in resolved} == python
    assert python['GITHUB_PERSONAL_ACCESS_TOKEN'] == 'secret-token-value'
    for env in ({'TOKEN': 'vault:absent_entry'}, {'TOKEN': 'vault:../../etc/passwd'}):
        with pytest.raises(ValueError) as python_error:
            resolve_vault_env_references(env)
        with pytest.raises(RustHostError) as rust_error:
            rust(env)
        assert str(rust_error.value) == str(python_error.value)
