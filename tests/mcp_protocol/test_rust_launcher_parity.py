"""Launcher install and vendored launch: the Rust client (packages/tofu-mcp,
``launcher.rs`` / ``vendor.rs``) against the Python oracle
(``lib/mcp/client/_install.py``, ``lib/mcp/client/_vendor.py``,
``lib/mcp/vendored.py``).

Every case runs the same inputs through both clients: ``launcher.<op>`` host
methods for the ported functions, real ``uv`` warms against a recording fake
``uv`` on PATH (neither client's warm runner is patched), and real connects of
a vendored server through both bridges. The registry rows, ``_repo_root()``,
the interpreter and the proxy pool reach Rust from the binding
(``lib.mcp.rust_host.launcher_callbacks``), so a test's monkeypatches feed both
clients the same facts. Each client keeps its own tool cache (markers are
compared, never shared, unless a case checks that one reads the other's).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import lib.mcp.client as mc
from lib.mcp.client import _vendor as vendor
from tests.mcp_protocol.rust_parity_support import (  # noqa: F401 - fixtures
    CRATE, REPLAY, ROOT, SESSIONS, mcp_host_binary, python_bridge, scene,
)
__all__ = ['mcp_host_binary', 'scene']

pytestmark = pytest.mark.unit

SERVER_MINOR = f'{sys.version_info.major}.{sys.version_info.minor}'
_PROXY_KEYS = ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy',
               'NO_PROXY', 'no_proxy', 'TOFU_HTTPS_PROXY', 'TOFU_HTTP_PROXY', 'TOFU_ALL_PROXY',
               'TOFU_NO_PROXY')
_TOOL_SETTINGS = ('UV_PYTHON', 'UV_EXCLUDE_NEWER', 'npm_config_before', 'TOFU_MCP_SUPPLY_CUTOFF',
                  'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR',
                  'UV_LINK_MODE', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'PIP_CACHE_DIR',
                  'npm_config_cache', 'CARGO_HOME', 'npm_config_prefix', 'NPM_CONFIG_PREFIX',
                  'TOFU_MCP_AUTO_VENDOR', 'TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS',
                  'TOFU_MCP_COLD_INSTALL_TIMEOUT')
_INTERNAL = ('hope-mcp', 'llm-mcp', 'xuecheng-mcp')
# The launcher fixture replaces HOME; Cargo (a rustup proxy) needs the real one.
_ORIGINAL_ENV = dict(os.environ)
_CARGO = shutil.which('cargo') or next(
    (str(Path(home) / 'bin' / 'cargo') for home in (os.environ.get('CARGO_HOME'), str(Path.home() / '.cargo'))
     if home and (Path(home) / 'bin' / 'cargo').is_file()), None)

COVERS: dict[str, list[str]] = {
    'test_mcp_install_nonblocking.py': [
        'test_launch_path_has_no_blocking_subprocess_and_no_pip',
        'test_vendored_command_translates_to_the_same_isolated_argv',
        'test_unregistered_or_sourceless_command_is_not_translated',
        'test_prewarm_runs_the_uv_warm_and_reports_failure_like_python',
        'test_prewarm_is_a_noop_for_non_vendored_commands',
        'test_install_job_returns_at_once_and_is_pollable',
        'test_install_job_ready_fast_path_for_a_non_vendored_launcher_on_path',
        'test_warm_marker_lifecycle_drives_the_cold_budget',
        'test_cold_budget_ignores_non_vendored_commands',
        'test_connect_plan_widens_the_handshake_only_for_a_cold_launch',
        'test_vendored_server_connects_isolated_through_both_clients',
        'test_install_job_warms_a_vendored_command_even_when_on_path',
    ],
    'test_mcp_launcher_resolve.py': [
        'test_resolve_launcher_next_to_the_interpreter',
        'test_prepend_interpreter_bin_to_path',
        'test_find_vendored_source_layouts',
        'test_vendored_command_translates_to_the_same_isolated_argv',
        'test_unregistered_or_sourceless_command_is_not_translated',
        'test_prewarm_failure_is_retryable_not_wedged',
        'test_launcher_install_hint',
        'test_registry_hot_reload_reaches_rust',
        'test_registry_broken_edit_keeps_the_last_good_rows',
        'test_registry_unchanged_is_not_reloaded',
        'test_snapshot_stale_reason',
        'test_check_snapshot_staleness',
    ],
    'test_mcp_launcher_python_pin.py': [
        'test_launch_environment_interpreter_pin',
        'test_prewarm_warms_the_interpreter_the_launch_uses',
        'test_launcher_and_command_children_share_the_managed_store',
    ],
    'test_mcp_launcher_cargo_workspace.py': [
        'test_launcher_cache_sdist_crate_is_its_own_workspace',
    ],
    'test_mcp_vendored_isolation.py': [
        'test_default_overleaf_launch_argv',
        'test_internal_launchers_translate_like_python',
        'test_shared_extra_sources_are_launched_or_refused_like_python',
        'test_bundle_launcher_contract',
        'test_vendored_server_connects_isolated_through_both_clients',
        'test_launch_path_has_no_blocking_subprocess_and_no_pip',
        'test_prewarm_runs_the_uv_warm_and_reports_failure_like_python',
    ],
    'test_mcp_prewarm_resilience.py': [
        'test_propagate_proxy_env',
        'test_negative_cache',
        'test_negative_cache_markers_are_interchangeable',
        'test_egress_identity',
        'test_is_index_unreachable',
    ],
    'test_mcp_sdk_pin_bounded.py': [
        'test_rmcp_is_pinned_exactly_and_locked',
        'test_launcher_env_carries_the_supply_cutoff',
        'test_default_overleaf_launch_argv',
        'test_only_exact_source_pins_are_exempt_from_cutoff',
        'test_manifest_change_invalidates_warm_marker',
        'test_supply_cutoff_is_operator_overridable_and_optional',
        'test_stale_npx_slot_is_reconciled_against_the_cutoff',
        'test_reconcile_runs_before_the_readiness_timer_not_inside_it',
        'test_pending_rebuild_is_visible_to_every_connect',
        'test_connect_plan_widens_the_handshake_only_for_a_cold_launch',
        'cargo:tests/launcher.rs::tool_input_schema_accepts_both_sdk_spellings',
    ],
}


# ── fixtures ─────────────────────────────────────────────────────────


class Rust:
    """A ``tofu-mcp-host`` with the launcher callbacks, started on first use.

    It is spawned with the test's ``os.environ`` (after its monkeypatches) and
    its own tool cache, so its markers never mix with the oracle's.
    """

    def __init__(self, binary: str, cache: Path):
        self.binary = binary
        self.cache = cache
        self.overrides: dict[str, str | None] = {'TOFU_MCP_CACHE_DIR': str(cache)}
        self.host = None

    def start(self, **overrides: str | None):
        from lib.mcp.rust_host import RustMCPHost, launcher_callbacks
        self.close()
        self.overrides.update(overrides)
        env = dict(os.environ)
        for key, value in self.overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.host = RustMCPHost(self.binary, env=env, callbacks=launcher_callbacks())
        return self.host

    def __call__(self, op: str, **params):
        if self.host is None:
            self.start()
        return self.host.call(f'launcher.{op}', params, timeout=120)

    def close(self):
        if self.host is not None:
            self.host.close()
            self.host = None


@pytest.fixture
def launcher_env(tmp_path, monkeypatch):
    """Hermetic launcher facts: own HOME and caches, no proxies, empty pool, fresh state."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path / 'py-cache'))
    for key in (*_PROXY_KEYS, *_TOOL_SETTINGS):
        monkeypatch.delenv(key, raising=False)
    import lib.proxy as lib_proxy
    monkeypatch.setattr(lib_proxy, 'first_global_proxy_url', lambda: '')
    monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url', lambda *a, **k: '')
    monkeypatch.setattr(mc, '_install_last_error', {})
    monkeypatch.setattr(mc, '_install_jobs', {})
    monkeypatch.setattr(mc, '_snapshot_reported', {})
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    return tmp_path


