"""The Rust MCP client's lifecycle (packages/tofu-mcp/src/bridge/lifecycle.rs) keeps the Python bridge's.

Every case runs the same servers through both clients: the Python ``MCPBridge``
(the oracle) and the Rust bridge in ``tofu-mcp-host``. Servers are the recorded
replay (tests/fixtures/mcp_sessions/lifecycle_like.json) and the oracle suites'
own raw stdio peers. Covered: the keepalive/liveness sweep (an uncached
``tools/list`` probe, no ping, no reconnect storm; a dead child reconnected),
reconnects and the circuit breaker, first-connect recovery through
``connect_all``, idle parking that kills the child and wakes on the next call,
``tools/list_changed``, the protocol compatibility notice and the 2026-07-28
negotiation, reconnects re-reading the catalog into a task's live schemas,
failure classification by JSON-RPC code, the degraded fast-fail and replay
rules over recorded codes, and shutdown disposal.

Time-driven Rust cases use the host's deterministic seams (``sweep_once``,
``advance_clock``); the Python side runs its shipped ``_keepalive_loop`` for a
short window, as the oracle suites do.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from tests.mcp_protocol.rust_parity_support import (  # noqa: F401 - fixtures
    SESSIONS, mcp_host_binary, python_bridge, rust_host, scene,
)
__all__ = ['mcp_host_binary', 'rust_host', 'scene']

pytestmark = pytest.mark.unit

LIFE = 'lifecycle_like.json'

COVERS: dict[str, list[str]] = {
    'test_mcp_liveness_probe.py': [
        'test_failure_codes_classify_identically',
        'test_liveness_sweep_probes_with_uncached_tools_list_and_never_reconnects',
        'test_a_killed_child_is_reconnected_by_the_sweep',
        'cargo:tests/lifecycle.rs::liveness_ping_method_not_found_never_reconnects',
        'cargo:tests/lifecycle.rs::liveness_falls_back_to_a_probe_the_peer_implements',
        'cargo:tests/lifecycle.rs::liveness_memo_stops_repaying_the_fallback_walk',
        'cargo:tests/lifecycle.rs::liveness_prefers_server_discover',
        'cargo:tests/lifecycle.rs::liveness_modern_session_never_pings',
        'cargo:tests/lifecycle.rs::liveness_sdk_v2_client_never_pings_a_legacy_peer',
        'cargo:tests/lifecycle.rs::liveness_skips_a_probe_that_needs_arguments',
        'cargo:tests/lifecycle.rs::liveness_healthy_ping_short_circuits',
        'cargo:tests/lifecycle.rs::liveness_other_protocol_error_is_alive_without_moving_on',
        'cargo:tests/lifecycle.rs::liveness_dead_pipe_triggers_reconnect',
        'cargo:tests/lifecycle.rs::liveness_peer_that_never_answers_triggers_reconnect',
        'cargo:tests/lifecycle.rs::liveness_credential_probe_runs_when_ping_answers_method_not_found',
        'cargo:tests/lifecycle.rs::liveness_credential_probe_skipped_on_a_dead_transport',
        'cargo:tests/lifecycle.rs::liveness_no_usable_probe_reads_alive',
    ],
    'test_mcp_idle_parking.py': [
        'test_idle_stdio_server_exits_and_transparently_restarts',
        'test_idle_parking_skips_young_servers',
        'test_maintenance_parks_before_liveness_work',
        'cargo:tests/lifecycle.rs::idle_parking_releases_transport_but_preserves_catalog',
        'cargo:tests/lifecycle.rs::idle_parking_skips_active_young_and_remote_servers',
        'cargo:tests/lifecycle.rs::idle_parked_catalog_wakes_on_the_next_call',
        'cargo:tests/lifecycle.rs::idle_call_activity_is_released_on_failure',
        'cargo:tests/lifecycle.rs::idle_maintenance_parks_before_liveness_work',
        'cargo:tests/lifecycle.rs::sweep_reconnects_inline_on_the_current_thread_runtime',
        'cargo:tests/lifecycle.rs::credential_probe_is_singleflight_per_server_and_unbounded_across_servers',
    ],
    'test_mcp_v2_protocol.py': [
        'test_protocol_negotiation_and_compatibility_notice_match',
        'test_idle_stdio_server_exits_and_transparently_restarts',
    ],
    'test_mcp_handshake_deadline_scope.py': [
        'test_explicit_per_server_budget_still_bounds_the_call',
        'test_unbudgeted_call_waits_past_the_handshake_phase',
        'cargo:tests/lifecycle.rs::unbudgeted_call_outlives_the_handshake_budget',
        'cargo:tests/lifecycle.rs::explicit_per_server_budget_still_bounds_the_call',
    ],
    'test_mcp_recovery_classification.py': [
        'test_failure_codes_classify_identically',
        'test_malformed_codes_are_no_protocol_reply',
        'test_recorded_timeout_codes_drive_the_degraded_gate',
        'test_answered_parameter_errors_neither_reconnect_nor_block_correction',
        'test_peer_answer_breaks_a_timeout_streak',
    ],
    'test_mcp_error_classify_rename_safe.py': [
        'test_failure_codes_classify_identically',
        'test_recorded_timeout_codes_drive_the_degraded_gate',
    ],
    'test_mcp_initial_connect_recovery.py': [
        'test_a_failed_first_connect_is_retried_by_the_keepalive',
        'test_a_server_disabled_since_boot_is_forgotten',
        'cargo:tests/lifecycle.rs::failed_first_connect_is_retried_by_the_keepalive',
        'cargo:tests/lifecycle.rs::retry_connects_with_the_stored_row_and_resets_the_breaker',
        'cargo:tests/lifecycle.rs::server_disabled_since_boot_is_forgotten_not_retried',
        'cargo:tests/lifecycle.rs::forget_disconnect_clears_a_first_connect_backoff',
        'cargo:tests/lifecycle.rs::failed_runtime_connect_handed_to_the_keepalive_is_retried',
    ],
    'test_mcp_reconnect_rereads_catalog.py': [
        'test_every_reconnect_rereads_tool_definitions',
        'cargo:tests/lifecycle.rs::reconnect_rereads_tool_definitions',
        'cargo:tests/lifecycle.rs::crashed_server_recovered_by_the_next_call_rereads_tool_definitions',
        'cargo:tests/lifecycle.rs::explicit_connect_of_a_connected_server_rereads_tool_definitions',
    ],
    'test_mcp_live_schema_refresh.py': [
        'test_task_calls_after_a_server_upgrade_use_the_live_schema',
        'test_list_changed_rebuilds_the_catalog_without_reconnect',
        'test_list_changed_on_a_2026_07_28_session_refreshes_both_catalogs',
        'cargo:tests/lifecycle.rs::list_changed_replaces_the_catalog_and_bumps_the_generation',
        'cargo:tests/lifecycle.rs::list_changed_from_a_replaced_session_is_ignored',
    ],
    'test_mcp_loop_shutdown.py': [
        'test_shutdown_disposes_every_child_and_admits_no_call',
        'cargo:tests/lifecycle.rs::disconnect_all_closes_every_transport_before_returning',
        'cargo:tests/lifecycle.rs::connection_cannot_publish_after_shutdown',
        'cargo:tests/lifecycle.rs::calls_after_shutdown_are_refused_without_a_send',
        'cargo:tests/lifecycle.rs::maintenance_task_ends_when_stopped',
    ],
    'test_mcp_submission_lifecycle.py': [
        'cargo:tests/lifecycle.rs::a_stuck_close_is_bounded_by_the_disconnect_budget',
        'cargo:tests/lifecycle.rs::maintenance_task_ends_when_stopped',
        'cargo:tests/lifecycle.rs::connection_cannot_publish_after_shutdown',
    ],
    'test_mcp_startup_lifecycle.py': [
        'test_shutdown_disposes_every_child_and_admits_no_call',
        'cargo:tests/lifecycle.rs::connection_cannot_publish_after_shutdown',
        'cargo:tests/lifecycle.rs::maintenance_task_ends_when_stopped',
    ],
    'test_mcp_disconnect_recovery.py': [
        'test_dead_transport_replay_follows_the_read_declaration',
        'test_a_dead_transport_refreshes_the_catalog_without_replaying_the_call',
        'test_replay_override_must_be_a_host_boolean',
    ],
    'test_mcp_call_health.py': [
        'test_recorded_timeout_codes_drive_the_degraded_gate',
        'test_a_declared_wait_extends_the_configured_budget',
        'test_tool_level_errors_never_count_as_timeouts',
        'test_explicit_per_server_budget_still_bounds_the_call',
    ],
}


# ── harness ──────────────────────────────────────────────────────────────────

def _host_params(**params):
    return {'owner_user_id': 1, **params}


class Pair:
    """One server per client, each with its own replay log, state and pid list."""

    def __init__(self, scene, monkeypatch, host, *, session=LIFE, name='life', extra=None):
        from lib.mcp.rust_host import RustMCPBridge
        self.scene, self.host, self.name = scene, host, name
        host.callbacks['mcp.config_rows'] = lambda params: {}
        self.config = {}
        for side in ('py', 'rs'):
            self.config[side] = {'command': scene.launcher(f'{side}-{name}', 'life-mcp', session),
                                 **(extra or {})}
        self.py = python_bridge(monkeypatch)
        self.rs = RustMCPBridge(host, 1)
        self.py.connect_server(name, dict(self.config['py']))
        self.rs.connect_server(name, dict(self.config['rs']))

    def client(self, side):
        return self.py if side == 'py' else self.rs

    def methods(self, side):
        log = self.scene.tmp / f'{side}-{self.name}.log'
        if not log.exists():
            return []
        return [json.loads(line)['method'] for line in log.read_text(encoding='utf-8').splitlines()]

    def pids(self, side):
        path = Path(f'{self.scene.tmp / (side + "-" + self.name + ".state")}.pids')
        return [int(p) for p in path.read_text().split()] if path.exists() else []

    def host_call(self, method, **params):
        return self.host.call(method, _host_params(**params), timeout=60)

    def call(self, side, tool, arguments=None, **options):
        return self.client(side).call_tool(f'mcp__{self.name}__{tool}', arguments or {}, **options)

    def outcome(self, side, tool, arguments=None, **options):
        try:
            return ('ok', str(self.call(side, tool, arguments, **options)))
        except Exception as exc:  # noqa: BLE001 - the failure category is the evidence
            return ('error', type(exc).__name__)


@pytest.fixture
def pair(scene, monkeypatch, rust_host):
    made = Pair(scene, monkeypatch, rust_host)
    yield made
    made.py.disconnect_all()
    made.rs.disconnect_all()


def _python_sweep(bridge, monkeypatch, *, window=0.4, interval=0.05):
    """Run the shipped ``_keepalive_loop`` on the bridge's own loop for ``window``."""
    from lib.mcp.client import _bridge as B
    monkeypatch.setattr(B, 'MCP_KEEPALIVE_INTERVAL', interval)

    async def run():
        bridge._keepalive_stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(bridge._keepalive_loop())
        await asyncio.sleep(window)
        bridge._keepalive_stop.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    bridge._run_async(run(), timeout=120)


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition never held'
        time.sleep(0.02)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as stat:
            return stat.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


