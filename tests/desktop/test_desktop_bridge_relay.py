"""The resident execution host reaches the desktop bridge owned by the API host.

Regression: with the managed API/execution split, desktop nodes long-poll the
API process (``routes/desktop.py``), so the process-local registry and command
queue in ``lib.desktop.bridge`` live there, while desktop tools, the
environment device listing and LLM egress run in the execution process, whose
registry was always empty. Settings showed a node online while the model was
told none of the owner's devices was online, and commands were refused or
queued where no node ever polls.
"""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytestmark = pytest.mark.unit

OWNER = '7'
AGENT = 'node-relay-1'

_OWNER_SCRIPT = textwrap.dedent('''
    import os, sys, threading, time
    from lib.desktop import bridge, bridge_relay
    path = sys.argv[1]
    server = bridge_relay.RelayServer(bridge_relay.record_path(path))
    def node():
        held = set()
        while True:
            bridge.register_agent(%(agent)r, {
                'name': 'relay-box', 'platform': 'linux',
                'capabilities': {'exec': True},
                'active_command_ids': sorted(held)}, user_id=%(owner)r, key_id='key-1')
            for cmd in bridge.take_pending_commands(agent_id=%(agent)r, user_id=%(owner)r):
                if cmd['type'] == 'desktop_list_files' and cmd['params'].get('hold'):
                    held.add(cmd['id'])
                    continue
                if cmd['type'] == 'desktop_run_command':
                    bridge.resolve_streams([
                        {'cmd_id': cmd['id'], 'seq': 1, 'stream': 'stdout', 'data': 'hi'},
                        {'cmd_id': cmd['id'], 'seq': 2, 'stream': 'stdout', 'data': '!',
                         'done': True}], agent_id=%(agent)r, user_id=%(owner)r)
                bridge.resolve_results([{'id': cmd['id'], 'result': {
                    'echo': cmd['params'], 'pid': os.getpid()}}],
                    agent_id=%(agent)r, user_id=%(owner)r)
            time.sleep(0.05)
    threading.Thread(target=node, daemon=True).start()
    time.sleep(0.2)
    server.start()
    print('ready', flush=True)
    sys.stdin.read()
    server.close(2.0)
''') % {'agent': AGENT, 'owner': OWNER}


@pytest.fixture
def isolated_bridge(monkeypatch):
    from lib.desktop import bridge
    monkeypatch.setattr(bridge, '_last_poll', [0.0])
    with bridge.command_queue_lock:
        bridge.command_queue.clear()
        bridge._agents.clear()
        bridge._streams.clear()
    yield bridge
    from lib.desktop.bridge_relay import stop_bridge_owner
    stop_bridge_owner()
    with bridge.command_queue_lock:
        bridge.command_queue.clear()
        bridge._agents.clear()
        bridge._streams.clear()