@pytest.fixture
def rust(mcp_host_binary, launcher_env):
    client = Rust(mcp_host_binary, launcher_env / 'rust-cache')
    yield client
    client.close()


def _register(tmp_path, monkeypatch, command='fake-mcp', layout='sibling', manifest='[project]\nname="x"\n'):
    """The oracle suites' fake vendored launcher backed by a real source dir."""
    repo = tmp_path / 'repo'
    repo.mkdir(exist_ok=True)
    if layout == 'sibling':
        src, rel = tmp_path / command, f'../{command}'
    else:
        src, rel = repo / 'tools' / command, f'tools/{command}'
    src.mkdir(parents=True)
    (src / 'pyproject.toml').write_text(manifest)
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, command, {'sources': [rel]})
    return str(src), str(repo)


class FakeUv:
    """A recording ``uv`` first on PATH: logs argv and the interpreter pin, answers per control."""

    def __init__(self, tmp_path: Path, monkeypatch, *, then_exec_replay: str | None = None):
        self.dir = tmp_path / 'fakebin'
        self.dir.mkdir(exist_ok=True)
        self.log = tmp_path / 'uv.log'
        self.control = tmp_path / 'uv.control.json'
        self.set()
        replay = ''
        if then_exec_replay is not None:
            replay = (f'os.execv(sys.executable, [sys.executable, {str(REPLAY)!r}, '
                      f'{str(SESSIONS / "hope_like.json")!r}, {then_exec_replay!r} + "." + str(os.getpid()) + ".log", '
                      f'{then_exec_replay!r} + "." + str(os.getpid()) + ".state"])\n')
        script = self.dir / 'uv'
        script.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys, time\n'
            f'control = json.load(open({str(self.control)!r}))\n'
            f'with open({str(self.log)!r}, "a") as f:\n'
            '    f.write(json.dumps({"argv": sys.argv[1:], "UV_PYTHON": os.environ.get("UV_PYTHON"),\n'
            '                        "UV_EXCLUDE_NEWER": os.environ.get("UV_EXCLUDE_NEWER"),\n'
            '                        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY")}) + "\\n")\n'
            + replay +
            'release = control.get("release")\n'
            'while release and not os.path.exists(release):\n'
            '    time.sleep(0.02)\n'
            'sys.stdout.write(control.get("stdout", ""))\n'
            'sys.stderr.write(control.get("stderr", ""))\n'
            'sys.exit(control.get("rc", 0))\n', encoding='utf-8')
        script.chmod(0o755)
        monkeypatch.setenv('PATH', f'{self.dir}{os.pathsep}{os.environ.get("PATH", "")}')

    def set(self, **control):
        self.control.write_text(json.dumps(control), encoding='utf-8')

    def runs(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def take(self) -> list[dict]:
        runs = self.runs()
        self.log.unlink(missing_ok=True)
        return runs


def _python_error(call):
    try:
        return ('ok', call())
    except ValueError as exc:
        return ('error', str(exc))


def _rust_error(call):
    from lib.mcp.rust_host import RustHostError
    try:
        return ('ok', call())
    except RustHostError as exc:
        return ('error', str(exc))


# ── install flow (test_mcp_install_nonblocking.py) ───────────────────


def test_launch_path_has_no_blocking_subprocess_and_no_pip():
    """The Rust launch and warm paths spawn only tokio children; pip is gone, not dormant."""
    for name in ('vendor.rs', 'launcher.rs', 'rmcp_session.rs'):
        source = (CRATE / 'src' / name).read_text(encoding='utf-8')
        assert 'std::process::Command' not in source, name
        assert 'spawn_blocking' not in source, name
        # Hint text may name pip (as the oracle's may); the action must be gone.
        assert '"-m", "pip"' not in source and 'Command::new("pip' not in source, name
    connect = (CRATE / 'src' / 'rmcp_session.rs').read_text(encoding='utf-8')
    assert 'plan_stdio_launch' in connect and 'cold_install_timeout' in connect


@pytest.mark.parametrize('layout', ['sibling', 'vendored'])
def test_vendored_command_translates_to_the_same_isolated_argv(rust, launcher_env, monkeypatch, layout):
    src, _ = _register(launcher_env, monkeypatch, 'fake-mcp', layout)
    expected = mc.vendored_launch_argv('fake-mcp')
    assert expected[:4] == ['uv', 'run', '--no-project', '--with-editable'] and expected[4] == src
    assert rust('vendored_launch_argv', command='fake-mcp') == expected
    assert rust('is_vendored_launcher', command='fake-mcp') is mc.is_vendored_launcher('fake-mcp') is True


def test_unregistered_or_sourceless_command_is_not_translated(rust, launcher_env, monkeypatch):
    repo = launcher_env / 'repo'
    (repo / 'tools').mkdir(parents=True)
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'gone-mcp', {'sources': ['tools/gone-mcp']})
    for command in ('gone-mcp', 'totally-unknown-mcp', '/usr/bin/hope-mcp', './foo', ''):
        assert mc.vendored_launch_argv(command) is None
        assert rust('vendored_launch_argv', command=command) is None
        assert rust('is_vendored_launcher', command=command) is mc.is_vendored_launcher(command)