def _kill(pid):
    os.kill(pid, signal.SIGKILL)
    _wait(lambda: not _alive(pid))


# ── failure classification (by JSON-RPC code, never prose) ───────────────────

CODES = [
    (-32602, 'Invalid timeout parameter: expected a positive integer.'),
    (-32601, 'Unknown method connection closed.'),
    (-32601, '方法未找到'),
    (-32601, ''),
    (-32042, 'Upstream connection closed after business timeout.'),
    (-32001, "Request 'tools/call' timed out"),
    (-32001, '等待响应超时'),
    (-32000, '连接已关闭'),
    (-32000, 'Connection closed during timeout handling.'),
]


@pytest.mark.parametrize('code,message', CODES)
def test_failure_codes_classify_identically(rust_host, code, message):
    from tests.mcp_protocol.test_mcp_recovery_classification import protocol_error
    from lib.mcp.client._errors import (
        _is_call_timeout_error, _is_method_not_found, _is_peer_answered_error, _is_transport_dead_error,
    )
    error = protocol_error(code, message)
    expected = {
        'timeout': _is_call_timeout_error(error), 'dead': _is_transport_dead_error(error),
        'peer_answered': _is_peer_answered_error(error), 'method_not_found': _is_method_not_found(error),
    }
    got = rust_host.call('classify_error', {'code': code, 'message': message}, timeout=30)
    assert {k: got[k] for k in expected} == expected


