"""Execution shards reach the execution host's MCP servers through one relay.

Found live 2026-11-17 (generation 9840324c): every execution shard ran its own
MCP auto-connect, 53 processes x up to 7 servers, and concurrent npx launches
on the shared npm cache left github/12306-train missing in 16/14 processes.
The execution host now owns the one bridge of the execution plane and its
shards relay to it (lib/mcp/bridge_relay.py); npm-cache launchers take the
cache one at a time (lib/mcp/client/_vendor.py::npm_cache_lock).

The servers are the chaos suite's stdio MCP server (tests/chaos/mcp_server.py)
under the names ``hope`` and ``github``; owners and shards are real processes
where the contract is about a process dying.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip('mcp', reason='MCP SDK not installed')

pytestmark = pytest.mark.unit

CHECKOUT = Path(__file__).resolve().parents[2]
SERVER = CHECKOUT / 'tests' / 'chaos' / 'mcp_server.py'
# A test bound for one wait on a real process, not a product timer.
DEADLINE_S = 60.0


def _row(state: Path, command: str = sys.executable) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    (state / 'schema').write_text('v1')
    return {'transport': 'stdio', 'command': command, 'args': [str(SERVER), str(state)],
            'enabled': True}


def _log(state: Path, name: str = 'calls.log') -> list[str]:
    path = state / name
    return path.read_text().splitlines() if path.exists() else []


def _wait(predicate, what: str) -> None:
    deadline = time.monotonic() + DEADLINE_S
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / 'mcp-cache'
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(root))
    monkeypatch.delenv('npm_config_cache', raising=False)
    return root


def _environment(cache: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ('TOFU_PROCESS_ROLE', 'npm_config_cache')}
    env.update({'PYTHONPATH': str(CHECKOUT), 'TOFU_MCP_CACHE_DIR': str(cache)})
    return env


@pytest.fixture
def owner(tmp_path, cache, monkeypatch):
    """An in-process execution host: its bridge, servers and published relay."""
    import lib.mcp.client as client
    from lib.mcp import bridge_relay
    from lib.mcp.client._bridge import MCPBridge
    bridge = MCPBridge(owner_user_id=1)
    monkeypatch.setitem(client._bridges, 1, bridge)
    states = {name: tmp_path / name for name in ('hope', 'github')}
    for name, state in states.items():
        bridge.connect_server(name, _row(state))
    project = tmp_path / 'project'
    server = bridge_relay.RelayServer(bridge_relay.record_path(project))
    server.start()
    try:
        yield bridge, server, project, states
    finally:
        server.close(timeout=DEADLINE_S)
        bridge.disconnect_all()


def _shard(project: Path):
    from lib.mcp import bridge_relay
    client = bridge_relay.RelayClient(bridge_relay.record_path(project))
    return bridge_relay.RelayedMCPBridge(client, 1)


# ── one catalog, one grant ─────────────────────────────────────────────────

def test_a_shard_sees_exactly_the_owners_catalog_and_grants(owner):
    bridge, _server, project, _states = owner
    shard = _shard(project)
    assert shard.connected and shard.server_count == 2
    assert shard.get_openai_tool_defs() == bridge.get_openai_tool_defs()
    assert shard.get_tool_safety() == bridge.get_tool_safety()
    assert shard.get_tool_catalog_snapshot() == bridge.get_tool_catalog_snapshot()
    assert shard.get_tool_catalog_projection()[0] == bridge.get_tool_catalog_projection()[0]
    assert shard.get_tool_info('mcp__hope__chaos_record') == bridge.get_tool_info('mcp__hope__chaos_record')
    assert sorted(row['name'] for row in shard.list_servers()) == ['github', 'hope']
    generation = shard.tool_catalog_generation()
    assert shard.tool_catalog_generation() == generation, 'an unchanged catalog changed identity'

    bridge.set_disabled_tools('hope', ['chaos_record'])
    assert shard.tool_catalog_generation() != generation
    assert shard.get_openai_tool_defs() == bridge.get_openai_tool_defs()
    assert 'mcp__hope__chaos_record' not in shard.get_tool_safety()
    assert ('mcp__hope__chaos_record', True) in [(ns, disabled) for ns, _i, disabled in shard.tool_index_rows()]
    with pytest.raises(ValueError, match='disabled by user'):
        shard.call_tool('mcp__hope__chaos_record', {'note': 'forbidden'})


def test_get_bridge_in_a_shard_is_the_relay_and_the_shard_connects_nothing(owner, monkeypatch):
    from lib.mcp import bridge_relay, get_bridge
    _bridge, _server, project, _states = owner
    try:
        assert bridge_relay.configure_mcp_owner(
            process_role='execution_shard', environ={'TOFU_PROJECT_PATH': str(project)}) == 'relay'
        relayed = get_bridge(1)
        assert isinstance(relayed, bridge_relay.RelayedMCPBridge)
        assert relayed.is_mcp_tool('mcp__github__chaos_record')
        with pytest.raises(RuntimeError, match='owned by the execution host'):
            relayed.connect_server('other', {})
    finally:
        bridge_relay.stop_mcp_owner()
    assert bridge_relay.relayed_bridge(1) is None


# ── calls and their failures ───────────────────────────────────────────────

def test_a_relayed_call_runs_once_and_keeps_its_result_and_failure_kind(owner):
    _bridge, _server, project, states = owner
    shard = _shard(project)
    assert shard.call_tool('mcp__hope__chaos_record', {'note': 'one'}) == 'recorded one'
    assert _log(states['hope']).count('record one') == 1
    with pytest.raises(ValueError):
        shard.call_tool('mcp__nowhere__tool', {})
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        shard.call_tool('mcp__hope__chaos_record', {'note': 'never'}, cancel=cancelled)
    assert 'record never' not in _log(states['hope'])


def test_an_mcp_server_killed_mid_call_is_a_transport_failure_never_replayed(owner):
    from lib.mcp.client._errors import _is_transport_dead_error
    _bridge, _server, project, states = owner
    shard = _shard(project)
    with pytest.raises(Exception) as failure:
        shard.call_tool('mcp__github__chaos_crash', {})
    assert _is_transport_dead_error(failure.value), repr(failure.value)
    assert _log(states['github']).count('crash') == 1, 'a crashing call was replayed'
    # The owner relaunches the server for the next call; every shard sees it.
    assert shard.call_tool('mcp__github__chaos_record', {'note': 'after'}) == 'recorded after'
    assert len(_log(states['github'], 'launches.log')) == 2


_SHARD_CALL = """
import json, sys
from lib.mcp import bridge_relay
from lib.mcp.client import get_bridge
bridge_relay.configure_mcp_owner(process_role='execution_shard', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
print(json.dumps(str(get_bridge(1).call_tool(sys.argv[2], json.loads(sys.argv[3])))), flush=True)
"""


def test_a_shard_killed_mid_call_withdraws_the_owners_call(owner, cache):
    _bridge, server, project, states = owner
    hold = states['hope'] / 'hold-call'
    hold.touch()
    shard = subprocess.Popen(
        [sys.executable, '-c', _SHARD_CALL, str(project), 'mcp__hope__chaos_wait', '{"note": "k"}'],
        env=_environment(cache), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        _wait(lambda: 'wait k' in _log(states['hope']), 'the shard call never reached the server')
        assert len(server._calls) == 1
        shard.kill()
        shard.wait()
        _wait(lambda: not server._calls, "the owner kept the dead shard's call")
    finally:
        hold.unlink(missing_ok=True)
        if shard.poll() is None:
            shard.kill()
            shard.wait()
    assert _log(states['hope']).count('wait k') == 1, 'the call was replayed'


# ── execution host restart with shards alive ──────────────────────────────

_OWNER = """
import json, sys
import lib.mcp.client as client
from lib.mcp.client._bridge import MCPBridge
from lib.mcp import bridge_relay
bridge = MCPBridge(owner_user_id=1)
client._bridges[1] = bridge
for name, row in json.loads(sys.argv[2]).items():
    bridge.connect_server(name, row)
bridge_relay.configure_mcp_owner(process_role='execution', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
print('ready', flush=True)
sys.stdin.read()
"""


def _start_owner(project: Path, rows: dict, cache: Path) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, '-c', _OWNER, str(project), json.dumps(rows)], env=_environment(cache),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    assert process.stdout.readline().strip() == b'ready'
    return process


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def test_execution_host_restart_with_shards_alive(tmp_path, cache):
    state = tmp_path / 'hope'
    rows = {'hope': _row(state)}
    project = tmp_path / 'project'
    owner = _start_owner(project, rows, cache)
    shard = _shard(project)
    try:
        assert shard.call_tool('mcp__hope__chaos_record', {'note': 'before'}) == 'recorded before'
        before = shard.tool_catalog_generation()
        (state / 'hold-call').touch()
        outcome = {}

        def in_flight():
            try:
                outcome['result'] = shard.call_tool('mcp__hope__chaos_wait', {'note': 'lost'})
            except Exception as exc:
                outcome['error'] = exc

        caller = threading.Thread(target=in_flight)
        caller.start()
        _wait(lambda: 'wait lost' in _log(state), 'the in-flight call never reached the server')
        _kill_tree(owner)
        caller.join(DEADLINE_S)
        assert isinstance(outcome.get('error'), ConnectionError), outcome
        assert 'outcome is unknown' in str(outcome['error'])
        # While no owner answers, the shard offers no MCP tools rather than stale ones.
        assert not shard.connected and shard.get_openai_tool_defs() == []
        (state / 'hold-call').unlink()

        owner = _start_owner(project, rows, cache)
        assert shard.connected
        assert shard.tool_catalog_generation() != before
        assert shard.call_tool('mcp__hope__chaos_record', {'note': 'after'}) == 'recorded after'
    finally:
        (state / 'hold-call').unlink(missing_ok=True)
        _kill_tree(owner)
    calls = _log(state)
    assert calls.count('wait lost') == 1, 'the lost call was replayed'
    assert calls.count('record before') == 1 and calls.count('record after') == 1


# ── concurrent sessions across shards ─────────────────────────────────────

_SHARD_SESSIONS = """
import json, sys, threading
from lib.mcp import bridge_relay
from lib.mcp.client import get_bridge
bridge_relay.configure_mcp_owner(process_role='execution_shard', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
shard, sessions = sys.argv[2], int(sys.argv[3])
results, servers = {}, set()

def session(index):
    bridge = get_bridge(1)
    servers.update(row['server_name'] for row in bridge.get_tool_catalog_snapshot())
    for server in ('hope', 'github'):
        note = f'{shard}-{index}-{server}'
        results[note] = str(bridge.call_tool(f'mcp__{server}__chaos_record', {'note': note}))

threads = [threading.Thread(target=session, args=(index,)) for index in range(sessions)]
for thread in threads: thread.start()
for thread in threads: thread.join()
print(json.dumps({'results': results, 'servers': sorted(servers)}), flush=True)
"""


def test_twenty_sessions_across_shards_reach_one_server_set(owner, cache):
    _bridge, _server, project, states = owner
    shards = [subprocess.Popen(
        [sys.executable, '-c', _SHARD_SESSIONS, str(project), f's{index}', '5'],
        env=_environment(cache), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for index in range(4)]
    reports = []
    for process in shards:
        out, err = process.communicate(timeout=DEADLINE_S)
        assert process.returncode == 0, err.decode()[-2000:]
        reports.append(json.loads(out.decode().strip().splitlines()[-1]))
    notes = {}
    for report in reports:
        assert report['servers'] == ['github', 'hope'], 'a shard is missing a server'
        notes.update(report['results'])
    assert len(notes) == 40
    assert all(result == f'recorded {note}' for note, result in notes.items())
    for name, state in states.items():
        calls = _log(state)
        expected = sorted(f'record {note}' for note in notes if note.endswith(name))
        assert sorted(line for line in calls if line.startswith('record ')) == expected, \
            'a call was lost or ran twice'
        assert len(_log(state, 'launches.log')) == 1, 'a shard launched its own server'


# ── one npm-cache launcher at a time ───────────────────────────────────────

_TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('held')
else:
    print('free')
"""


def _lock_state(cache: Path) -> str:
    lock = cache / 'npm' / '.tofu-launch.lock'
    return subprocess.run([sys.executable, '-c', _TRY_LOCK, str(lock)], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.mark.parametrize('launcher, held', [('npx', True), ('python', False)])
def test_an_npm_cache_launcher_holds_the_cache_until_its_handshake(tmp_path, cache, launcher, held):
    from lib.mcp.client._bridge import MCPBridge
    command = sys.executable
    if launcher == 'npx':
        command = str(tmp_path / 'bin' / 'npx')
        Path(command).parent.mkdir()
        Path(command).write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        os.chmod(command, 0o755)
    state = tmp_path / 'server'
    row = _row(state, command)
    (state / 'hold-initialize').touch()
    bridge = MCPBridge(owner_user_id=1)
    connect = threading.Thread(target=bridge.connect_server, args=('srv', row))
    connect.start()
    try:
        _wait(lambda: _log(state, 'launches.log'), 'the server never launched')
        assert _lock_state(cache) == ('held' if held else 'free')
        (state / 'hold-initialize').unlink()
        connect.join(DEADLINE_S)
        assert bridge.is_mcp_tool('mcp__srv__chaos_record')
        assert _lock_state(cache) == 'free'
    finally:
        (state / 'hold-initialize').unlink(missing_ok=True)
        connect.join(DEADLINE_S)
        bridge.disconnect_all()