def test_prewarm_runs_the_uv_warm_and_reports_failure_like_python(rust, launcher_env, monkeypatch):
    src, _ = _register(launcher_env, monkeypatch, 'warm-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)

    python = list(mc.prewarm_vendored_launcher('warm-mcp'))
    py_runs = uv.take()
    rs = rust('prewarm', command='warm-mcp')
    rs_runs = uv.take()
    assert rs == python and rs[0] is True and 'uv run' in rs[1]
    assert [run['argv'] for run in rs_runs] == [run['argv'] for run in py_runs] == [
        ['run', '--no-project', '--with-editable', src, 'python', '-c', 'import warm_mcp']]

    uv.set(rc=1, stderr='ResolutionImpossible: boom')
    python = list(mc.prewarm_vendored_launcher('warm-mcp'))
    rs = rust('prewarm', command='warm-mcp')
    assert rs == python == [False, 'uv warm exited 1: ResolutionImpossible: boom']
    assert rust('last_error', command='warm-mcp') == mc._install_last_error['warm-mcp']


@pytest.mark.parametrize('command', ['definitely-not-vendored', '/abs/path/to/thing'])
def test_prewarm_is_a_noop_for_non_vendored_commands(rust, launcher_env, command):
    assert rust('prewarm', command=command) == list(mc.prewarm_vendored_launcher(command)) == [True, '']


def _wait_job(rust, command):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = rust('get_install_job', command=command)
        if job['state'] != 'installing':
            return job
        time.sleep(0.05)
    raise AssertionError(f'install job for {command} never finished')


def test_install_job_returns_at_once_and_is_pollable(rust, launcher_env, monkeypatch):
    _register(launcher_env, monkeypatch, 'job-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)
    release = launcher_env / 'release'
    uv.set(release=str(release))

    job = rust('start_install_job', command='job-mcp')
    assert job['state'] == 'installing' and job['ended'] == 0.0
    assert rust('get_install_job', command='job-mcp')['state'] == 'installing'
    again = rust('start_install_job', command='job-mcp')
    assert again['state'] == 'installing' and again['started'] == job['started']

    release.write_text('go')
    final = _wait_job(rust, 'job-mcp')
    assert final['state'] == 'ready' and 'uv run' in final['detail']
    assert len(uv.runs()) == 1, 're-clicking Install must re-attach, never start a second warm'


def test_install_job_ready_fast_path_for_a_non_vendored_launcher_on_path(rust, launcher_env, monkeypatch):
    tools = launcher_env / 'tools-bin'
    tools.mkdir()
    launcher = tools / 'some-foreign-tool'
    launcher.write_text('#!/bin/sh\nexit 0\n')
    launcher.chmod(0o755)
    monkeypatch.setenv('PATH', str(tools))
    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', lambda c: pytest.fail('no warm for on-PATH'))
    assert mc.start_install_job('some-foreign-tool')['state'] == 'ready'
    assert rust('start_install_job', command='some-foreign-tool')['state'] == 'ready'
    assert rust('start_install_job', command='/abs/x')['state'] == 'ready'


def test_install_job_warms_a_vendored_command_even_when_on_path(rust, launcher_env, monkeypatch):
    _register(launcher_env, monkeypatch, 'fast-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)
    stale = uv.dir / 'fast-mcp'
    stale.write_text('#!/bin/sh\nexit 0\n')
    stale.chmod(0o755)
    release = launcher_env / 'release'
    uv.set(release=str(release))
    assert rust('start_install_job', command='fast-mcp')['state'] == 'installing'
    release.write_text('go')
    assert _wait_job(rust, 'fast-mcp')['state'] == 'ready'
    assert len(uv.runs()) == 1


def test_warm_marker_lifecycle_drives_the_cold_budget(rust, launcher_env, monkeypatch):
    _register(launcher_env, monkeypatch, 'mark-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)

    def lifecycle(cold, prewarm):
        seen = [cold()]
        uv.set()
        seen += [prewarm()[0], cold()]
        uv.set(rc=1, stderr='ResolutionImpossible: boom')
        seen += [prewarm()[0], cold()]
        return seen

    python = lifecycle(lambda: mc.vendored_needs_cold_budget('mark-mcp'),
                       lambda: mc.prewarm_vendored_launcher('mark-mcp'))
    rs = lifecycle(lambda: rust('needs_cold_budget', command='mark-mcp'),
                   lambda: rust('prewarm', command='mark-mcp'))
    assert rs == python == [True, True, False, False, True]


@pytest.mark.parametrize('command', ['definitely-not-vendored', '/abs/path/tool'])
def test_cold_budget_ignores_non_vendored_commands(rust, launcher_env, command):
    assert rust('needs_cold_budget', command=command) is mc.vendored_needs_cold_budget(command) is False


def test_connect_plan_widens_the_handshake_only_for_a_cold_launch(rust, launcher_env, monkeypatch):
    from lib.mcp.types import MCP_COLD_INSTALL_TIMEOUT, MCP_CONNECT_TIMEOUT
    src, _ = _register(launcher_env, monkeypatch, 'plan-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)
    config = {'command': 'plan-mcp', 'args': ['--flag']}

    cold = rust('connect_plan', config=config)
    assert cold['cold_install'] is mc.vendored_needs_cold_budget('plan-mcp') is True
    assert cold['handshake_budget'] == MCP_COLD_INSTALL_TIMEOUT
    assert [cold['command'], *cold['args']] == [*mc.vendored_launch_argv('plan-mcp'), '--flag']
    assert cold['holds_npm_lock'] is False

    uv.set()
    assert rust('prewarm', command='plan-mcp')[0] and mc.prewarm_vendored_launcher('plan-mcp')[0]
    warm = rust('connect_plan', config=config)
    assert warm['cold_install'] is mc.vendored_needs_cold_budget('plan-mcp') is False
    assert warm['handshake_budget'] == MCP_CONNECT_TIMEOUT

    # An outstanding npx rebuild widens any launch, and an npm launcher keeps the cache lock.
    slot = launcher_env / 'rust-cache' / 'npm' / '_npx' / 'stale0001'
    slot.mkdir(parents=True)
    (slot / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    npx = rust('connect_plan', config={'command': 'npx', 'args': ['-y', 'x']})
    assert npx == {'command': 'npx', 'args': ['-y', 'x'], 'cold_install': True,
                   'handshake_budget': MCP_COLD_INSTALL_TIMEOUT, 'holds_npm_lock': True}


def test_vendored_server_connects_isolated_through_both_clients(mcp_host_binary, scene, tmp_path, monkeypatch):
    """A bare vendored name connects through ``uv run --no-project --with-editable <src> <name>``."""
    from lib.mcp.rust_host import RustMCPBridge, RustMCPHost, launcher_callbacks
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    monkeypatch.setattr(mc, '_install_last_error', {})
    (tmp_path / 'home').mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    for key in ('CARGO_HOME', 'npm_config_prefix', 'NPM_CONFIG_PREFIX', 'UV_TOOL_BIN_DIR'):
        monkeypatch.delenv(key, raising=False)
    src, _ = _register(tmp_path, monkeypatch, 'iso-mcp', 'vendored')
    uv = FakeUv(tmp_path, monkeypatch, then_exec_replay=str(tmp_path / 'replay'))
    py = python_bridge(monkeypatch)
    host = RustMCPHost(mcp_host_binary, env=dict(os.environ), callbacks=launcher_callbacks())
    try:
        host.call('configure', scene.host_settings(), timeout=30)
        rs = RustMCPBridge(host, 1)
        py_tools = py.connect_server('iso', {'command': 'iso-mcp'})
        py_runs = uv.take()
        rs_tools = rs.connect_server('iso', {'command': 'iso-mcp'})
        rs_runs = uv.take()
        assert sorted(rs_tools) == sorted(getattr(t, 'name', t) for t in py_tools)
        assert [r['argv'] for r in rs_runs] == [r['argv'] for r in py_runs] == [
            ['run', '--no-project', '--with-editable', src, 'iso-mcp']]
        assert rs_runs[0]['UV_PYTHON'] == py_runs[0]['UV_PYTHON'] == SERVER_MINOR
        assert rs_runs[0]['UV_EXCLUDE_NEWER'] == py_runs[0]['UV_EXCLUDE_NEWER']
    finally:
        py.disconnect_all()
        host.close()


# ── resolution, hints, registry, snapshots (test_mcp_launcher_resolve.py) ──


def _exe(path: Path) -> None:
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)


@pytest.mark.parametrize('case', ['next_to_interpreter', 'missing', 'not_executable',
                                  'base_prefix_bin', 'pathful'])
def test_resolve_launcher_next_to_the_interpreter(rust, launcher_env, monkeypatch, case):
    exe_dir = launcher_env / 'venv' / 'bin'
    exe_dir.mkdir(parents=True)
    base = launcher_env / 'base'
    (base / 'bin').mkdir(parents=True)
    monkeypatch.setattr(sys, 'executable', str(exe_dir / 'python'))
    monkeypatch.setattr(sys, 'base_prefix', str(base))
    command = {'next_to_interpreter': 'my-mcp', 'missing': 'definitely-not-here-xyz',
               'not_executable': 'not-exec', 'base_prefix_bin': 'base-mcp', 'pathful': './foo'}[case]
    if case == 'next_to_interpreter':
        _exe(exe_dir / 'my-mcp')
    elif case == 'not_executable':
        (exe_dir / 'not-exec').write_text('#!/bin/sh\n')
    elif case == 'base_prefix_bin':
        _exe(base / 'bin' / 'base-mcp')
    expected = mc._resolve_launcher(command)
    assert rust('resolve_launcher', command=command) == expected
    assert (expected is None) == (case in ('missing', 'not_executable', 'pathful'))
    if case == 'pathful':
        assert rust('resolve_launcher', command='/usr/bin/env') is mc._resolve_launcher('/usr/bin/env') is None


@pytest.mark.parametrize('case', ['prepend', 'idempotent', 'dedupe', 'no_path_key', 'missing_dir'])
def test_prepend_interpreter_bin_to_path(rust, launcher_env, monkeypatch, case):
    fake_bin = launcher_env / 'bin'
    if case != 'missing_dir':
        fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))
    env = {'prepend': {'PATH': '/usr/bin:/bin'}, 'idempotent': {'PATH': '/usr/bin'},
           'dedupe': {'PATH': os.pathsep.join(['/usr/bin', str(fake_bin), '/bin'])},
           'no_path_key': {}, 'missing_dir': {'PATH': '/usr/bin'}}[case]
    python = dict(env)
    mc._prepend_interpreter_bin_to_path(python)
    rs = rust('prepend_interpreter_bin', env=env)
    assert rs == python
    if case == 'idempotent':
        again = dict(python)
        mc._prepend_interpreter_bin_to_path(again)
        assert rust('prepend_interpreter_bin', env=rs) == again == python


def test_find_vendored_source_layouts(rust, launcher_env, monkeypatch):
    src_v, _ = _register(launcher_env, monkeypatch, 'vend-mcp', 'vendored')
    src_s, _ = _register(launcher_env, monkeypatch, 'sib-mcp', 'sibling')
    repo = launcher_env / 'repo'
    (repo / 'tools' / 'dual-mcp').mkdir(parents=True)
    (repo / 'tools' / 'dual-mcp' / 'pyproject.toml').write_text('x')
    (launcher_env / 'dual-mcp').mkdir()
    (launcher_env / 'dual-mcp' / 'pyproject.toml').write_text('x')
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'dual-mcp', {'sources': ['../dual-mcp', 'tools/dual-mcp']})
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'gone-mcp', {'sources': ['tools/gone-mcp']})
    absolute = launcher_env / 'abs-src'
    absolute.mkdir()
    (absolute / 'pyproject.toml').write_text('x')
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'abs-mcp', {'sources': [str(absolute)]})
    for command in ('vend-mcp', 'sib-mcp', 'dual-mcp', 'gone-mcp', 'abs-mcp', 'not-registered'):
        expected = mc._find_vendored_source(command)
        assert rust('find_vendored_source', command=command) == (list(expected) if expected else None)
    assert mc._find_vendored_source('vend-mcp') == (src_v, False)
    assert mc._find_vendored_source('sib-mcp') == (src_s, True)