@pytest.mark.parametrize('code', [True, False, '-32001', -32001.0])
def test_malformed_codes_are_no_protocol_reply(rust_host, code):
    from types import SimpleNamespace
    from lib.mcp.client._errors import _is_peer_answered_error
    error = RuntimeError('unclassified failure')
    error.error = SimpleNamespace(code=code)
    got = rust_host.call('classify_error', {'code': code, 'message': 'unclassified failure'}, timeout=30)
    assert got['peer_answered'] is _is_peer_answered_error(error) is False


# ── call health over recorded codes ──────────────────────────────────────────

@pytest.mark.parametrize('tool', ['peer_timeout', 'peer_timeout_cn'])
def test_recorded_timeout_codes_drive_the_degraded_gate(scene, monkeypatch, rust_host, tool):
    from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
    made = Pair(scene, monkeypatch, rust_host, extra={'timeout': 360})
    try:
        for side in ('py', 'rs'):
            outcomes = [made.outcome(side, tool) for _ in range(MCP_DEGRADED_TIMEOUT_STREAK)]
            assert all(kind == 'error' for kind, _ in outcomes), (side, outcomes)
            fast = made.call(side, tool)
            assert 'degraded' in str(fast) and 'configured call budget: 360s' in str(fast), side
        sent = {side: made.methods(side).count('tools/call') for side in ('py', 'rs')}
        assert sent == {'py': MCP_DEGRADED_TIMEOUT_STREAK, 'rs': MCP_DEGRADED_TIMEOUT_STREAK}
    finally:
        made.py.disconnect_all()
        made.rs.disconnect_all()


def test_a_declared_wait_extends_the_configured_budget(scene, monkeypatch, rust_host):
    from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
    made = Pair(scene, monkeypatch, rust_host, extra={'timeout': 360})
    try:
        texts = {}
        for side in ('py', 'rs'):
            for _ in range(MCP_DEGRADED_TIMEOUT_STREAK):
                made.outcome(side, 'watch', {'timeout_sec': 900})
            texts[side] = str(made.call(side, 'watch', {'timeout_sec': 900}))
        assert texts['py'] == texts['rs']
        assert 'configured call budget: 960s' in texts['rs']
    finally:
        made.py.disconnect_all()
        made.rs.disconnect_all()