@pytest.fixture
def owner_process(tmp_path):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.getcwd() + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.Popen([sys.executable, '-c', _OWNER_SCRIPT, str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == 'ready'
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert not (tmp_path / 'data' / 'desktop-bridge-relay.json').exists()


def _select_relay(tmp_path):
    from lib.desktop import bridge_relay
    assert bridge_relay.configure_bridge_owner(
        process_role='execution', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'relay'


def test_execution_role_sees_and_drives_node_owned_by_another_process(
        tmp_path, owner_process, isolated_bridge):
    from lib.desktop import (
        bridge, is_desktop_agent_connected, list_agents, online_agents, send_desktop_command)

    # This process never saw a poll: locally the node is absent.
    assert bridge.resolve_device_target(AGENT, OWNER)[1] is not None
    assert is_desktop_agent_connected() is False
    record_file = tmp_path / 'data' / 'desktop-bridge-relay.json'
    record = json.loads(record_file.read_text())
    assert record['host'] == '127.0.0.1' and record['pid'] == owner_process.pid
    assert oct(os.stat(record_file).st_mode & 0o777) == '0o600'

    _select_relay(tmp_path)
    assert is_desktop_agent_connected() is True
    assert [a['agent_id'] for a in list_agents(user_id=OWNER) if a['online']] == [AGENT]
    assert list_agents(user_id='8') == []  # owner-scoped on the owner side
    assert [a['agent_id'] for a in online_agents()] == [AGENT]
    agent, error = bridge.resolve_device_target('relay-box', OWNER)
    assert error is None and agent['agent_id'] == AGENT
    assert bridge.online_connection(AGENT, OWNER)['connection_epoch'] == 1
    assert bridge.online_connection(AGENT, '8') is None

    result, error = send_desktop_command(
        'desktop_list_files', {'path': '~'}, timeout=5, target_agent_id=AGENT, user_id=OWNER)
    assert error is None
    assert result == {'echo': {'path': '~'}, 'pid': owner_process.pid}

    # The desktop tool handler the task loop runs, addressed by device name.
    from lib.tasks_pkg.handlers.misc._agents import _run_desktop
    text = _run_desktop('desktop_list_files', {'path': '~', 'device': 'relay-box'},
                        user_id=OWNER)
    assert f'pid: {owner_process.pid}' in text

    # Tier admission stays on the owner side, against the node's own frame.
    result, error = send_desktop_command(
        'desktop_gui_action', {'action': 'click', 'x': 1, 'y': 1}, timeout=5,
        target_agent_id=AGENT, user_id=OWNER)
    assert result is None and 'gui permission' in error


def test_relayed_streams_and_command_liveness(
        tmp_path, owner_process, isolated_bridge, monkeypatch):
    from lib.desktop import (
        bridge, consume_desktop_command, enqueue_desktop_command, get_command_stream,
        release_desktop_command)
    from lib.tasks_pkg.explained_wait import device_wait, explained_wait

    _select_relay(tmp_path)
    limits = {'bytes': 1024, 'frames': 16, 'frame_bytes': 256}
    cmd_id, error = enqueue_desktop_command(
        'desktop_run_command', {'command': 'echo hi'}, target_agent_id=AGENT,
        user_id=OWNER, stream_limits=limits)
    assert error is None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stream = get_command_stream(cmd_id)
        if stream and stream['done']:
            break
        time.sleep(0.02)
    assert stream == {'stdout': 'hi!', 'stderr': '', 'done': True, 'last_seq': 2}
    drained = consume_desktop_command(cmd_id, target_agent_id=AGENT, user_id=OWNER)
    assert [frame[0] for frame in drained['frames']] == [1, 2]
    assert drained['stream_done'] is True
    assert release_desktop_command(cmd_id, target_agent_id=AGENT, user_id=OWNER) is True

    # A task blocked on a relayed command claimed by a live node names the
    # device as the owner of its silence (the liveness judge asks the owner).
    task = {'id': 't1', '_userId': OWNER}
    outcome = {}
    cancel = threading.Event()
    held = threading.Thread(target=lambda: outcome.update(value=bridge.send_desktop_command(
        'desktop_list_files', {'path': '~', 'hold': True}, timeout=30,
        target_agent_id=AGENT, user_id=OWNER, cancellation=cancel)))

    from contextlib import contextmanager

    @contextmanager
    def scope(cmd):
        with device_wait(task, cmd):
            yield

    monkeypatch.setattr(bridge, '_device_wait_scope', scope)
    held.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and explained_wait(task) != 'device':
        time.sleep(0.05)
    assert explained_wait(task) == 'device'
    cancel.set()
    held.join(5)
    assert outcome['value'] == (
        None, 'Desktop command cancelled; remote completion is unconfirmed')
    assert explained_wait(task) == ''


def test_missing_owner_is_a_typed_failure_not_an_empty_fleet(tmp_path, isolated_bridge):
    from lib.desktop import bridge, bridge_relay, send_desktop_command

    _select_relay(tmp_path)
    result, error = send_desktop_command(
        'desktop_list_files', {}, timeout=1, target_agent_id=AGENT, user_id=OWNER)
    assert result is None and 'API process' in error
    with pytest.raises(bridge_relay.RelayUnavailable):
        bridge.list_agents(user_id=OWNER)
    agent, error = bridge.resolve_device_target(AGENT, OWNER)
    assert agent is None and 'API process' in error
    # Infrastructure predicates read an unreachable owner as no reachable device.
    assert bridge.online_agents() == []
    assert bridge.is_desktop_agent_connected() is False
    assert bridge.command_active('cmd', time.time()) is False


def test_relay_rejects_wrong_token(tmp_path, isolated_bridge):
    from lib.desktop import bridge_relay

    server = bridge_relay.RelayServer(bridge_relay.record_path(tmp_path / 'other'))
    server.start()
    try:
        record = json.loads(bridge_relay.record_path(tmp_path / 'other').read_text())
        record['token'] = 'forged'
        forged = tmp_path / 'forged' / 'data' / bridge_relay.RECORD_NAME
        forged.parent.mkdir(parents=True)
        forged.write_text(json.dumps(record))
        with pytest.raises(bridge_relay.RelayUnavailable, match='relay_refused'):
            bridge_relay.RelayClient(forged).call('online_agents', {})
    finally:
        assert server.close(2.0)


def test_role_selection_keeps_all_in_one_local(tmp_path, isolated_bridge):
    from lib.desktop import bridge_relay
    assert bridge_relay.configure_bridge_owner(
        process_role='all', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'local'
    assert bridge_relay.selected_relay() is None
    assert bridge_relay.configure_bridge_owner(
        process_role='api', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'owner'
    assert bridge_relay.record_path(tmp_path).exists()
    assert bridge_relay.stop_bridge_owner()
    assert not bridge_relay.record_path(tmp_path).exists()
    assert bridge_relay.configure_bridge_owner(
        process_role='execution_shard', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'relay'
    with pytest.raises(RuntimeError):
        bridge_relay.configure_bridge_owner(process_role='execution', environ={})