def test_prewarm_failure_is_retryable_not_wedged(rust, launcher_env, monkeypatch):
    _register(launcher_env, monkeypatch)
    uv = FakeUv(launcher_env, monkeypatch)
    uv.set(rc=1, stderr='network unreachable')
    assert rust('prewarm', command='fake-mcp')[0] is False
    assert rust('prewarm', command='fake-mcp')[0] is False
    assert len(uv.take()) == 2, 'a second attempt must be allowed to retry'
    assert 'network unreachable' in rust('last_error', command='fake-mcp')


def test_launcher_install_hint(rust, launcher_env, monkeypatch):
    src, _ = _register(launcher_env, monkeypatch, 'sib-mcp', 'sibling')
    uv = FakeUv(launcher_env, monkeypatch)
    uv.set(rc=1, stderr='boom')
    assert rust('prewarm', command='sib-mcp') == list(mc.prewarm_vendored_launcher('sib-mcp'))
    hint = rust('install_hint', command='sib-mcp')
    assert hint == mc._launcher_install_hint('sib-mcp')
    assert 'boom' in hint and src in hint and 'package manager' not in hint
    for command in ('totally-unknown-mcp', 'uvx', 'npx', 'pipx', 'node', 'python3', '/opt/x/npx', 'uv'):
        assert rust('install_hint', command=command) == mc._launcher_install_hint(command)
    assert 'package manager' in rust('install_hint', command='totally-unknown-mcp')


_FAKE_SEQ = [0]


def _fake_vendored_module(tmp_path, monkeypatch, launchers):
    import importlib
    _FAKE_SEQ[0] += 1
    name = f'fake_rust_vendored_mod_{_FAKE_SEQ[0]}'
    path = tmp_path / f'{name}.py'
    path.write_text('VENDORED_LAUNCHERS = ' + repr(launchers) + '\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(name)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(mc, '_vendored_mod', module)
    monkeypatch.setattr(mc, '_VENDORED_LAUNCHERS', launchers)
    monkeypatch.setattr(mc, '_vendored_mtime', os.path.getmtime(path))
    return str(path)


def test_registry_hot_reload_reaches_rust(rust, launcher_env, monkeypatch):
    live = {'hope-mcp': {'sources': ['../hope-mcp', 'tools/hope-mcp']}}
    path = _fake_vendored_module(launcher_env, monkeypatch, live)
    assert set(rust('registry')) == {'hope-mcp'}
    with open(path, 'w', encoding='utf-8') as f:
        f.write("VENDORED_LAUNCHERS = {'hope-mcp': {'sources': ['../hope-mcp']}, "
                "'newrow-mcp': {'sources': ['../newrow-mcp', 'tools/newrow-mcp']}}\n")
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert rust('registry') == {'hope-mcp': {'sources': ['../hope-mcp']},
                                'newrow-mcp': {'sources': ['../newrow-mcp', 'tools/newrow-mcp']}}
    assert mc._VENDORED_LAUNCHERS is live and 'newrow-mcp' in live


def test_registry_broken_edit_keeps_the_last_good_rows(rust, launcher_env, monkeypatch):
    live = {'hope-mcp': {'sources': ['../hope-mcp']}, 'llm-mcp': {'sources': ['../llm-mcp']}}
    path = _fake_vendored_module(launcher_env, monkeypatch, live)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('VENDORED_LAUNCHERS = { this is not valid python {{{\n')
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert set(rust('registry')) == {'hope-mcp', 'llm-mcp'}
    # When the binding itself cannot answer, Rust keeps its last good registry.
    rust.host.callbacks.pop('launcher_context')
    assert set(rust('registry')) == {'hope-mcp', 'llm-mcp'}


def test_registry_unchanged_is_not_reloaded(rust, launcher_env, monkeypatch):
    live = {'hope-mcp': {'sources': ['../hope-mcp']}}
    path = _fake_vendored_module(launcher_env, monkeypatch, live)
    monkeypatch.setattr(mc, '_vendored_mtime', os.path.getmtime(path) + 100)
    reloaded = []
    real_reload = mc.importlib.reload
    monkeypatch.setattr(mc.importlib, 'reload', lambda m: (reloaded.append(m), real_reload(m))[1])
    assert rust('registry') == live
    assert reloaded == []


def _vendor_pair(tmp_path, monkeypatch, name='pair-mcp', synced=True):
    repo = tmp_path / 'repo'
    (repo / 'tools').mkdir(parents=True, exist_ok=True)
    sibling = tmp_path / name
    snapshot = repo / 'tools' / name
    (sibling / 'src').mkdir(parents=True)
    (sibling / 'pyproject.toml').write_text('[project]\nname="x"\n')
    (sibling / 'src' / 'mod.py').write_text('print("v1")\n')
    if synced:
        shutil.copytree(sibling, snapshot, dirs_exist_ok=True)
        for root, _dirs, files in os.walk(sibling):
            for f in files:
                s = os.path.join(root, f)
                st = os.stat(s)
                os.utime(os.path.join(snapshot, os.path.relpath(s, sibling)), (st.st_atime, st.st_mtime))
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, name, {'sources': [f'../{name}', f'tools/{name}']})
    return str(repo), str(sibling), str(snapshot)