@pytest.mark.parametrize('tool', ['peer_invalid'])
def test_answered_parameter_errors_neither_reconnect_nor_block_correction(pair, tool):
    from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
    for side in ('py', 'rs'):
        outcomes = [pair.outcome(side, tool) for _ in range(MCP_DEGRADED_TIMEOUT_STREAK + 1)]
        assert all(kind == 'error' for kind, _ in outcomes)
        assert pair.methods(side).count('initialize') == 1, f'{side} reconnected on an answered error'
        assert pair.methods(side).count('tools/call') == MCP_DEGRADED_TIMEOUT_STREAK + 1
        assert pair.outcome(side, 'invalid_then_ok')[0] == 'error'
        assert pair.outcome(side, 'invalid_then_ok') == ('ok', 'corrected response')


def test_peer_answer_breaks_a_timeout_streak(scene, monkeypatch, rust_host):
    from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
    made = Pair(scene, monkeypatch, rust_host, extra={'timeout': 360})
    try:
        for side in ('py', 'rs'):
            for _ in range(MCP_DEGRADED_TIMEOUT_STREAK - 1):
                made.outcome(side, 'peer_timeout')
            made.outcome(side, 'peer_invalid')
            assert made.outcome(side, 'peer_timeout')[0] == 'error'
            assert 'degraded' not in made.outcome(side, 'peer_timeout')[1], side
    finally:
        made.py.disconnect_all()
        made.rs.disconnect_all()


def test_tool_level_errors_never_count_as_timeouts(pair):
    from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
    for side in ('py', 'rs'):
        for _ in range(MCP_DEGRADED_TIMEOUT_STREAK + 2):
            assert pair.outcome(side, 'peer_invalid')[0] == 'error'
        assert pair.methods(side).count('tools/call') == MCP_DEGRADED_TIMEOUT_STREAK + 2


def test_explicit_per_server_budget_still_bounds_the_call(scene, monkeypatch, rust_host):
    from concurrent.futures import TimeoutError as FutureTimeout
    made = Pair(scene, monkeypatch, rust_host, extra={'timeout': 1})
    try:
        for side in ('py', 'rs'):
            kind, name = made.outcome(side, 'slower')
            assert kind == 'error' and name in ('TimeoutError', FutureTimeout.__name__, 'MCPError', 'McpError'), (side, name)
    finally:
        made.py.disconnect_all()
        made.rs.disconnect_all()


def test_unbudgeted_call_waits_past_the_handshake_phase(pair):
    for side in ('py', 'rs'):
        assert pair.outcome(side, 'slow') == ('ok', 'slow-ok')


# ── replay after a dead transport ────────────────────────────────────────────

@pytest.mark.parametrize('tool,retry,replayed', [
    ('crash_read', None, True),
    ('crash_write', None, False),
    ('crash_write', True, True),
    ('crash_read', False, False),
])
def test_dead_transport_replay_follows_the_read_declaration(pair, tool, retry, replayed):
    for side in ('py', 'rs'):
        options = {} if retry is None else {'retry_on_disconnect': retry}
        kind, _ = pair.outcome(side, tool, **options)
        assert (kind == 'ok') is replayed, side
        assert pair.methods(side).count('initialize') == 2, f'{side} did not reconnect'
        assert pair.methods(side).count('tools/call') == (2 if replayed else 1)


def test_a_dead_transport_refreshes_the_catalog_without_replaying_the_call(pair):
    for side in ('py', 'rs'):
        assert pair.outcome(side, 'crash')[0] == 'error'
        assert pair.methods(side).count('initialize') == 2
        assert pair.methods(side).count('tools/call') == 1
        assert pair.client(side).get_tool_info('mcp__life__read_doc') is not None


@pytest.mark.parametrize('value', ['false', 0, 1, [], {}])
def test_replay_override_must_be_a_host_boolean(pair, value):
    for side in ('py', 'rs'):
        with pytest.raises(Exception, match='retry_on_disconnect'):
            pair.call(side, 'read_doc', {'doc': 'a'}, retry_on_disconnect=value)
        assert pair.methods(side).count('tools/call') == 0


# ── liveness sweep, reconnects and the breaker ───────────────────────────────

def test_liveness_sweep_probes_with_uncached_tools_list_and_never_reconnects(pair, monkeypatch):
    _python_sweep(pair.py, monkeypatch)
    for _ in range(3):
        pair.host_call('sweep_once')
    for side in ('py', 'rs'):
        methods = pair.methods(side)
        assert methods.count('tools/list') >= 2, f'{side} never probed'
        assert 'ping' not in methods, f'{side} pinged a peer through an SDK-v2 style client'
        assert methods.count('initialize') == 1, f'{side} reconnected a live peer'
    assert pair.py._probe_method.get('life') == 'list_tools'
    assert pair.host_call('lifecycle_state', name='life')['probe_method'] == 'list_tools'


