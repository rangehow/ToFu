"""Fault injection through real processes: a node lost mid-tool (docs/modules/remote_execution.md,
"Device routing and permissions").

A real Hypercorn Home and a real node subprocess, run once with each implementation of
the one node program (``python -m lib.desktop_agent`` and the Rust ``tofu node``):

1. Home enforcement over the wire: a new node is read-only on the Home even
   though it allows writes itself; raising the Home ceiling lets the write run;
   a command smuggled past the Home boundary with a read-only ``home_tiers`` is
   refused by the node itself.
2. Node loss mid-tool: the node is SIGKILLed (its whole process group) while a
   side-effecting command runs. The waiting call settles with
   ``device_unavailable`` within the liveness window (no hang), nothing is
   retried on the other online node, and no process of the group survives.
3. The node comes back with the same identity and the turn repeats the call
   with the same idempotency key: the node answers from its durable journal and
   the side effect is not duplicated.

Run: pytest tests/desktop/test_device_routing_faults_slow.py -m slow
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OWNER = '1'


@pytest.fixture(params=['python', 'rust'])
def node_program(request):
    """The one node program, in either implementation: the Python node or ``tofu node``."""
    if request.param == 'python':
        return [sys.executable, '-m', 'lib.desktop_agent']
    from tests.support.tofu_node import node_command
    return node_command()


def _start_node(program, live_server, token, config, log_path, *flags, name):
    env = dict(os.environ)
    env['TOFU_DESKTOP_CONFIG'] = str(config)
    env['PYTHONPATH'] = _REPO + os.pathsep + env.get('PYTHONPATH', '')
    with open(config, 'a', encoding='utf-8'):
        pass
    if not config.read_text():
        config.write_text(json.dumps({'name': name}))
    log = open(log_path, 'a', encoding='utf-8')  # noqa: SIM115
    proc = subprocess.Popen(
        [*program, '--server', live_server,
         '--bridge-token', token, '--poll-interval', '0.2', *flags],
        cwd=_REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, log


def _wait_online(agent_id=None, *, exclude=(), deadline_s=30.0):
    from lib.desktop import bridge as db
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        for agent in db.list_agents(user_id=OWNER):
            if agent['online'] and agent['agent_id'] not in exclude and (
                    agent_id is None or agent['agent_id'] == agent_id):
                return agent
        time.sleep(0.1)
    return None


def _group_alive(pgid) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop(proc, log):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)
    log.close()


def test_node_loss_mid_tool_settles_unavailable_and_never_duplicates(live_server, tmp_path, node_program):
    from lib.api_keys import create_key
    from lib.desktop import bridge as db
    from lib.desktop import device_policy
    from lib.tasks_pkg.handlers.misc import _agents
    _row, token = create_key(owner_user_id=int(OWNER), name='routing-fault-node',
                             scopes=['agents:bridge'])
    effect = tmp_path / 'effect.txt'
    victim_cfg, spare_cfg = tmp_path / 'victim.json', tmp_path / 'spare.json'
    victim, victim_log = _start_node(node_program, live_server, token, victim_cfg, tmp_path / 'victim.log',
                                     '--allow-write', '--allow-exec', name='victim')
    spare = spare_log = None
    try:
        node = _wait_online()
        assert node is not None, (tmp_path / 'victim.log').read_text()[-2000:]
        node_id = node['agent_id']

        # 1) The Home boundary: new device read-only although the node allows writes.
        target = tmp_path / 'written.txt'
        text = _agents._run_desktop('desktop_write_file',
                                    {'path': str(target), 'content': 'x', 'device': node_id},
                                    user_id=OWNER)
        assert 'permission_denied' in text and not target.exists()
        device_policy.set_ceiling(int(OWNER), node_id, {'write': True, 'exec': True}, expected_revision=0)
        text = _agents._run_desktop('desktop_write_file',
                                    {'path': str(target), 'content': 'x', 'device': node_id},
                                    user_id=OWNER)
        assert target.read_text() == 'x', text
        # The node re-checks: a command past the Home boundary with a read-only
        # ceiling is refused by the device.
        cmd_id = 'smuggled-write'
        with db.command_queue_lock:
            db.command_queue[cmd_id] = {
                'id': cmd_id, 'type': 'desktop_write_file',
                'params': {'path': str(tmp_path / 'smuggled.txt'), 'content': 'x'},
                'created_at': time.time(), 'event': threading.Event(), 'result': None,
                'error': None, 'target_agent_id': node_id, 'user_id': OWNER}
        real_home = device_policy.home_tiers
        device_policy.home_tiers = lambda owner, nid: {'read': True, 'write': False, 'exec': False,
                                                       'gui': False, 'egress': False,
                                                       'codex_login': False}
        try:
            assert db.command_queue[cmd_id]['event'].wait(10)
        finally:
            device_policy.home_tiers = real_home
        assert 'permission_denied' in (db.command_queue[cmd_id]['error'] or '')
        assert not (tmp_path / 'smuggled.txt').exists()
        db.command_queue.pop(cmd_id, None)

        # A second node stays online throughout: nothing may be retried on it.
        # Its own credential: the Home binds one credential to one node ID
        # (contracts/desktop_node_registry_v1.schema.json x-identity.binding).
        _row, spare_token = create_key(owner_user_id=int(OWNER), name='routing-fault-spare',
                                       scopes=['agents:bridge'])
        spare, spare_log = _start_node(node_program, live_server, spare_token, spare_cfg, tmp_path / 'spare.log',
                                       '--allow-write', '--allow-exec', name='spare')
        assert _wait_online(exclude=(node_id,)) is not None
        device_policy.set_defaults(int(OWNER), {'computer': node_id})

        # 2) Kill the node mid-command.
        task = {'_userId': OWNER, 'convId': 'conv-fault', 'id': 'task-fault'}
        key = 'call-fault-1'
        command = f'echo once >> {effect}; sleep 120'
        outcome = {}

        def call():
            outcome['text'] = _agents._run_desktop('desktop_run_command', {'command': command},
                                                   user_id=OWNER, task=task, call_key=key)
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        deadline = time.time() + 20
        while time.time() < deadline and not (effect.exists() and effect.read_text()):
            time.sleep(0.1)
        assert effect.read_text() == 'once\n'
        os.killpg(victim.pid, signal.SIGKILL)
        killed_at = time.monotonic()
        worker.join(timeout=db._CONNECTED_WINDOW_S + 10)
        assert not worker.is_alive(), 'the tool call hung after its node was lost'
        assert time.monotonic() - killed_at <= db._CONNECTED_WINDOW_S + 10
        assert 'device_unavailable' in outcome['text'] and 'not retried' in outcome['text']
        victim.wait(timeout=10)
        deadline = time.time() + 10
        while _group_alive(victim.pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not _group_alive(victim.pid), 'a process of the killed node survived'
        assert effect.read_text() == 'once\n'  # not re-run on the spare node
        assert db.pending_commands_count() == 0

        # 3) The node returns with the same identity; the resumed turn repeats the call.
        victim_log.close()
        victim, victim_log = _start_node(node_program, live_server, token, victim_cfg, tmp_path / 'victim.log',
                                         '--allow-write', '--allow-exec', name='victim')
        assert _wait_online(node_id) is not None
        text = _agents._run_desktop('desktop_run_command', {'command': command},
                                    user_id=OWNER, task=task, call_key=key)
        assert 'device_unavailable' in text and 'not run again' in text
        assert effect.read_text() == 'once\n'
    finally:
        _stop(victim, victim_log)
        if spare is not None:
            _stop(spare, spare_log)
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()