def _drift(sibling: str, body='print("v2 changed")\n'):
    path = os.path.join(sibling, 'src', 'mod.py')
    with open(path, 'w') as f:
        f.write(body)
    os.utime(path, (time.time() + 5, time.time() + 5))


@pytest.mark.parametrize('case', ['fresh', 'runtime_artifacts', 'changed', 'added_and_removed', 'missing'])
def test_snapshot_stale_reason(rust, launcher_env, monkeypatch, case):
    _, sib, snap = _vendor_pair(launcher_env, monkeypatch, synced=case != 'missing')
    if case == 'runtime_artifacts':
        for rel in ('src/__pycache__/mod.cpython-312.pyc', 'x.egg-info/PKG-INFO', '.tofu_trash/r/old.py',
                    '.oa-skills-ref/client.js', 'JOURNAL.md', 'citadel (1).zip', '.venv/lib/x.py',
                    'src/link.egg-link'):
            os.makedirs(os.path.dirname(os.path.join(sib, rel)), exist_ok=True)
            Path(sib, rel).write_text('artifact')
    elif case == 'changed':
        _drift(sib)
    elif case == 'added_and_removed':
        for index in range(5):
            Path(sib, f'new{index}.py').write_text('x')
        Path(snap, 'gone.py').write_text('x')
    expected = mc._snapshot_stale_reason(sib, snap)
    assert rust('snapshot_stale_reason', sibling=sib, snapshot=snap) == expected
    assert (expected == '') == (case in ('fresh', 'runtime_artifacts'))


@pytest.mark.parametrize('case', ['detect_only', 'opt_in_rebuild', 'no_sibling', 'not_vendored'])
def test_check_snapshot_staleness(rust, launcher_env, monkeypatch, case):
    """Warn (always) and rebuild (opt-in only) through a real ``scripts/vendor_mcp.sh``."""
    log = launcher_env / 'vendored.log'
    monkeypatch.setenv('VENDOR_SCRIPT_LOG', str(log))
    if case == 'no_sibling':
        repo = launcher_env / 'repo'
        snap = repo / 'tools' / 'deploy-mcp'
        snap.mkdir(parents=True)
        (snap / 'pyproject.toml').write_text('[project]\nname="x"\n')
        monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
        monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'deploy-mcp', {'sources': ['../deploy-mcp', 'tools/deploy-mcp']})
        command = 'deploy-mcp'
    elif case == 'not_vendored':
        repo = launcher_env / 'repo'
        repo.mkdir()
        monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
        command = 'npx'
    else:
        _, sib, _ = _vendor_pair(launcher_env, monkeypatch, 'stale-mcp')
        repo = launcher_env / 'repo'
        _drift(sib, 'drifted\n')
        command = 'stale-mcp'
    (repo / 'scripts').mkdir(parents=True, exist_ok=True)
    (repo / 'scripts' / 'vendor_mcp.sh').write_text('#!/bin/bash\necho "$1 $PWD" >> "$VENDOR_SCRIPT_LOG"\n')
    if case in ('opt_in_rebuild', 'no_sibling'):
        monkeypatch.setenv('TOFU_MCP_AUTO_VENDOR', '1')

    messages: list[str] = []

    class Recorder:
        def warning(self, message, *args):
            messages.append(message % args)
        info = debug = error = warning

    monkeypatch.setattr(vendor, 'logger', Recorder())
    mc._check_snapshot_staleness(command)
    warned = [m for m in messages if 'is STALE' in m]
    py_vendored = log.read_text().splitlines() if log.exists() else []
    log.unlink(missing_ok=True)

    rs_warning = rust('check_snapshot_staleness', command=command)
    rs_vendored = log.read_text().splitlines() if log.exists() else []
    assert ([rs_warning] if rs_warning else []) == warned
    assert rs_vendored == py_vendored
    if case == 'opt_in_rebuild':
        assert rs_vendored == [f'{command} {os.path.realpath(repo)}']
    elif case == 'detect_only':
        assert warned and rs_vendored == []
        # Same drift as last time: the oracle stays quiet, and so does Rust.
        assert rust('check_snapshot_staleness', command=command) is None
    else:
        assert rs_warning is None and rs_vendored == []


# ── interpreter pin (test_mcp_launcher_python_pin.py) ────────────────


def _rust_stdio_environment(rust, config):
    import lib.tools.process_environment as process_environment
    if rust.host is None:
        rust.start()
    return rust.host.call('stdio_environment', {
        'config': config, 'process_env': dict(os.environ), 'prepare_directories': False,
        'runtime_base': process_environment.get_runtime_base(), 'repo_root': str(ROOT),
        'python_minor': SERVER_MINOR, 'policy_path_template': '/nonexistent/{owner}'}, timeout=60)


@pytest.mark.parametrize('source', ['default', 'server configuration', 'operator environment', 'cutoff disabled'])
def test_launch_environment_interpreter_pin(rust, launcher_env, monkeypatch, source):
    from lib.mcp.stdio_environment import build_stdio_environment
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_pool_proxy_env', lambda env: None)
    config = {'command': 'uvx'}
    if source == 'server configuration':
        config['env'] = {'UV_PYTHON': '3.13'}
    elif source == 'operator environment':
        monkeypatch.setenv('UV_PYTHON', '3.13')
    elif source == 'cutoff disabled':
        monkeypatch.setenv('TOFU_MCP_SUPPLY_CUTOFF', '')
    python = build_stdio_environment(config, prepare_directories=False)
    rs = _rust_stdio_environment(rust, config)
    for key in ('UV_PYTHON', 'UV_EXCLUDE_NEWER', 'npm_config_before'):
        assert rs.get(key) == python.get(key), key
    expected = {'default': SERVER_MINOR, 'cutoff disabled': None}.get(source, '3.13')
    assert rs.get('UV_PYTHON') == expected