def test_a_killed_child_is_reconnected_by_the_sweep(pair, monkeypatch):
    for side in ('py', 'rs'):
        _kill(pair.pids(side)[-1])
    _python_sweep(pair.py, monkeypatch, window=1.0)
    pair.host_call('sweep_once')
    for side in ('py', 'rs'):
        assert pair.methods(side).count('initialize') == 2, side
        assert len(pair.pids(side)) == 2 and _alive(pair.pids(side)[-1]), side
        assert pair.outcome(side, 'read_doc', {'doc': 'a'}) == ('ok', 'doc body')


def _breaker(pair, side):
    if side == 'py':
        return pair.py.get_breaker_state('life')
    return pair.host_call('breaker_state', name='life')


def test_failed_reconnects_back_off_identically_and_heal(pair, monkeypatch):
    shims = {side: Path(pair.config[side]['command']) for side in ('py', 'rs')}
    healthy = {side: shims[side].read_text() for side in shims}
    for side in ('py', 'rs'):
        shims[side].write_text('#!/bin/sh\nexit 3\n')
        _kill(pair.pids(side)[-1])
    for attempt in (1, 2):
        with pytest.raises(Exception):
            pair.py._reconnect_server('life')
        with pytest.raises(Exception):
            pair.host_call('reconnect_server', name='life')
        states = {side: _breaker(pair, side) for side in ('py', 'rs')}
        assert {s: v['failures'] for s, v in states.items()} == {'py': attempt, 'rs': attempt}
        assert {s: round(v['retry_in'] / 10) for s, v in states.items()} == {
            'py': 3 * 2 ** (attempt - 1), 'rs': 3 * 2 ** (attempt - 1)}
    for side in ('py', 'rs'):
        assert pair.client(side).list_servers() == []
        assert pair.client(side).get_tool_info('mcp__life__read_doc') is None
        shims[side].write_text(healthy[side])
    # A sweep inside the backoff leaves the server alone; after it, heals.
    pair.host_call('sweep_once')
    assert pair.host_call('breaker_state', name='life')['failures'] == 2
    with pair.py._lock:
        pair.py._breaker['life'] = (2, 0.0)
    pair.host_call('advance_clock', seconds=61)
    _python_sweep(pair.py, monkeypatch, window=1.0)
    pair.host_call('sweep_once')
    for side in ('py', 'rs'):
        assert _breaker(pair, side) is None, side
        assert pair.outcome(side, 'read_doc', {'doc': 'a'}) == ('ok', 'doc body')


def _boot_rows(pair_scene, side):
    return {
        'flaky': {'command': str(pair_scene.tmp / 'bin' / f'{side}-flaky' / 'life-mcp'), 'enabled': True},
        'off': {'command': 'never-launched', 'enabled': False},
    }


def _connect_all_both(scene, monkeypatch, rust_host):
    from lib.mcp.client import _bridge as B
    from lib.mcp.rust_host import RustMCPBridge
    for side in ('py', 'rs'):
        shim = Path(scene.launcher(f'{side}-flaky', 'life-mcp', LIFE))
        shim.with_suffix('.good').write_text(shim.read_text())
        shim.write_text('#!/bin/sh\nexit 3\n')
    rows = {side: _boot_rows(scene, side) for side in ('py', 'rs')}
    py = python_bridge(monkeypatch)
    monkeypatch.setattr(B, 'load_mcp_config', lambda user_id: rows['py'])
    rust_host.callbacks['mcp.config_rows'] = lambda params: rows['rs']
    rs = RustMCPBridge(rust_host, 1)
    assert py.connect_all() == {}
    assert rust_host.call('connect_all', _host_params(), timeout=60) == {}
    return py, rs, rows


def test_a_failed_first_connect_is_retried_by_the_keepalive(scene, monkeypatch, rust_host):
    py, rs, _rows = _connect_all_both(scene, monkeypatch, rust_host)
    try:
        assert py.get_breaker_state('flaky')['failures'] == 1
        assert rust_host.call('breaker_state', _host_params(name='flaky'), timeout=30)['failures'] == 1
        for side in ('py', 'rs'):
            shim = scene.tmp / 'bin' / f'{side}-flaky' / 'life-mcp'
            shim.write_text(shim.with_suffix('.good').read_text())
        with py._lock:
            py._breaker['flaky'] = (1, 0.0)
        rust_host.call('advance_clock', _host_params(seconds=31), timeout=30)
        _python_sweep(py, monkeypatch, window=1.0)
        rust_host.call('sweep_once', _host_params(), timeout=60)
        assert py.get_breaker_state('flaky') is None
        assert rust_host.call('breaker_state', _host_params(name='flaky'), timeout=30) is None
        assert [s['name'] for s in py.list_servers()] == [s['name'] for s in rs.list_servers()] == ['flaky']
    finally:
        py.disconnect_all()
        rs.disconnect_all()


