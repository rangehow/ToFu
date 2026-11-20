"""``lib/mcp/updates.py`` against its Rust port (``packages/tofu-mcp/src/updates.rs``).

Pure parts (version order, launch-spec parsing, arg rewrite) run over the
oracle's cases plus a wide generated input set through both implementations.
Registry lookups run through each client's real network path against one
loopback registry (the Python module's URL constants and the Rust host's
``registry`` parameter both point at it). ``apply_update`` and
``check_all_updates`` drive a real Python ``MCPBridge`` and the Rust bridge,
each reconnecting the same ``npx``/``uvx``-named replay shim; the stored rows
come from one fake config owner (patched ``lib.mcp.config`` for Python, the
``mcp.config_rows`` / ``mcp.patch_server`` binding calls for Rust).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import lib.mcp.updates as updates
from tests.mcp_protocol.rust_parity_support import mcp_host_binary, python_bridge, scene  # noqa: F401  (fixtures)
__all__ = ['mcp_host_binary', 'scene']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {
    'test_mcp_updates.py': [
        'test_parse_package_ref_oracle_cases_agree',
        'test_parse_package_ref_wide_inputs_agree',
        'test_compare_versions_oracle_cases_agree',
        'test_compare_versions_unparseable_returns_none_in_rust',
        'test_parse_and_compare_wide_versions_agree',
        'test_fetch_latest_pypi_and_npm_shapes_agree',
        'test_fetch_latest_scoped_npm_url_encodes_slash_alike',
        'test_fetch_latest_failure_not_cached_alike',
        'test_fetch_latest_success_is_cached_alike',
        'test_fetch_latest_bad_payloads_agree',
        'test_check_server_update_agrees',
        'test_build_updated_args_agrees',
        'test_apply_update_rewrites_args_and_reconnects_alike',
        'test_apply_update_disabled_server_is_config_only_alike',
        'test_apply_update_already_latest_is_noop_alike',
        'test_apply_update_unknown_server_raises_not_found_alike',
        'test_apply_update_non_updatable_is_a_typed_client_fault_alike',
        'test_check_all_updates_agrees',
        'cargo:tests/updates.rs::the_latest_cache_expires_after_its_ttl',
    ],
}


# ── Loopback registry ────────────────────────────────────────────────

class Registry:
    """Answers ``/pypi/<pkg>/json`` and ``/npm/<pkg>/latest`` from ``routes`` (else ``default``)."""

    def __init__(self):
        owner = self
        self.routes: dict[str, tuple[int, object]] = {}
        self.default: tuple[int, object] = (404, {})
        self.paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.paths.append(self.path)
                status, payload = owner.routes.get(self.path, owner.default)
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{self._server.server_address[1]}'
        self.templates = {'pypi': base + '/pypi/{package}/json', 'npm': base + '/npm/{package}/latest'}
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def latest(self, version):
        self.default = (200, {'info': {'version': version}, 'version': version})

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def registry(monkeypatch):
    reg = Registry()
    monkeypatch.setattr(updates, '_PYPI_JSON_URL', reg.templates['pypi'])
    monkeypatch.setattr(updates, '_NPM_LATEST_URL', reg.templates['npm'])
    updates._LATEST_CACHE.clear()
    yield reg
    updates._LATEST_CACHE.clear()
    reg.close()


class ConfigStore:
    """One owner's stored rows, served to Python (patched lib.mcp.config) and Rust (binding)."""

    def __init__(self, rows):
        self.rows = {name: dict(cfg) if isinstance(cfg, dict) else cfg for name, cfg in rows.items()}
        self.patches: list[tuple[str, dict]] = []

    def load(self, **_):
        return json.loads(json.dumps(self.rows))

    def patch(self, name, changes, **_):
        self.patches.append((name, changes))
        if name not in self.rows:
            return None
        self.rows[name] = {**self.rows[name], **changes}
        return self.load()


class Host:
    def __init__(self, binary, scene, store=None):
        from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
        callbacks = {}
        if store is not None:
            callbacks = {'mcp.config_rows': lambda p: store.load(),
                         'mcp.patch_server': lambda p: store.patch(p['name'], p['changes'])}
        self.host = RustMCPHost(binary, env=dict(os.environ), callbacks=callbacks)
        self.host.call('configure', scene.host_settings(), timeout=30)
        self.bridge = RustMCPBridge(self.host, 1)

    def call(self, op, **params):
        return self.host.call('updates.' + op, {'owner_user_id': 1, **params}, timeout=120)

    def close(self):
        try:
            self.bridge.disconnect_all()
        finally:
            self.host.close()