def test_prewarm_warms_the_interpreter_the_launch_uses(rust, launcher_env, monkeypatch):
    _register(launcher_env, monkeypatch, 'pin-probe-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)
    assert rust('prewarm', command='pin-probe-mcp')[0] is True
    assert mc.prewarm_vendored_launcher('pin-probe-mcp')[0] is True
    rs_run, py_run = uv.take()
    launch = _rust_stdio_environment(rust, {'command': 'pin-probe-mcp'})
    assert rs_run['UV_PYTHON'] == py_run['UV_PYTHON'] == launch['UV_PYTHON'] == SERVER_MINOR
    assert rs_run['UV_EXCLUDE_NEWER'] == py_run['UV_EXCLUDE_NEWER'] == launch['UV_EXCLUDE_NEWER']


def test_launcher_and_command_children_share_the_managed_store(rust, launcher_env):
    """The run_command half (``_get_cmd_env`` drops the pin) is Python-only; Rust owns the launcher half."""
    from lib.project_mod.run_command import _get_cmd_env
    project = launcher_env / 'project'
    project.mkdir()
    command = _get_cmd_env(str(project), project_root=str(project))
    launcher = _rust_stdio_environment(rust, {'command': 'uvx'})
    assert launcher['UV_PYTHON_INSTALL_DIR'] == command['UV_PYTHON_INSTALL_DIR']
    assert launcher['UV_PYTHON'] == SERVER_MINOR and 'UV_PYTHON' not in command


# ── Cargo workspace isolation (test_mcp_launcher_cargo_workspace.py) ──


def _crate(directory: Path, name: str) -> Path:
    (directory / 'src').mkdir(parents=True, exist_ok=True)
    (directory / 'src/lib.rs').write_text('')
    (directory / 'Cargo.toml').write_text(f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n')
    return directory


def _checkout(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = (ROOT / 'Cargo.toml').read_text()
    for index, member in enumerate(tomllib.loads(manifest)['workspace']['members']):
        _crate(directory / member, f'member-{index}')
    (directory / 'Cargo.toml').write_text(manifest)


@pytest.mark.parametrize('checkout', ['.', '.tofu/worktrees/feature'])
def test_launcher_cache_sdist_crate_is_its_own_workspace(rust, tmp_path, monkeypatch, checkout):
    from lib.tools.process_environment import tool_cache_defaults, tool_cache_root
    from runtime_path_policy import get_runtime_base
    monkeypatch.delenv('TOFU_MCP_CACHE_DIR', raising=False)
    rust.start(TOFU_MCP_CACHE_DIR=None)
    defaults = rust('tool_cache_defaults')
    assert defaults == tool_cache_defaults(tool_cache_root())
    uv_cache = Path(defaults['UV_CACHE_DIR']).relative_to(get_runtime_base())
    if _CARGO is None:
        pytest.skip('Cargo is required to verify actual workspace discovery')
    workspace = tmp_path / 'ws'
    _checkout(workspace)
    _checkout(workspace / checkout)
    crate = _crate(workspace / checkout / uv_cache / 'sdists-v10/pypi/pydantic-core/2.46.4/build/src',
                   'pydantic-core')
    result = subprocess.run([_CARGO, 'metadata', '--format-version', '1', '--no-deps', '--manifest-path',
                             str(crate / 'Cargo.toml')], capture_output=True, text=True, timeout=120,
                            cwd=workspace, env=_ORIGINAL_ENV)
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['workspace_root']) == crate


# ── vendored isolation (test_mcp_vendored_isolation.py) ──────────────


def test_default_overleaf_launch_argv(rust, launcher_env):
    """The shipped registry row: owned source, shared isolated runner, exact pin exempt from the cutoff."""
    from lib.mcp.registry import get_catalog_entry
    catalog = get_catalog_entry('overleaf')
    assert catalog['command'] == 'tofu-overleaf-mcp' and catalog['args'] == []
    expected = mc.vendored_launch_argv(catalog['command'])
    argv = rust('vendored_launch_argv', command=catalog['command'])
    assert argv == expected
    source = Path(argv[argv.index('--with-editable') + 1])
    assert source.parent.name == 'packages' and source.name == 'tofu-overleaf-mcp'
    exemptions = [argv[i + 1] for i, a in enumerate(argv) if a == '--exclude-newer-package']
    assert 'overleaf-mcp-plus=false' in exemptions
    assert rust('launch_fingerprint', command='tofu-overleaf-mcp') == vendor._launch_fingerprint('tofu-overleaf-mcp')


def test_internal_launchers_translate_like_python(rust, launcher_env):
    """Overlay-declared internal servers (when installed) get the same argv, adapter source included."""
    for name in (*_INTERNAL, 'github-batch-mcp', 'definitely-not-a-server', '/usr/bin/hope-mcp'):
        assert rust('vendored_launch_argv', command=name) == mc.vendored_launch_argv(name), name
    assert {name: row['sources'] for name, row in rust('registry').items()} == {
        name: list(row['sources']) for name, row in mc._VENDORED_LAUNCHERS.items()}


def test_shared_extra_sources_are_launched_or_refused_like_python(rust, launcher_env, monkeypatch):
    import lib.mcp.vendored as vendored
    src, repo = _register(launcher_env, monkeypatch, 'adapter-mcp', 'vendored')
    adapter = Path(repo) / 'packages' / 'tofu-mtsso-auth'
    adapter.mkdir(parents=True)
    (adapter / 'pyproject.toml').write_text('[project]\nname="a"\ndependencies=["pinned==1"]\n')
    row = {'sources': ['tools/adapter-mcp'], 'extra_sources': ['packages/tofu-mtsso-auth']}
    monkeypatch.setitem(vendored.VENDORED_LAUNCHERS, 'adapter-mcp', row)
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'adapter-mcp', row)
    expected = mc.vendored_launch_argv('adapter-mcp')
    assert rust('vendored_launch_argv', command='adapter-mcp') == expected
    assert expected.count('--with-editable') == 2 and 'pinned=false' in expected
    shutil.rmtree(adapter)
    python = _python_error(lambda: mc.vendored_launch_argv('adapter-mcp'))
    rs = _rust_error(lambda: rust('vendored_launch_argv', command='adapter-mcp'))
    assert rs == python == ('error', 'MCP shared dependency source missing: packages/tofu-mtsso-auth')


def test_bundle_launcher_contract(rust, launcher_env, monkeypatch):
    import lib.mcp.vendored as vendored
    import tofu_extensions
    first, second = launcher_env / 'bundle-a', launcher_env / 'bundle-b'
    first.mkdir()
    second.mkdir()
    contract = vendored.MCP_LAUNCHERS_CONTRACT
    documents = {
        first / 'a.json': {'contract': contract, 'launchers': {
            'ok-mcp': {'sources': ['tools/ok-mcp', 'vendor/ok-mcp']},
            'shared-mcp': {'sources': ['tools/shared-mcp'], 'extra_sources': ['packages/auth']}}},
        first / 'b.json': {'contract': 'other/v1', 'launchers': {'nope-mcp': {'sources': ['x']}}},
        first / 'c.json': {'contract': contract, 'launchers': {
            'early-mcp': {'sources': ['tools/early']}, 'bad-mcp': {'sources': []}}},
        second / 'd.json': {'contract': contract, 'launchers': {
            'ok-mcp': {'sources': ['tools/override']}, 'extra-mcp': {'sources': ['x'], 'extra_sources': [1]}}},
        second / 'e.json': {'contract': contract, 'launchers': {
            'empty-extra-mcp': {'sources': ['x'], 'extra_sources': []}}},
        second / 'f.json': ['not', 'a', 'document'],
    }
    for path, document in documents.items():
        path.write_text(json.dumps(document), encoding='utf-8')
    (second / 'g.json').write_text('{broken', encoding='utf-8')
    (second / 'ignored.txt').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(tofu_extensions, 'bundle_directories', lambda kind: [first, second])
    expected = vendored._bundle_launchers()
    rs = rust('bundle_launchers', directories=[str(first), str(second)])
    assert rs['rows'] == expected
    assert list(rs['rows']) == list(expected)
    # b (contract), c (empty sources), d (non-string extra), f (not a mapping), g (not JSON).
    assert [Path(line.split('skipping launcher file ')[1].split(':')[0]).name for line in rs['skipped']] == [
        'b.json', 'c.json', 'd.json', 'f.json', 'g.json']


# ── proxy, egress and the negative cache (test_mcp_prewarm_resilience.py) ──


@pytest.mark.parametrize('case', ['standard', 'alias', 'caller_wins', 'pool_fallback', 'pool_never_overrides',
                                  'pool_empty'])
def test_propagate_proxy_env(rust, launcher_env, monkeypatch, case):
    import lib.proxy as lib_proxy
    env: dict[str, str] = {}
    if case == 'standard':
        monkeypatch.setenv('HTTPS_PROXY', 'http://proxy:8080')
        monkeypatch.setenv('http_proxy', 'http://proxy-lower:8080')
        monkeypatch.setenv('ALL_PROXY', 'socks5://proxy:1080')
    elif case == 'alias':
        monkeypatch.setenv('TOFU_HTTPS_PROXY', 'http://alias:9090')
        monkeypatch.setenv('TOFU_NO_PROXY', 'localhost')
    elif case == 'caller_wins':
        monkeypatch.setenv('HTTPS_PROXY', 'http://from-env:8080')
        env = {'HTTPS_PROXY': 'http://from-caller:8080'}
    elif case == 'pool_fallback':
        monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url',
                            lambda *a, **k: 'http://user:pass@pool-proxy:8080')
    elif case == 'pool_never_overrides':
        monkeypatch.setenv('HTTPS_PROXY', 'http://explicit:8080')
        monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url', lambda *a, **k: 'http://pool:8080')
    python = dict(env)
    vendor._propagate_proxy_env(python)
    assert rust('propagate_proxy_env', env=env) == python
    if case == 'pool_fallback':
        assert python['http_proxy'] == 'http://user:pass@pool-proxy:8080'
    if case == 'pool_empty':
        assert python == {}