def test_a_server_disabled_since_boot_is_forgotten(scene, monkeypatch, rust_host):
    py, rs, rows = _connect_all_both(scene, monkeypatch, rust_host)
    try:
        disabled = {**rows['py']['flaky'], 'enabled': False}
        monkeypatch.setattr(py, '_stored_server_config', lambda name, refresh_browser=False: (disabled, None))
        rows['rs']['flaky']['enabled'] = False
        with pytest.raises(ValueError, match='disabled'):
            py._reconnect_server('flaky')
        with pytest.raises(Exception, match='MCP server flaky is disabled'):
            rust_host.call('reconnect_server', _host_params(name='flaky'), timeout=60)
        assert py.get_breaker_state('flaky') is None
        assert rust_host.call('breaker_state', _host_params(name='flaky'), timeout=30) is None
        assert rust_host.call('lifecycle_state', _host_params(name='flaky'), timeout=30)['configured'] is False
    finally:
        py.disconnect_all()
        rs.disconnect_all()


# ── idle parking ─────────────────────────────────────────────────────────────

def _park(pair, side, monkeypatch):
    from lib.mcp.client import _bridge as B
    if side == 'py':
        pair.py._last_activity[pair.name] = time.monotonic() - B.MCP_STDIO_IDLE_SECONDS - 1
        return pair.py._park_idle_stdio_server(pair.name)
    pair.host_call('advance_clock', seconds=B.MCP_STDIO_IDLE_SECONDS + 1)
    return pair.host_call('park_idle', name=pair.name)


def test_idle_stdio_server_exits_and_transparently_restarts(pair, monkeypatch):
    for side in ('py', 'rs'):
        old = pair.pids(side)[-1]
        assert _park(pair, side, monkeypatch) is True, side
        _wait(lambda: not _alive(old))
        assert pair.client(side).list_servers()[0]['parked'] is True
        assert pair.client(side).get_tool_info('mcp__life__read_doc') is not None
        assert pair.outcome(side, 'read_doc', {'doc': 'a'}) == ('ok', 'doc body')
        assert pair.pids(side)[-1] != old and _alive(pair.pids(side)[-1])
        assert pair.client(side).list_servers()[0]['parked'] is False


def test_idle_parking_skips_young_servers(pair):
    assert pair.py._park_idle_stdio_server('life') is False
    assert pair.host_call('park_idle', name='life') is False
    for side in ('py', 'rs'):
        assert _alive(pair.pids(side)[-1])


@pytest.mark.parametrize('keepalive', [0, 45])
def test_maintenance_parks_before_liveness_work(scene, monkeypatch, mcp_host_binary, keepalive):
    from lib.mcp.client import _bridge as B
    from lib.mcp.rust_host import RustMCPHost
    host = RustMCPHost(mcp_host_binary, env={**os.environ, 'TOFU_MCP_KEEPALIVE_INTERVAL': str(keepalive)})
    try:
        host.call('configure', scene.host_settings(), timeout=30)
        made = Pair(scene, monkeypatch, host)
        monkeypatch.setattr(B, 'MCP_STDIO_IDLE_SECONDS', 0.01)
        made.py._last_activity['life'] = time.monotonic() - 100
        _python_sweep(made.py, monkeypatch, interval=0.01 if keepalive else 0, window=0.3)
        made.host_call('advance_clock', seconds=24 * 60 * 60 + 1)
        made.host_call('sweep_once')
        for side in ('py', 'rs'):
            assert made.client(side).list_servers()[0]['parked'] is True, side
            after_connect = made.methods(side)[made.methods(side).index('tools/list') + 1:]
            assert 'tools/list' not in after_connect, f'{side} probed a server it parks'
            assert made.methods(side).count('initialize') == 1
        made.py.disconnect_all()
        made.rs.disconnect_all()
    finally:
        host.close()


# ── tools/list_changed and live schemas ──────────────────────────────────────

def _rows(client):
    return {name: (info['input_schema'], info['description']) for name, info, _ in client.tool_index_rows()}


def test_list_changed_rebuilds_the_catalog_without_reconnect(pair):
    generations = {'py': pair.py.tool_catalog_generation(), 'rs': pair.rs.tool_catalog_generation()}
    for side in ('py', 'rs'):
        assert pair.client(side).list_servers()[0]['tools_list_changed'] is True
        assert pair.outcome(side, 'upgrade') == ('ok', 'upgraded')
        _wait(lambda: pair.client(side).get_tool_info('mcp__life__write_doc') is not None)
        assert pair.client(side).tool_catalog_generation() > generations[side]
        assert pair.methods(side).count('initialize') == 1
    assert _rows(pair.py) == _rows(pair.rs)
    assert set(pair.rs.get_tool_info('mcp__life__read_doc')['input_schema']['properties']) == {'doc'}