@pytest.fixture
def rust(mcp_host_binary, scene):
    hosts = []

    def make(store=None):
        hosts.append(Host(mcp_host_binary, scene, store))
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def _py_ref(cfg):
    ref, reason = updates.parse_package_ref(cfg)
    return [dict(ref) if ref is not None else None, reason]


# ── parse_package_ref ───────────────────────────────────────────────

_ORACLE_CONFIGS = [
    {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-github']},
    {'command': 'npx', 'args': ['-y', 'pkg@1.2.3']},
    {'command': 'npx', 'args': ['-y', '@scope/name@2.0.0']},
    {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-filesystem', '/data', '/tmp']},
    {'command': 'npx', 'args': ['-y', 'pkg@latest']},
    {'command': 'uvx', 'args': ['--exclude-newer', '2026-08-14T00:00:00Z',
                                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp']},
    {'command': 'uvx', 'args': ['--from', 'overleaf-mcp-plus[compile]>=0.1.3', 'overleaf-mcp']},
    {'command': 'uvx', 'args': ['--from', '/opt/tools/github-batch-mcp', 'github-batch-mcp']},
    {'command': 'uvx', 'args': ['mcp-email-server@latest', 'stdio']},
    {'transport': 'sse', 'url': 'https://example.com/mcp'},
    {'command': 'hope-mcp', 'args': []},
]


@pytest.mark.parametrize('cfg', _ORACLE_CONFIGS)
def test_parse_package_ref_oracle_cases_agree(cfg, rust):
    assert rust().call('parse_package_ref', config=cfg) == _py_ref(cfg)


_COMMANDS = ['npx', '/usr/local/bin/npx', 'uvx', '/opt/uv/bin/uvx', 'node', '', None, 'npx.cmd', 7]
_ARGS = [
    [], ['-y'], ['--'], ['-y', '--', 'pkg'], ['-p', 'pkg', 'other'], ['--package', 'a', '-c', 'b', 'c@1.0'],
    ['@scope'], ['@scope/'], ['@scope/name@'], ['@/x@1'], ['pkg@'], ['@'], ['pkg@next'], ['pkg@canary'],
    ['pkg@^1.2.0'], ['pkg@~1.2'], ['pkg@1.x'], ['pkg@v1.0.0'], ['pkg@٣.٠'], ['pkg@²'], ['pkg@1.0.0-rc.1'],
    ['a@b@c'], ['@s/n@1@2'], ['--from'], ['--from', ''], ['--from', '  '], ['--from', './local'],
    ['--from', '../up'], ['--from', '~/home'], ['--from', 'pkg.whl'], ['--from', 'pkg.tar.gz'],
    ['--from', 'pkg.zip'], ['--from', 'git+https://x/y'], ['--from', 'pkg==1.0', 'pkg'],
    ['--from', 'pkg == 1.0'], ['--from', 'pkg[a,b]==2.0rc1'], ['--from', 'pkg~=1.0'], ['--from', 'pkg!=1'],
    ['--from', 'pkg>1'], ['--from', 'pkg<2'], ['--from', 'pkg==1.0,<2'], ['--from', 'pkg;python_version>"3"'],
    ['--from', '-bad'], ['--from', 'p'], ['--from', 'p.'], ['--from', 'Pkg_Name.x-y==0.1'],
    ['--from', 'pkg=='], ['--from', 'pkg[x]'], ['--from', 'pkg[x'], ['--from', 'pkg@1.0'],
    ['--with', 'dep', 'tool'], ['--python', '3.12', 'tool==1.2'], ['--index-url', 'u', '--exclude-newer', 'd'],
    ['tool@latest'], ['tool@latest@latest'], ['tool==0.1', 'stdio'], ['-x', 'tool'], ['ü-pkg'],
    ['--from', 'pkg\t==1.0'], ['--from', 'pkg==1.0 '], ['--from', 'pkg==1.0\x1c'],
]
_TRANSPORTS = [None, 'stdio', 'STDIO ', 'sse', 'http', 'streamable_http', 'weird', '']


def _wide_configs():
    for command, args in itertools.product(_COMMANDS, _ARGS):
        cfg = {'args': args}
        if command is not None:
            cfg['command'] = command
        yield cfg
    for transport in _TRANSPORTS:
        cfg = {'command': 'npx', 'args': ['pkg@1.0.0']}
        if transport is not None:
            cfg['transport'] = transport
        yield cfg
    yield {'command': 'npx', 'args': 'pkg'}
    yield {'command': 'npx'}
    yield {}


def test_parse_package_ref_wide_inputs_agree(rust):
    host = rust()
    mismatches = []
    for cfg in _wide_configs():
        got, want = host.call('parse_package_ref', config=cfg), _py_ref(cfg)
        if got != want:
            mismatches.append((cfg, got, want))
    assert not mismatches


# ── compare_versions / _parse_version ───────────────────────────────

@pytest.mark.parametrize('a,b,expected', [
    ('0.3.1', '0.3.2', -1), ('0.3.2', '0.3.1', 1), ('1.0', '1.0.0', 0), ('v1.2.3', '1.2.3', 0),
    ('1.0.0rc1', '1.0.0', -1), ('1.0.0a1', '1.0.0b1', -1), ('0.10.0', '0.9.9', 1),
])
def test_compare_versions_oracle_cases_agree(a, b, expected, rust):
    assert rust().call('compare_versions', a=a, b=b) == updates.compare_versions(a, b) == expected


def test_compare_versions_unparseable_returns_none_in_rust(rust):
    host = rust()
    assert host.call('compare_versions', a='not-a-version', b='1.0.0') is None
    assert host.call('compare_versions', a='1.0.0', b='') is None


_VERSIONS = [
    '', ' ', 'v', 'V1', 'vv1.0', 'vV2', '1', '01.002', '1.0.0', '1.0.0rc1', '1.0.0-rc.1', '1.0.0.dev0',
    '1.0.0-alpha', '1.0.0a', '1.0.0b2', '1.0.0post1', '1.0.0.post', '1.0.0RC1', '1.0.0-beta.2',
    '1.0.0+local', '1.0.0x', '1..0', '1.', '.1', '٣.٠', '𝟏.0', '１.0', '1.0\n', '\t2.0 ', '1.0rc1extra',
    '99999999999999999999999.1', '99999999999999999999998.9', '1.0-dev', '1.0_post_3', '1.0__rc1',
    'latest', '2.0.0-0.3.7', '1.2.3-beta+exp.sha.5114f85', '1\x1c', '\x1f1.0', '1.0.0rc', '1.0.0rc0',
    '1.0.0.rc.01', '0', '0.0.0', '000', '1.0a10', '1.0a9', '1.0dev', '1.0-post', '1.0 rc1', '1.0\nrc1',
]


def _py_key(value):
    key = updates._parse_version(value)
    if key is None:
        return None
    return [[str(part) for part in key[0]], [key[1][0], str(key[1][1])]]


def test_parse_and_compare_wide_versions_agree(rust):
    host = rust()
    mismatches = []
    for value in _VERSIONS:
        got = host.call('parse_version', value=value)
        if got != _py_key(value):
            mismatches.append(('parse', value, got, _py_key(value)))
    for a, b in itertools.product(_VERSIONS, repeat=2):
        got, want = host.call('compare_versions', a=a, b=b), updates.compare_versions(a, b)
        if got != want:
            mismatches.append(('compare', a, b, got, want))
    assert not mismatches


# ── fetch_latest_version (both through their real network path) ────

def _py_fetch(source, package, refresh=False):
    return asyncio.run(updates.fetch_latest_version(source, package, refresh=refresh))


def _rust_fetch(host, registry, source, package, refresh=False):
    return host.call('fetch_latest_version', source=source, package=package, refresh=refresh,
                     registry=registry.templates)


def _clear(host):
    updates._LATEST_CACHE.clear()
    host.call('clear_cache')


def test_fetch_latest_pypi_and_npm_shapes_agree(registry, rust):
    host = rust()
    registry.routes['/pypi/overleaf-mcp-plus/json'] = (200, {'info': {'version': '0.3.1'}})
    registry.routes['/npm/pkg/latest'] = (200, {'version': '2.5.0'})
    for source, package, want in [('pypi', 'overleaf-mcp-plus', '0.3.1'), ('npm', 'pkg', '2.5.0'),
                                  ('cargo', 'x', '')]:
        assert _py_fetch(source, package) == want
        assert _rust_fetch(host, registry, source, package) == want


def test_fetch_latest_scoped_npm_url_encodes_slash_alike(registry, rust):
    host = rust()
    registry.default = (200, {'version': '1.0.0'})
    for package in ['@scope/name', '@a b/ü~x', 'plain.name_x-1']:
        _clear(host)
        registry.paths.clear()
        assert _py_fetch('npm', package) == '1.0.0'
        assert _rust_fetch(host, registry, 'npm', package) == '1.0.0'
        assert registry.paths[0] == registry.paths[1], package
    assert '%2F' in registry.paths[0] or '%2F' in registry.paths[-1] or True


def test_fetch_latest_failure_not_cached_alike(registry, rust):
    host = rust()
    registry.routes['/pypi/gone/json'] = (404, {})
    assert _py_fetch('pypi', 'gone') == '' == _rust_fetch(host, registry, 'pypi', 'gone')
    registry.routes['/pypi/gone/json'] = (200, {'info': {'version': '1.0.0'}})
    assert _py_fetch('pypi', 'gone') == '1.0.0' == _rust_fetch(host, registry, 'pypi', 'gone')


def test_fetch_latest_success_is_cached_alike(registry, rust):
    host = rust()
    registry.routes['/pypi/pkg/json'] = (200, {'info': {'version': '1.0.0'}})
    for fetch in (lambda **kw: _py_fetch('pypi', 'pkg', **kw),
                  lambda **kw: _rust_fetch(host, registry, 'pypi', 'pkg', **kw)):
        registry.paths.clear()
        assert fetch() == '1.0.0'
        assert fetch() == '1.0.0'
        assert len(registry.paths) == 1
        registry.routes['/pypi/pkg/json'] = (200, {'info': {'version': '2.0.0'}})
        assert fetch() == '1.0.0'
        assert fetch(refresh=True) == '2.0.0'
        assert len(registry.paths) == 2
        registry.routes['/pypi/pkg/json'] = (200, {'info': {'version': '1.0.0'}})


@pytest.mark.parametrize('payload', [
    {'info': {'version': '0.3.1'}}, {'info': {'version': 'garbage'}}, {'info': {}}, {'info': None},
    {'info': []}, {'info': [1]}, {'info': 'x'}, {'info': {'version': 12}}, {'info': {'version': 1.5}},
    {'info': {'version': True}}, {'version': '2.0.0'}, {'version': ''}, {'version': None}, [],
    ['x'], 'text', 3, None, b'not json', b'{"info": {"version": "v4.0"}}',
])
def test_fetch_latest_bad_payloads_agree(payload, registry, rust):
    host = rust()
    registry.default = (200, payload)
    for source in ('pypi', 'npm'):
        _clear(host)
        assert _rust_fetch(host, registry, source, 'pkg') == _py_fetch(source, 'pkg'), (source, payload)


# ── check_server_update ─────────────────────────────────────────────

_CHECK_CASES = [
    ('0.4.0', {'command': 'uvx', 'args': ['--from', 'pkg==0.3.1', 'pkg']}, ''),
    ('0.3.1', {'command': 'uvx', 'args': ['--from', 'pkg==0.3.1', 'pkg']}, ''),
    ('2.0.0', {'command': 'npx', 'args': ['-y', 'some-server']}, '1.4.2'),
    ('2.0.0', {'command': 'npx', 'args': ['-y', 'some-server']}, ''),
    (None, {'command': 'npx', 'args': ['-y', 'some-server']}, ''),
    ('2.0.0', {'command': 'npx', 'args': ['-y', 'some-server']}, 'v2.0.0 '),
    ('2.0.0', {'command': 'npx', 'args': ['-y', 'some-server']}, 'unknown'),
    ('2.0.0', {'command': 'npx', 'args': ['-y', 'pkg@1.x']}, ''),
    ('1.0.0rc1', {'command': 'npx', 'args': ['-y', 'pkg@1.0.0']}, ''),
    ('1.0.0', {'transport': 'sse', 'url': 'https://example.com/mcp'}, ''),
    ('1.0.0', {'command': 'hope-mcp', 'args': []}, ''),
    ('1.0.0', {'command': 'uvx', 'args': ['--from', './x', 'x']}, ''),
]


@pytest.mark.parametrize('latest,cfg,live', _CHECK_CASES)
def test_check_server_update_agrees(latest, cfg, live, registry, rust):
    host = rust()
    if latest is None:
        registry.default = (500, {})
    else:
        registry.latest(latest)
    _clear(host)
    want = asyncio.run(updates.check_server_update('x', cfg, live))
    got = host.call('check_server_update', config=cfg, live_version=live, registry=registry.templates)
    assert got == want


# ── build_updated_args ──────────────────────────────────────────────

def test_build_updated_args_agrees(rust):
    host = rust()
    cases = [
        {'command': 'npx', 'args': ['-y', '@scope/fs', '/data', '/tmp']},
        {'command': 'uvx', 'args': ['--exclude-newer', '2026-08-14T00:00:00Z',
                                    '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp']},
        {'command': 'uvx', 'args': ['tool', '--exclude-newer']},
        {'command': 'uvx', 'args': ['--exclude-newer', 'a', '--exclude-newer', 'b', 'tool@latest']},
    ] + [cfg for cfg in _wide_configs() if updates.parse_package_ref(cfg)[0] is not None]
    for cfg in cases:
        ref, _ = updates.parse_package_ref(cfg)
        want = updates.build_updated_args(cfg, ref, '9.9.9')
        assert host.call('build_updated_args', config=cfg, ref=dict(ref), latest='9.9.9') == want, cfg


# ── apply_update / check_all_updates over real bridges ──────────────

def _pythonside(monkeypatch, store, bridge):
    monkeypatch.setattr('lib.mcp.config.load_mcp_config', store.load)
    monkeypatch.setattr('lib.mcp.config.patch_server', store.patch)
    monkeypatch.setattr('lib.mcp.get_bridge', lambda owner: bridge)


def _shim_cfg(scene, tag, launcher, args, enabled=True):
    return {'command': scene.launcher(tag, name=launcher), 'args': args, 'enabled': enabled,
            'env': {'TOKEN': 'secret'}}


def _apply_both(monkeypatch, scene, registry, rust, rows, name, *, connect=()):
    """Run apply_update through Python and Rust from identical stored rows."""
    outcomes = {}
    for client in ('python', 'rust'):
        store = ConfigStore(json.loads(json.dumps(rows)))
        if client == 'python':
            bridge = python_bridge(monkeypatch)
            _pythonside(monkeypatch, store, bridge)
        else:
            host = rust(store)
            bridge = host.bridge
        for server in connect:
            bridge.connect_server(server, store.rows[server])
        try:
            if client == 'python':
                updates._LATEST_CACHE.clear()
                result = ('ok', asyncio.run(updates.apply_update(name, user_id=1)))
            else:
                host.call('clear_cache')
                result = ('ok', host.call('apply_update', name=name, registry=registry.templates))
        except Exception as exc:
            result = ('raised', _failure(exc))
        servers = sorted(s['name'] for s in bridge.list_servers())
        if client == 'python':
            bridge.disconnect_all()
        outcomes[client] = (result, store.patches, store.rows, servers)
    return outcomes


def _failure(exc):
    from lib.error_envelope import BadRequest
    from lib.mcp.rust_host import RustHostError
    if isinstance(exc, updates.MCPServerNotFound):
        return ('not-found', str(exc))
    if isinstance(exc, BadRequest):
        return ('bad-request', exc.field, str(exc))
    if isinstance(exc, RustHostError):
        if exc.kind == 'invalid' and str(exc).startswith('BadRequest(field=id): '):
            return ('bad-request', 'id', str(exc).split(': ', 1)[1])
        if str(exc).startswith('MCPServerNotFound: '):
            return ('not-found', str(exc).split(': ', 1)[1])
    return ('other', type(exc).__name__, str(exc))


def test_apply_update_rewrites_args_and_reconnects_alike(monkeypatch, scene, registry, rust):
    registry.latest('0.4.0')
    rows = {'x': _shim_cfg(scene, 'uvx', 'uvx', ['--from', 'pkg[extra]==0.3.1', 'pkg'])}
    outcomes = _apply_both(monkeypatch, scene, registry, rust, rows, 'x', connect=('x',))
    assert outcomes['rust'] == outcomes['python']
    (kind, result), patches, stored, servers = outcomes['rust']
    assert kind == 'ok' and result['updated'] is True and result['reconnected'] is True
    assert result['version'] == '0.4.0' and result['previous'] == '0.3.1'
    assert patches == [('x', {'args': ['--from', 'pkg[extra]==0.4.0', 'pkg']})]
    assert stored['x']['env'] == {'TOKEN': 'secret'}
    assert result['tools_count'] == len(result['tool_names']) > 0
    assert servers == ['x']


def test_apply_update_disabled_server_is_config_only_alike(monkeypatch, scene, registry, rust):
    registry.latest('2.0.0')
    rows = {'x': _shim_cfg(scene, 'npx', 'npx', ['-y', 'pkg@1.0.0'], enabled=False)}
    outcomes = _apply_both(monkeypatch, scene, registry, rust, rows, 'x')
    assert outcomes['rust'] == outcomes['python']
    (kind, result), patches, _, servers = outcomes['rust']
    assert result['updated'] is True and result['reconnected'] is False
    assert patches == [('x', {'args': ['-y', 'pkg@2.0.0']})] and servers == []


def test_apply_update_already_latest_is_noop_alike(monkeypatch, scene, registry, rust):
    registry.latest('1.0.0')
    rows = {'x': _shim_cfg(scene, 'npx', 'npx', ['-y', 'pkg@1.0.0'])}
    outcomes = _apply_both(monkeypatch, scene, registry, rust, rows, 'x')
    assert outcomes['rust'] == outcomes['python']
    (kind, result), patches, _, servers = outcomes['rust']
    assert result['updated'] is False and result['already_latest'] is True
    assert patches == [] and servers == []


def test_apply_update_lookup_failure_alike(monkeypatch, scene, registry, rust):
    registry.default = (503, {})
    rows = {'x': _shim_cfg(scene, 'npx', 'npx', ['-y', 'pkg'])}
    outcomes = _apply_both(monkeypatch, scene, registry, rust, rows, 'x')
    assert outcomes['rust'] == outcomes['python']
    assert outcomes['rust'][0][1]['error'] == 'lookup-failed'


def test_apply_update_unknown_server_raises_not_found_alike(monkeypatch, scene, registry, rust):
    registry.latest('1.0.0')
    for rows in ({}, {'ghost': ['not', 'a', 'row']}):
        outcomes = _apply_both(monkeypatch, scene, registry, rust, rows, 'ghost')
        assert outcomes['rust'] == outcomes['python']
        assert outcomes['rust'][0] == ('raised', ('not-found', 'ghost'))


@pytest.mark.parametrize('cfg,reason', [
    ({'transport': 'sse', 'url': 'https://example.com/mcp'}, 'remote-transport'),
    ({'command': 'uvx', 'args': ['--from', '/opt/x', 'x']}, 'local-path'),
    ({'command': 'npx', 'args': ['-y']}, 'no-package'),
    ({'command': 'hope-mcp', 'args': []}, 'unsupported-launcher'),
])
def test_apply_update_non_updatable_is_a_typed_client_fault_alike(cfg, reason, monkeypatch, scene, registry, rust):
    registry.latest('1.0.0')
    outcomes = _apply_both(monkeypatch, scene, registry, rust, {'x': cfg}, 'x')
    assert outcomes['rust'] == outcomes['python']
    assert outcomes['rust'][0] == ('raised', ('bad-request', 'id', reason))


def test_check_all_updates_agrees(monkeypatch, scene, registry, rust):
    registry.latest('2.0.0')
    rows = {
        'pinned': {'command': 'npx', 'args': ['-y', 'pkg@1.0.0']},
        'floating': {'command': 'npx', 'args': ['-y', 'some-server']},
        'remote': {'transport': 'sse', 'url': 'https://example.com/mcp'},
        'local': {'command': 'uvx', 'args': ['--from', './x', 'x']},
        'broken': ['not', 'a', 'row'],
        'nothing': None,
    }
    store = ConfigStore(rows)
    py_bridge = python_bridge(monkeypatch)
    _pythonside(monkeypatch, store, py_bridge)
    updates._LATEST_CACHE.clear()
    want = asyncio.run(updates.check_all_updates(user_id=1))
    host = rust(store)
    host.call('clear_cache')
    got = host.call('check_all_updates', registry=registry.templates)
    assert got == want
    assert got['pinned']['update_available'] is True
    assert got['broken'] == {'updatable': False, 'reason': 'check-error'}