def _negative_cache_steps(client, set_egress, case):
    """One oracle scenario; returns what each read answered."""
    seen = [client.reason()]
    if case == 'roundtrip':
        client.note('package index unreachable: dns error')
        seen.append(client.reason())
    elif case == 'expires':
        client.note('stale')
        data = json.loads(Path(client.path()).read_text())
        data['ts'] = time.time() - 7 * 3600
        Path(client.path()).write_text(json.dumps(data))
        seen.append(client.reason())
    elif case == 'disabled_when_zero':
        client.note('nope')
        seen.append(client.reason())
    elif case == 'egress_changed':
        client.note('package index unreachable: dns error')
        seen.append(client.reason())
        set_egress('http://10.213.87.132:8080')
        seen.append(client.reason())
    elif case == 'egress_unchanged':
        set_egress('http://gw:8080')
        client.note('package index unreachable: dns error')
        seen.append(client.reason())
    elif case == 'legacy_marker':
        set_egress('http://gw:8080')
        os.makedirs(os.path.dirname(client.path()), exist_ok=True)
        Path(client.path()).write_text(json.dumps({'ts': time.time(), 'reason': 'legacy'}))
        seen.append(client.reason())
    return seen


class _PyCache:
    reason = staticmethod(vendor._prewarm_unreachable_reason)
    note = staticmethod(vendor._prewarm_note_unreachable)
    path = staticmethod(vendor._prewarm_unreachable_marker_path)


class _RsCache:
    def __init__(self, rust):
        self.rust = rust

    def reason(self):
        return self.rust('unreachable_reason')

    def note(self, reason):
        self.rust('note_unreachable', reason=reason)

    def path(self):
        return self.rust('unreachable_marker_path')


@pytest.mark.parametrize('case', ['roundtrip', 'expires', 'disabled_when_zero', 'egress_changed',
                                  'egress_unchanged', 'legacy_marker'])
def test_negative_cache(rust, launcher_env, monkeypatch, case):
    import lib.proxy as lib_proxy
    monkeypatch.setenv('TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS', '0' if case == 'disabled_when_zero' else '6')

    def set_egress(url):
        monkeypatch.setattr(lib_proxy, 'first_global_proxy_url', lambda: url)

    python = _negative_cache_steps(_PyCache, set_egress, case)
    set_egress('')
    rs = _negative_cache_steps(_RsCache(rust), set_egress, case)
    assert rs == python
    assert python[-1] == ('package index unreachable: dns error'
                          if case in ('roundtrip', 'egress_unchanged') else '')


def test_negative_cache_markers_are_interchangeable(rust, launcher_env, monkeypatch):
    """A marker one client wrote suppresses (and one it skipped warms) the other's pre-warm."""
    _register(launcher_env, monkeypatch, 'neg-mcp', 'vendored')
    uv = FakeUv(launcher_env, monkeypatch)
    uv.set(rc=2, stderr='error: Failed to fetch: https://pypi.org/simple/mcp/\n'
                        'Caused by: dns error: failed to lookup address: Name or service not known')
    rs = rust('prewarm', command='neg-mcp')
    assert rs[0] is False and rs[1].startswith('uv warm exited 2: ')
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(rust.cache))
    cached = vendor._prewarm_unreachable_reason()
    assert cached == 'package index unreachable: ' + rs[1][len('uv warm exited 2: '):]
    assert list(mc.prewarm_vendored_launcher('neg-mcp')) == [False, cached]
    assert rust('prewarm', command='neg-mcp') == [False, cached]
    assert rust('prewarm_all') == mc.prewarm_all_vendored() == {'neg-mcp': cached}
    assert len(uv.take()) == 1, 'no warm may run while the negative cache holds'


@pytest.mark.parametrize('url', ['http://alice:s3cr3t-token@gw.internal:8080', '', 'socks5://H', 'http://[::1]:8080',
                                 'http://h:bad', 'http://h:99999', 'http://u@h:0', 'localhost', 'gw:3128'])
def test_egress_identity(rust, launcher_env, monkeypatch, url):
    import lib.proxy as lib_proxy
    monkeypatch.setattr(lib_proxy, 'first_global_proxy_url', lambda: url)
    expected = vendor._egress_identity()
    assert rust('egress_identity') == expected
    assert 's3cr3t-token' not in expected
    if not url:
        assert expected == 'direct'
        monkeypatch.setenv('no_proxy', 'localhost')
        monkeypatch.setenv('TOFU_ALL_PROXY', 'http://alias-gw:1')
        rust.start()
        assert rust('egress_identity') == vendor._egress_identity()


@pytest.mark.parametrize('text', [
    'Failed to fetch https://pypi.org/simple/mcp/ dns error: Name or service not known',
    'error: distribution mcp not found (some build conflict)', '',
    'Could not resolve host: files.pythonhosted.org/simple', 'Temporary failure in name resolution (PyPI)',
    'dns error but no index named'])
def test_is_index_unreachable(rust, launcher_env, text):
    assert rust('is_index_unreachable', text=text) is vendor._is_index_unreachable(text)


# ── SDK pins and the supply cutoff (test_mcp_sdk_pin_bounded.py) ─────


def test_rmcp_is_pinned_exactly_and_locked():
    """The Rust client's SDK: rmcp pinned with ``=`` and resolved in a committed Cargo.lock."""
    manifest = tomllib.loads((CRATE / 'Cargo.toml').read_text(encoding='utf-8'))
    rmcp = manifest['dependencies']['rmcp']
    assert re.fullmatch(r'=\d+\.\d+\.\d+', rmcp['version']), rmcp
    lock = tomllib.loads((CRATE / 'Cargo.lock').read_text(encoding='utf-8'))
    locked = [p['version'] for p in lock['package'] if p['name'] == 'rmcp']
    assert locked == [rmcp['version'][1:]]
    tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', 'packages/tofu-mcp/Cargo.lock'],
                             cwd=ROOT, capture_output=True, text=True)
    assert tracked.returncode == 0, 'Cargo.lock must be committed so the SDK resolve is reproducible'


def test_launcher_env_carries_the_supply_cutoff(rust, launcher_env):
    python: dict[str, str] = {}
    vendor._ensure_writable_caches(python)
    rs = rust('ensure_writable_caches', env={})
    rs_cache = str(rust.cache)
    assert rs == {k: v.replace(str(launcher_env / 'py-cache'), rs_cache) for k, v in python.items()}
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', rs['npm_config_before'])
    assert rs['UV_EXCLUDE_NEWER'].startswith(rs['npm_config_before'])
    assert rs['UV_EXCLUDE_NEWER'] == vendor._SUPPLY_CUTOFF_DEFAULT