def _docs_pair(tmp_path, monkeypatch, rust_host):
    from lib.mcp.rust_host import RustMCPBridge
    from tests.mcp_protocol.test_mcp_live_schema_refresh import _SERVER
    rust_host.callbacks['mcp.config_rows'] = lambda params: {}
    sides = {}
    for side in ('py', 'rs'):
        script = tmp_path / side / 'fake_docs_mcp.py'
        script.parent.mkdir()
        script.write_text(_SERVER, encoding='utf-8')
        version = tmp_path / side / 'version'
        version.write_text('old', encoding='utf-8')
        config = {'transport': 'stdio', 'command': sys.executable,
                  'args': [str(script), str(version)], 'enabled': True}
        sides[side] = (script, version, config)
    py = python_bridge(monkeypatch, owner=7)
    rs = RustMCPBridge(rust_host, 7)
    py.connect_server('docs', dict(sides['py'][2]))
    rs.connect_server('docs', dict(sides['rs'][2]))
    return {'py': py, 'rs': rs}, sides


def _script_pid(script):
    import psutil
    found = [p.pid for p in psutil.process_iter(['cmdline'])
             if any(str(script) == part for part in (p.info['cmdline'] or []))]
    assert found, f'{script} is not running'
    return found[0]


@pytest.mark.parametrize('path', ['recovery', 'crash', 'explicit'])
def test_every_reconnect_rereads_tool_definitions(tmp_path, monkeypatch, rust_host, path):
    clients, sides = _docs_pair(tmp_path, monkeypatch, rust_host)
    try:
        for side, client in clients.items():
            script, version, config = sides[side]
            props = client.get_tool_info('mcp__docs__read_doc')['openai_def']['function']['parameters']['properties']
            assert 'max_rows' in props
            version.write_text('new', encoding='utf-8')
            if path == 'recovery':
                if side == 'py':
                    client._reconnect_server('docs')
                else:
                    rust_host.call('reconnect_server', {'owner_user_id': 7, 'name': 'docs'}, timeout=60)
            elif path == 'crash':
                _kill(_script_pid(script))
                with pytest.raises(Exception):
                    client.call_tool('mcp__docs__ping', {})
            else:
                client.connect_server('docs', dict(config))
            props = client.get_tool_info('mcp__docs__read_doc')['openai_def']['function']['parameters']['properties']
            assert set(props) == {'doc'}, side
            assert str(client.call_tool('mcp__docs__read_doc', {'doc': '7'})) == 'doc 7 @new'
    finally:
        for client in clients.values():
            client.disconnect_all()


def test_task_calls_after_a_server_upgrade_use_the_live_schema(tmp_path, monkeypatch, rust_host):
    import lib.mcp
    from lib.mcp.client import _bridge as B
    from lib.tasks_pkg.handlers.tool_gateway import _executable_catalog
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.gateway import normalize_gateway_call
    clients, sides = _docs_pair(tmp_path, monkeypatch, rust_host)
    try:
        for side, client in clients.items():
            monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner, client=client: client)
            catalog = client.get_openai_tool_defs()
            task = {'_userId': 7, '_executable_tool_catalog': catalog,
                    '_toolContractDocumentsByName': compile_execution_contract_documents(catalog, namespace='docs')}
            sides[side][1].write_text('new', encoding='utf-8')
            if side == 'py':
                client._last_activity['docs'] = time.monotonic() - B.MCP_STDIO_IDLE_SECONDS - 1
                assert client._park_idle_stdio_server('docs') is True
            else:
                rust_host.call('advance_clock', {'owner_user_id': 7, 'seconds': B.MCP_STDIO_IDLE_SECONDS + 1}, timeout=30)
                assert rust_host.call('park_idle', {'owner_user_id': 7, 'name': 'docs'}, timeout=30) is True
            assert str(client.call_tool('mcp__docs__ping', {})) == 'pong @new'
            call, error = normalize_gateway_call(
                {'name': 'mcp__docs__read_doc', 'arguments': {'doc': '42'}},
                catalog=_executable_catalog(task), namespace_by_name={}, gateway_call_id='gw', index=0,
                source='execute_calls', contract_documents_by_name=task['_toolContractDocumentsByName'])
            assert error is None and call['_normalized_arguments'] == {'doc': '42'}, side
            assert str(client.call_tool('mcp__docs__read_doc', call['_normalized_arguments'])) == 'doc 42 @new'
            generation = client.tool_catalog_generation()
            client.disconnect_all()
            assert client.tool_catalog_generation() > generation
    finally:
        for client in clients.values():
            client.disconnect_all()


# ── protocol negotiation and the compatibility notice ───────────────────────