def test_only_exact_source_pins_are_exempt_from_cutoff(rust, launcher_env, monkeypatch):
    src = launcher_env / 'pin-mcp'
    src.mkdir()
    (src / 'pyproject.toml').write_text(
        '[project]\nname = "pin-mcp"\nversion = "0"\ndependencies = [\n'
        '  "Exact_Pkg[extra]==1.2.3",\n  "marked==2.0; python_version >= \'3\'",\n'
        '  "floating>=1",\n  "wild==1.*",\n  "ranged==1.0,<2",\n  "triple.dot===3",\n  " spaced == 4 ",\n]\n')
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'pin-mcp', {'sources': [str(src)]})
    expected = mc.vendored_launch_argv('pin-mcp')
    assert rust('vendored_launch_argv', command='pin-mcp') == expected
    assert rust('exact_pin_exemptions', sources=[str(src), str(launcher_env / 'missing')]) == \
        vendor._exact_pin_exemptions([str(src), str(launcher_env / 'missing')])
    (src / 'pyproject.toml').write_text('not toml [[[')
    assert rust('vendored_launch_argv', command='pin-mcp') == mc.vendored_launch_argv('pin-mcp') == [
        'uv', 'run', '--no-project', '--with-editable', str(src), 'pin-mcp']


def test_manifest_change_invalidates_warm_marker(rust, launcher_env, monkeypatch):
    src = launcher_env / 'bump-mcp'
    src.mkdir()
    manifest = src / 'pyproject.toml'
    manifest.write_text('[project]\nname = "bump-mcp"\nversion = "0"\ndependencies = ["x==1", "ü-pkg>=1"]\n')
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'bump-mcp', {'sources': [str(src)]})
    assert rust('launch_fingerprint', command='bump-mcp') == vendor._launch_fingerprint('bump-mcp')
    assert rust('needs_cold_budget', command='bump-mcp') is mc.vendored_needs_cold_budget('bump-mcp') is True
    rust('note_warm', command='bump-mcp')
    vendor._prewarm_note_warm('bump-mcp')
    assert rust('needs_cold_budget', command='bump-mcp') is mc.vendored_needs_cold_budget('bump-mcp') is False
    # Each client accepts the other's warm marker: the fingerprints are byte-identical.
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(rust.cache))
    assert mc.vendored_needs_cold_budget('bump-mcp') is False
    manifest.write_text(manifest.read_text().replace('x==1', 'x==2'))
    assert rust('needs_cold_budget', command='bump-mcp') is mc.vendored_needs_cold_budget('bump-mcp') is True


def test_supply_cutoff_is_operator_overridable_and_optional(rust, launcher_env, monkeypatch):
    kept = {'UV_EXCLUDE_NEWER': '2030-01-01T00:00:00Z'}
    assert rust('ensure_writable_caches', env=kept)['UV_EXCLUDE_NEWER'] == '2030-01-01T00:00:00Z'
    monkeypatch.setenv('TOFU_MCP_SUPPLY_CUTOFF', '')
    rust.start()
    opted_out = rust('ensure_writable_caches', env={})
    python: dict[str, str] = {}
    vendor._ensure_writable_caches(python)
    for key in ('UV_EXCLUDE_NEWER', 'npm_config_before', 'UV_PYTHON'):
        assert key not in opted_out and key not in python
    monkeypatch.setenv('TOFU_MCP_SUPPLY_CUTOFF', ' 2025-01-02T00:00:00Z ')
    rust.start()
    python = {}
    vendor._ensure_writable_caches(python)
    rs = rust('ensure_writable_caches', env={})
    assert (rs['UV_EXCLUDE_NEWER'], rs['npm_config_before']) == (
        python['UV_EXCLUDE_NEWER'], python['npm_config_before']) == ('2025-01-02T00:00:00Z', '2025-01-02')


def _npx_tree(root: Path, cutoff: str) -> None:
    npx = root / '_npx'
    stale = npx / 'deadbeefcafe0001'
    stale.mkdir(parents=True)
    (stale / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (stale / 'package.json').write_text('{"dependencies":{"12306-mcp":"^0.3.10"}}')
    (stale / 'node_modules').mkdir()
    fresh = npx / 'deadbeefcafe0002'
    fresh.mkdir(parents=True)
    (fresh / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (fresh / vendor._NPX_CUTOFF_MARKER).write_text(cutoff + '\n')
    (fresh / 'node_modules').mkdir()
    lockless = npx / 'deadbeefcafe0003'
    lockless.mkdir(parents=True)
    (lockless / 'package.json').write_text('{}')
    other = npx / 'deadbeefcafe0004'
    other.mkdir(parents=True)
    (other / 'package-lock.json').write_text('{}')
    (other / vendor._NPX_CUTOFF_MARKER).write_text('2020-01-01T00:00:00Z')


def _listing(root: Path) -> list[tuple[str, str]]:
    return sorted((str(p.relative_to(root)), p.read_text() if p.is_file() else '/') for p in root.rglob('*'))


def test_stale_npx_slot_is_reconciled_against_the_cutoff(rust, launcher_env):
    cutoff = '2026-07-27T00:00:00Z'
    py_root, rs_root = launcher_env / 'py-npm', launcher_env / 'rs-npm'
    _npx_tree(py_root, cutoff)
    _npx_tree(rs_root, cutoff)
    python = vendor._reconcile_npx_cache(str(py_root), cutoff)
    rs = rust('reconcile_npx_cache', npm_cache=str(rs_root), cutoff=cutoff)
    assert rs == python == 2
    assert _listing(rs_root) == _listing(py_root)
    assert rust('reconcile_npx_cache', npm_cache=str(rs_root), cutoff=cutoff) == 0
    assert rust('reconcile_npx_cache', npm_cache=str(launcher_env / 'absent'), cutoff=cutoff) == 0


def test_reconcile_runs_before_the_readiness_timer_not_inside_it(rust, launcher_env):
    slot = rust.cache / 'npm' / '_npx' / 'deadbeefcafe0004'
    slot.mkdir(parents=True)
    (slot / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (slot / 'node_modules').mkdir()
    env = rust('ensure_writable_caches', env={})
    assert (slot / 'node_modules').exists(), 'building the launcher env must never evict'
    assert rust('reconcile_for_connect') == 1
    assert not (slot / 'node_modules').exists()
    assert (slot / vendor._NPX_CUTOFF_MARKER).read_text().strip() == env['UV_EXCLUDE_NEWER']


def test_pending_rebuild_is_visible_to_every_connect(rust, launcher_env, monkeypatch):
    cutoff = '2026-07-27T00:00:00Z'
    npx = launcher_env / 'pending' / '_npx'
    emptied = npx / 'aaaa0001'
    emptied.mkdir(parents=True)
    (emptied / vendor._NPX_CUTOFF_MARKER).write_text(cutoff)
    rebuilt = npx / 'aaaa0002'
    rebuilt.mkdir(parents=True)
    (rebuilt / vendor._NPX_CUTOFF_MARKER).write_text(cutoff)
    (rebuilt / 'package-lock.json').write_text('{}')
    foreign = npx / 'aaaa0003'
    foreign.mkdir(parents=True)
    (foreign / 'package-lock.json').write_text('{}')
    (npx / 'aaaa0004').mkdir()
    root = str(launcher_env / 'pending')
    assert rust('npx_rebuild_pending', npm_cache=root) == vendor._npx_rebuild_pending(root, cutoff) == 1
    (emptied / 'package-lock.json').write_text('{}')
    assert rust('npx_rebuild_pending', npm_cache=root) == vendor._npx_rebuild_pending(root, cutoff) == 0

    for cache in (launcher_env / 'py-cache', rust.cache):
        stale = cache / 'npm' / '_npx' / 'bbbb0001'
        stale.mkdir(parents=True)
        (stale / 'package-lock.json').write_text('{}')
    assert [vendor.reconcile_for_connect(), vendor.reconcile_for_connect()] == [1, 1]
    assert [rust('reconcile_for_connect'), rust('reconcile_for_connect')] == [1, 1]