@pytest.mark.parametrize('era', ['modern', 'legacy'])
def test_protocol_negotiation_and_compatibility_notice_match(tmp_path, monkeypatch, rust_host, era):
    from lib.mcp.rust_host import RustMCPBridge
    from tests.mcp_protocol.test_mcp_v2_protocol import _RAW_SERVER
    server = tmp_path / f'raw_{era}_mcp.py'
    # rmcp parses the 2026-07-28 DiscoverResult strictly: the schema's required
    # resultType/ttlMs/cacheScope must be present (the oracle's SDK tolerates
    # their absence; see the module's known differences).
    server.write_text(_RAW_SERVER.replace(
        "'supportedVersions': ['2026-07-28'],",
        "'supportedVersions': ['2026-07-28'], 'resultType': 'complete', 'ttlMs': 0, 'cacheScope': 'private',",
    ), encoding='utf-8')
    rows = {}
    for side, client in (('py', python_bridge(monkeypatch)), ('rs', RustMCPBridge(rust_host, 1))):
        trace = tmp_path / f'{side}.trace'
        try:
            client.connect_server(era, {'transport': 'stdio', 'command': sys.executable,
                                        'args': [str(server), era, str(trace)], 'enabled': True})
            result = str(client.call_tool(f'mcp__{era}__echo_era', {}))
            row = client.list_servers()[0]
            rows[side] = (result, row['protocol_version'], row['sdk_generation'], row['compatibility_notice'],
                          row['server_impl_name'], trace.read_text().splitlines()[:2])
        finally:
            client.disconnect_all()
    assert rows['py'] == rows['rs']
    assert rows['rs'][0] == f'{era}-ok'


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_shutdown_disposes_every_child_and_admits_no_call(scene, monkeypatch, mcp_host_binary):
    from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
    host = RustMCPHost(mcp_host_binary, env=dict(os.environ))
    host.call('configure', scene.host_settings(), timeout=30)
    host.callbacks['mcp.config_rows'] = lambda params: {}
    rs = RustMCPBridge(host, 1)
    py = python_bridge(monkeypatch)
    names = ('one', 'two')
    for name in names:
        rs.connect_server(name, {'command': scene.launcher(f'rs-{name}', 'life-mcp', LIFE)})
        py.connect_server(name, {'command': scene.launcher(f'py-{name}', 'life-mcp', LIFE)})

    def pids(side):
        return [int(Path(f'{scene.tmp / (side + "-" + n + ".state")}.pids').read_text().split()[-1]) for n in names]

    rs.disconnect_all()
    py.disconnect_all()
    for side in ('py', 'rs'):
        assert not any(_alive(pid) for pid in pids(side)), f'{side} left a child running'
    with pytest.raises(Exception, match='not connected'):
        rs.call_tool('mcp__one__read_doc', {'doc': 'a'})
    with pytest.raises(ValueError, match='not connected'):
        py.call_tool('mcp__one__read_doc', {'doc': 'a'})
    # The host's own exit disposes a child it still owns.
    rs.connect_server('three', {'command': scene.launcher('rs-three', 'life-mcp', LIFE)})
    child = int(Path(f'{scene.tmp / "rs-three.state"}.pids').read_text().split()[-1])
    assert host.close() == 0
    _wait(lambda: not _alive(child))


# ── tools/list_changed on a 2026-07-28 session ──────────────────────────────

_MODERN_LIST_CHANGED_SERVER = r'''
import json, sys
tools = [{'name': 'upgrade', 'description': 'Add a tool.', 'inputSchema': {'type': 'object', 'properties': {}}}]

def send(message):
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method, request_id = request.get('method', ''), request.get('id')
    if request_id is None:
        continue
    if method == 'server/discover':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {'listChanged': True}},
            'serverInfo': {'name': 'modern-lc', 'version': '1.0'},
            'resultType': 'complete', 'ttlMs': 0, 'cacheScope': 'private'}})
    elif method == 'tools/list':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'tools': tools, 'resultType': 'complete', 'ttlMs': 0, 'cacheScope': 'private'}})
    elif method == 'tools/call':
        if len(tools) == 1:
            tools.append({'name': 'added', 'description': 'New.', 'inputSchema': {'type': 'object', 'properties': {}}})
        send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'content': [{'type': 'text', 'text': 'upgraded'}], 'resultType': 'complete'}})
    else:
        send({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}})
'''


def test_list_changed_on_a_2026_07_28_session_refreshes_both_catalogs(tmp_path, monkeypatch, rust_host):
    from lib.mcp.rust_host import RustMCPBridge
    server = tmp_path / 'modern_list_changed.py'
    server.write_text(_MODERN_LIST_CHANGED_SERVER, encoding='utf-8')
    rust_host.callbacks['mcp.config_rows'] = lambda params: {}
    config = {'transport': 'stdio', 'command': sys.executable, 'args': [str(server)], 'enabled': True}
    clients = {'py': python_bridge(monkeypatch), 'rs': RustMCPBridge(rust_host, 1)}
    try:
        for side, client in clients.items():
            client.connect_server('modern', dict(config))
            row = client.list_servers()[0]
            assert row['protocol_version'] == '2026-07-28', side
            assert row['tools_list_changed'] is True, side
            generation = client.tool_catalog_generation()
            assert str(client.call_tool('mcp__modern__upgrade', {})) == 'upgraded', side
            _wait(lambda client=client: client.get_tool_info('mcp__modern__added') is not None)
            assert client.tool_catalog_generation() > generation, side
    finally:
        for client in clients.values():
            client.disconnect_all()
