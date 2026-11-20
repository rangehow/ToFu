"""Fault injection for node identity, pairing, trust and the node↔Home link.

Real processes only: an isolated Home (tests/chaos/instance.py: storage host,
execution host, separate API host), real node processes running the one node
program (tests/chaos/node.py → ``python -m lib.desktop_agent``, and the same suite again
with the Rust ``tofu node``), and a network
edge between them (tests/chaos/edge.py) that the node can cross only through
its HTTP proxy and a CodeLab-style ``/proxy/<port>`` prefix. Model Turns are
scripted (tests/chaos/fake_provider.py) and run node commands through the
execution host and the bridge relay to the API host the nodes poll.

Every fault asserts the node contracts of docs/DEFENSIVE_PATTERNS.md, "Fault
contracts of the chaos suite":

  N1  no durable state is lost: the Home's node registry (record, trust state,
      fingerprint) and the node's own identity and credential survive;
  N2  no side effect runs twice: a node command's effect is counted;
  N3  clear status: the Home lists the node's state and the node records why
      a link stopped; the Turn settles with an error instead of hanging;
  N4  no leaked processes: nothing a node or the Home started outlives it;
  N5  no blind retry loop: reconnects are paced, refusals end the link.

Run with ``make test-chaos``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
from threading import Barrier
import time
import urllib.error
import urllib.request
import uuid

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.client import InstanceClient, TERMINAL_TURN_STATUSES
from tests.chaos.edge import EDGE_HOST, Edge
from tests.chaos.instance import ChaosInstance
from tests.chaos.node import ChaosNode

pytestmark = [pytest.mark.api, pytest.mark.timeout(900)]

# Suite bounds, not product timers. The node's reconnect backoff tops out at
# 60 s (lib/desktop_agent/_runtime.retry_delay); the liveness window is 15 s.
SETTLE_DEADLINE_S = 180.0
RECONNECT_DEADLINE_S = 150.0
POLL = '/api/desktop/poll'


class Fleet:
    def __init__(self, root: Path, instance: ChaosInstance, provider: fp.ScriptedProvider, edge: Edge,
                 program: str = 'python'):
        self.root, self.instance, self.provider, self.edge = root, instance, provider, edge
        self.program = program
        self.client = InstanceClient(instance.base_url)
        self.nodes: list[ChaosNode] = []
        self.effects = root / 'effects.log'

    # Home API ------------------------------------------------------------------
    def api(self, method: str, path: str, body=None, *, host: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if host:
            headers['Host'] = host
        request = urllib.request.Request(self.instance.base_url + path, data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read() or b'{}')
                return response.status, payload.get('data', payload) if isinstance(payload, dict) else payload
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw.decode(errors='replace')

    def mint(self) -> dict:
        status, body = self.api('POST', f'/api/v4/desktop/pair-code?base={self.edge.home_url}/',
                                {}, host=EDGE_HOST)
        assert status == 201, body
        assert body['link'].startswith(self.edge.home_url + '/#tofu-pair=')
        return body

    def nodes_listing(self) -> dict:
        status, body = self.api('GET', '/api/v4/desktop/nodes')
        assert status == 200, body
        return {n['node_id']: n for n in body['nodes'] if n['node_id']}

    def confirm(self, node_id: str) -> dict:
        status, body = self.api('POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
        assert status == 200, body
        return body

    def wait_node(self, node_id: str, predicate, what: str, timeout: float = RECONNECT_DEADLINE_S) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.nodes_listing().get(node_id)
            except (OSError, AssertionError) as error:  # Home restarting
                last = error
            if isinstance(last, dict) and predicate(last):
                return last
            time.sleep(0.25)
        raise TimeoutError(f'node {node_id} never became {what}: {last}')

    # nodes ---------------------------------------------------------------------
    def node(self, name: str, **kwargs) -> ChaosNode:
        node = ChaosNode(self.root, name, tag=self.instance.tag, proxy=self.edge.proxy_url,
                         program=self.program, **kwargs)
        self.nodes.append(node)
        return node

    def pair(self, node: ChaosNode, *, confirm: bool = True) -> str:
        """The one-line headless join, the owner's confirmation, and the node online."""
        minted = self.mint()
        node.join(minted['link'])
        log = node.wait_log('fingerprint', SETTLE_DEADLINE_S)
        node_id = node.node_id
        pending = self.wait_node(node_id, lambda n: n['state'] == 'pending', 'pending')
        # The owner compares the fingerprint the node printed with the Home's.
        assert pending['fingerprint'] and pending['fingerprint'] in log
        assert pending['kind'] == 'server' and not pending['online']
        if confirm:
            self.confirm(node_id)
            # The owner's preset: new devices are read-only (decision 6).
            self.ceiling(node_id, exec=True)
            self.wait_node(node_id, lambda n: n['online'] and n['state'] == 'active', 'online')
        return node_id

    def ceiling(self, node_id: str, **tiers) -> dict:
        status, snapshot = self.api('GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = self.api('PUT', f'/api/v4/devices/{node_id}/ceiling',
                                {'expected_revision': snapshot['rev'], 'tiers': {'read': True, **tiers}})
        assert status == 200, body
        return body

    def defaults(self, **kinds) -> dict:
        if kinds:
            status, body = self.api('PUT', '/api/v4/devices/defaults', kinds)
        else:
            status, body = self.api('GET', '/api/v4/devices/defaults')
        assert status == 200, body
        return body['defaults']

    def unaddressed_turn(self, command):
        nonce = self.provider.script([])
        self.provider._scripts[nonce] = [
            fp.tool('desktop_run_command', {'command': command(nonce)}), fp.text('Done.')]
        conversation = f'chaosnode{uuid.uuid4().hex[:12]}'
        self.client.create_turn(conversation, f'Run it. [chaos turn={nonce}]', workspace=str(self.root),
                                first=True, config={'desktopEnabled': True})
        return conversation, nonce

    def where(self, nonce: str) -> list[str]:
        """The HOME of every node that ran the effect for ``nonce``."""
        if not self.effects.exists():
            return []
        return [line.split()[1] for line in self.effects.read_text().splitlines()
                if line.split()[:1] == [nonce]]

    # turns ---------------------------------------------------------------------
    def effect_count(self, nonce: str) -> int:
        return self.effects.read_text().split().count(nonce) if self.effects.exists() else 0

    def node_turn(self, node_id: str, command, *, final: str = 'Node work done.'):
        nonce = self.provider.script([])
        self.provider._scripts[nonce] = [
            fp.tool('desktop_run_command', {'command': command(nonce), 'device': node_id}), fp.text(final)]
        conversation = f'chaosnode{uuid.uuid4().hex[:12]}'
        user_text = f'Run it on the node. [chaos turn={nonce}]'
        response = self.client.create_turn(conversation, user_text, workspace=str(self.root),
                                           first=True, config={'desktopEnabled': True})
        return conversation, nonce, response

    def effect(self, nonce: str, then: str = '') -> str:
        return f'echo {nonce} >> {self.effects}{"; " + then if then else ""}; echo effect-recorded'

    def settled(self, conversation: str) -> dict:
        turns = self.client.wait_settled(conversation, timeout=SETTLE_DEADLINE_S)
        assistant = [t for t in turns if t.get('actor') == 'assistant'][-1]
        assert assistant['status'] in TERMINAL_TURN_STATUSES
        if assistant['status'] != 'completed':
            settlement = assistant.get('settlement') or {}
            assert settlement.get('resumeOptions'), settlement
            assert ((settlement.get('error') or {}).get('message') or '').strip(), settlement
        return assistant

    def tool_result(self, nonce: str) -> str:
        requests = self.provider.requests(nonce)
        return str(requests[1]['tool_results'][0].get('content')) if len(requests) > 1 else ''

    def run_on(self, node_id: str) -> None:
        """A healthy round trip: the node runs one command, exactly once."""
        conversation, nonce, _ = self.node_turn(node_id, self.effect)
        assistant = self.settled(conversation)
        assert assistant['status'] == 'completed', (self.provider.requests(nonce)[0]['tools'], json.dumps(assistant.get('settlement'))[:300])
        assert self.effect_count(nonce) == 1, self.tool_result(nonce)


@pytest.fixture(scope='module', params=['python', 'rust'])
def fleet(tmp_path_factory, request):
    """One isolated Home and its nodes, for each implementation of the node program."""
    if request.param == 'rust':
        from tests.support.tofu_node import node_binary
        node_binary()
    root = tmp_path_factory.mktemp(f'chaos-nodes-{request.param}')
    provider = fp.ScriptedProvider().start()
    # Desktop tools in the execution host reach the API host's bridge through
    # the relay record both roles find under their one project (production shape).
    instance = ChaosInstance(root / 'home', faultfs=False, shared_project=True)
    edge = None
    harness = None
    try:
        instance.start()
        edge = Edge(instance.port).start()
        harness = Fleet(root, instance, provider, edge, program=request.param)
        harness.client.install_route(provider.base_url)
        yield harness
    finally:
        try:
            for node in (harness.nodes if harness else []):
                node.kill()
            instance.stop()
        finally:
            provider.stop()
            if edge is not None:
                edge.stop()
            leaked, locks = instance.leaked_processes(), instance.held_locks()
            instance.kill_leaked()
    # N4 for the whole fleet: every node and Home owner joined what it started.
    assert not leaked, f'processes outlived the fleet: {leaked}'
    assert not locks, f'locks held after the fleet stopped: {locks}'


def _no_node_children(node: ChaosNode) -> None:
    deadline = time.monotonic() + 30
    while node.pids() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not node.pids(), f'node {node.name} left processes: {node.pids()}'


# ── pairing through the CodeLab-style edge ────────────────────────────────────

def test_headless_join_confirm_and_run_through_the_proxy_edge(fleet):
    node = fleet.node('alpha')
    node_id = fleet.pair(node)
    # The node reached the Home only through its proxy and the /proxy/<port> prefix.
    assert fleet.edge.count(POLL, node=node_id) > 0
    assert node.attachment()['url'] == fleet.edge.home_url
    fleet.run_on(node_id)
    # A pending node never received work: its first commands came after confirmation.
    record = fleet.nodes_listing()[node_id]
    assert record['state'] == 'active' and record['route'] == 'direct'


# ── Home faults ───────────────────────────────────────────────────────────────

def test_home_api_restart_mid_command_runs_it_once_and_reconnects(fleet):
    node = fleet.node('restart')
    node_id = fleet.pair(node)
    before = fleet.nodes_listing()[node_id]
    marker = fleet.root / 'restart-started'
    conversation, nonce, _ = fleet.node_turn(
        node_id, lambda n: f'touch {marker}; sleep 4; ' + fleet.effect(n))
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not marker.exists():
        assert time.monotonic() < deadline, 'the node never started the command'
        time.sleep(0.1)
    fleet.instance.restart_api()
    fleet.edge.retarget(fleet.instance.port)
    fleet.settled(conversation)
    # N2: the command ran to completion on the node, once; nothing replayed it.
    deadline = time.monotonic() + 30
    while fleet.effect_count(nonce) == 0 and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(2)
    assert fleet.effect_count(nonce) == 1
    # N1: same node, same trust, no re-pair.
    after = fleet.wait_node(node_id, lambda n: n['online'], 'online again')
    assert (after['state'], after['fingerprint'], after['paired_at']) == (
        'active', before['fingerprint'], before['paired_at'])
    assert node.process.poll() is None
    fleet.run_on(node_id)


def test_full_home_restart_keeps_registry_and_pending_pairing(fleet):
    node = fleet.node('durable')
    node_id = fleet.pair(node)
    pending_node = fleet.node('pending-across-restart')
    minted = fleet.mint()
    fleet.instance.stop()
    fleet.instance.start()
    fleet.edge.retarget(fleet.instance.port)
    fleet.client = InstanceClient(fleet.instance.base_url)
    # N1: a link minted before the restart still pairs (durable pairing) ...
    pending_node.join(minted['link'])
    pending_node.wait_log('fingerprint', SETTLE_DEADLINE_S)
    assert fleet.wait_node(pending_node.node_id, lambda n: n['state'] == 'pending', 'pending')
    # ... and the confirmed node reconnects without re-pairing.
    fleet.wait_node(node_id, lambda n: n['online'] and n['state'] == 'active', 'online again')
    fleet.run_on(node_id)
    # The pending node polled the whole time and never received work.
    fleet.api('POST', f'/api/v4/desktop/nodes/{pending_node.node_id}/reject')
    pending_node.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
    assert pending_node.attachment()['state'] == 'revoked'


def test_execution_host_killed_mid_command_never_replays_it(fleet):
    node = fleet.node('split')
    node_id = fleet.pair(node)
    marker = fleet.root / 'split-started'
    conversation, nonce, _ = fleet.node_turn(
        node_id, lambda n: f'touch {marker}; sleep 3; ' + fleet.effect(n))
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not marker.exists():
        assert time.monotonic() < deadline, 'the node never started the command'
        time.sleep(0.1)
    fleet.instance.kill_execution_host()
    fleet.instance.recover_execution_host()
    fleet.settled(conversation)
    time.sleep(5)
    assert fleet.effect_count(nonce) == 1
    # The API host kept the node online throughout; the new execution host sees it.
    assert fleet.nodes_listing()[node_id]['online']
    fleet.run_on(node_id)


# ── node faults ───────────────────────────────────────────────────────────────

def test_node_killed_mid_command_and_rejoined(fleet):
    node = fleet.node('killed')
    node_id = fleet.pair(node)
    marker = fleet.root / 'killed-started'
    conversation, nonce, _ = fleet.node_turn(
        node_id, lambda n: f'touch {marker}; sleep 600; ' + fleet.effect(n))
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not marker.exists():
        assert time.monotonic() < deadline, 'the node never started the command'
        time.sleep(0.1)
    node.kill(signal.SIGKILL)
    # N4: the node's command supervisor retires its process tree with it.
    _no_node_children(node)
    fleet.wait_node(node_id, lambda n: not n['online'], 'offline')
    # Re-join = restart the same node program; no pairing, same identity.
    node.serve()
    fleet.wait_node(node_id, lambda n: n['online'] and n['state'] == 'active', 'online again')
    # N3: the dead process's command settles as an unknown outcome, never
    # replayed: the liveness watcher's device_unavailable (15 s without a
    # poll), or, if the node is back sooner, the new boot's lost-claim settlement.
    fleet.settled(conversation)
    result = fleet.tool_result(nonce)
    assert 'unknown' in result and 'effect-recorded' not in result, result
    assert fleet.effect_count(nonce) == 0  # the killed command never completed, never replayed
    fleet.run_on(node_id)
    assert node.node_id == node_id


def test_network_partition_and_heal_reconnects_without_a_retry_storm(fleet):
    node = fleet.node('partition')
    node_id = fleet.pair(node)
    fleet.edge.partition()
    cut = time.monotonic()
    try:
        fleet.wait_node(node_id, lambda n: not n['online'], 'offline (partitioned)')
        # A command for an offline node is refused before enqueue, never queued blind.
        conversation, nonce, _ = fleet.node_turn(node_id, fleet.effect)
        # No device online: the Turn's toolset has no desktop tools, so the
        # call cannot run anywhere; the Turn settles with an error.
        assistant = fleet.settled(conversation)
        assert fleet.effect_count(nonce) == 0
        assert assistant['status'] != 'completed' or 'effect-recorded' not in fleet.tool_result(nonce)
        time.sleep(max(0.0, 45 - (time.monotonic() - cut)))
        # N5: paced, jittered backoff: a 45 s cut costs a handful of attempts.
        attempts = fleet.edge.count(POLL, since=cut, node=node_id)
        assert attempts <= 12, attempts
    finally:
        fleet.edge.heal()
    fleet.wait_node(node_id, lambda n: n['online'], 'online after heal')
    fleet.run_on(node_id)
    assert fleet.effect_count(nonce) == 0  # the refused command was never replayed later


# ── identity and trust faults ─────────────────────────────────────────────────

def test_duplicate_process_of_one_identity_is_refused_and_flagged(fleet):
    node = fleet.node('dup')
    node_id = fleet.pair(node)
    twin = fleet.node('dup-twin', config_path=node.config_path, hostname=node.hostname,
                      container=node.container)
    assert twin.host_key() == node.host_key()  # indistinguishable on disk
    twin.serve()
    # The earlier boot is refused with a clear reason; the newer one serves.
    node.wait_log('desktop_node_identity_conflict', RECONNECT_DEADLINE_S)
    flagged = fleet.wait_node(node_id, lambda n: (n.get('conflict') or {}).get('reason') == 'duplicate_boot',
                              'flagged')
    assert flagged['state'] == 'conflict' and flagged['online']
    # The refused process stopped its link (no retry loop) and left the shared config usable.
    since = time.monotonic()
    time.sleep(20)
    assert twin.attachment()['state'] == 'active'
    fleet.run_on(node_id)
    assert fleet.edge.count(POLL, since=since, node=node_id) >= 1  # the newer process keeps polling
    node.kill()
    twin.kill()
    _no_node_children(node)
    _no_node_children(twin)


def test_twins_on_one_home_directory_are_separate_revocable_nodes(fleet):
    first = fleet.node('twin-a', hostname='shared-host')
    first_id = fleet.pair(first)
    second = fleet.node('twin-b', config_path=first.config_path, hostname='shared-host')
    second_id = fleet.pair(second, confirm=False)
    assert second_id != first_id
    # Detected at join: the Home shows the newcomer next to a live twin.
    listed = fleet.nodes_listing()[second_id]
    assert listed['predecessor'] == {'node_id': first_id, 'name': listed['predecessor']['name'],
                                     'online': True}
    fleet.confirm(second_id)
    fleet.wait_node(second_id, lambda n: n['online'], 'online')
    assert first.attachment()['secret'] != second.attachment()['secret']
    # Revoking one twin leaves the other connected (own credential each).
    status, _ = fleet.api('DELETE', f'/api/v4/desktop/nodes/{second_id}')
    assert status == 200
    second.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
    assert second.attachment()['state'] == 'revoked'
    fleet.run_on(first_id)
    second.kill()
    first.kill()


def test_recreated_container_is_a_confirmed_re_pair_of_the_known_device(fleet):
    old = fleet.node('recreate', hostname='worker-0')
    old_id = fleet.pair(old)
    old.kill()
    fleet.wait_node(old_id, lambda n: not n['online'], 'offline')
    new = fleet.node('recreate-2', config_path=old.config_path, hostname='worker-0')
    new_id = fleet.pair(new, confirm=False)
    assert new_id != old_id
    assert fleet.nodes_listing()[new_id]['predecessor']['node_id'] == old_id
    fleet.confirm(new_id)
    listing = fleet.wait_node(new_id, lambda n: n['online'], 'online')
    assert listing['predecessor'] is None
    assert old_id not in fleet.nodes_listing()  # one device, not a silent duplicate
    fleet.run_on(new_id)
    new.kill()


def test_revoked_device_reconnect_attempts_are_refused_and_stop(fleet):
    node = fleet.node('revoked')
    node_id = fleet.pair(node)
    status, _ = fleet.api('DELETE', f'/api/v4/desktop/nodes/{node_id}')
    assert status == 200
    node.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
    assert node.attachment()['state'] == 'revoked'
    # A copied old config tries the revoked credential again.
    cfg = node.config()
    for rows in [cfg.get('attachments') or []] + list((cfg.get('host_attachments') or {}).values()):
        for row in rows:
            row['state'] = 'active'
    node.config_path.write_text(json.dumps(cfg))
    node.kill()
    since = time.monotonic()
    node.serve()
    node.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
    time.sleep(10)
    # N5: one refused poll ends the link; no loop. N1: nothing resurrected on the Home.
    assert fleet.edge.count(POLL, since=since, node=node_id) == 1
    assert node_id not in fleet.nodes_listing()
    assert node.attachment()['state'] == 'revoked'
    node.kill()
    _no_node_children(node)



# ── routing/permissions hooks (lib/desktop/device_policy.py, lib/desktop/routing.py) ──

def _effect_here(fleet):
    return lambda n: f'echo {n} $HOME >> {fleet.effects}; echo effect-recorded'


def _assert_selected(fleet, expected_home, *, device=None, refusal=None):
    if device is None:
        conversation, nonce = fleet.unaddressed_turn(_effect_here(fleet))
    else:
        conversation, nonce, _ = fleet.node_turn(device, _effect_here(fleet))
    assert fleet.settled(conversation)['status'] == 'completed'
    result = fleet.tool_result(nonce)
    assert fleet.where(nonce) == ([] if expected_home is None else [str(expected_home)]), result
    assert (refusal or 'effect-recorded') in result, result


def _race_ceiling_and_default(fleet, node_id):
    path = f'/api/v4/devices/{node_id}/ceiling'
    rev = fleet.ceiling(node_id, exec=True)['rev']
    edit = {'expected_revision': rev, 'tiers': {'read': True, 'exec': True, 'write': True}}
    gate = Barrier(2, timeout=10)

    def put_ceiling():
        gate.wait()
        return fleet.api('PUT', path, edit)

    with ThreadPoolExecutor(max_workers=1) as pool:
        competing = pool.submit(put_ceiling)
        gate.wait()
        status, defaults = fleet.api('PUT', '/api/v4/devices/defaults', {'computer': node_id})
        assert status == 200, defaults
        status, body = competing.result()
    assert status == 200 or (status == 409 and 'stale_device_policy' in str(body)), body
    # If the ceiling won, its successful ACK still cannot be replayed after
    # the defaults commit; if it lost, it must not modify the winner's policy.
    stale_status, stale = fleet.api('PUT', path, edit)
    assert stale_status == 409 and 'stale_device_policy' in str(stale), stale
    assert fleet.defaults()['computer'] == node_id
    code, permissions = fleet.api('GET', '/api/v4/devices/permissions')
    assert code == 200, permissions
    assert defaults['rev'] == rev + (2 if status == 200 else 1)
    assert permissions['rev'] == defaults['rev']
    assert permissions['permissions'][node_id]['home']['write'] is (status == 200)

    # Deterministic second interleaving: one settings client reads a revision,
    # another commits a changed default, then the first submits its pending edit.
    pending = {'expected_revision': permissions['rev'], 'tiers': {'read': True}}
    code, switched = fleet.api('PUT', '/api/v4/devices/defaults', {'computer': None})
    assert code == 200 and switched['rev'] == permissions['rev'] + 1, switched
    code, stale = fleet.api('PUT', path, pending)
    assert code == 409 and 'stale_device_policy' in str(stale), stale
    code, after = fleet.api('GET', '/api/v4/devices/permissions')
    assert code == 200 and after['rev'] == switched['rev'], after
    assert after['permissions'][node_id]['home'] == permissions['permissions'][node_id]['home']
    assert fleet.defaults()['computer'] is None
    fleet.defaults(computer=node_id)


def test_selected_device_lifecycle_fences_old_settings(fleet):
    target = fleet.node('journey-target')
    spare = fleet.node('journey-spare')
    try:
        target_id = fleet.pair(target)
        spare_id = fleet.pair(spare)
        home = target.root / 'home'
        pids = {fleet.instance.api.pid, fleet.instance.execution_record()['pid'],
                target.process.pid, spare.process.pid}
        assert len(pids) == 4
        _race_ceiling_and_default(fleet, target_id)
        _assert_selected(fleet, spare.root / 'home', device=spare_id)
        _assert_selected(fleet, home, device=target_id)
        target.kill()
        fleet.wait_node(target_id, lambda n: not n['online'], 'offline')
        _assert_selected(fleet, None, refusal='device_offline')
        _assert_selected(fleet, None, device=target_id, refusal='device_offline')
        target.serve()
        fleet.wait_node(target_id, lambda n: n['online'], 'reconnected')
        _assert_selected(fleet, home)

        # Toggle a tier so the write advances the revision regardless of who
        # won the race. First prove this exact request really is replayable.
        status, snapshot = fleet.api('GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        ceiling = f'/api/v4/devices/{target_id}/ceiling'
        write = not snapshot['permissions'][target_id]['home']['write']
        old = {'expected_revision': snapshot['rev'],
               'tiers': {'read': True, 'exec': True, 'write': write}}
        status, saved = fleet.api('PUT', ceiling, old)
        assert status == 200 and saved['rev'] == snapshot['rev'] + 1, saved
        assert saved['home']['write'] is write
        status, replay = fleet.api('PUT', ceiling, old)
        assert status == 200 and replay['rev'] == saved['rev'], replay
        assert replay['home'] == saved['home']
        old_credential = fleet.nodes_listing()[target_id]['credential']['id']
        status, removed = fleet.api('DELETE', f'/api/v4/desktop/nodes/{target_id}')
        assert status == 200, removed
        target.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
        fleet.wait_node(spare_id, lambda n: n['online'], 'spare still online')
        assert target_id not in fleet.nodes_listing()
        assert fleet.defaults()['computer'] is None
        _assert_selected(fleet, None, device=target_id, refusal='device_not_found')

        target.kill()
        assert fleet.pair(target, confirm=False) == target_id
        fleet.confirm(target_id)
        active = fleet.wait_node(target_id, lambda n: n['online'], 'paired again')
        assert active['credential']['id'] != old_credential
        assert active['tiers']['exec'] is False
        status, stale = fleet.api('PUT', ceiling, old)
        assert status == 409 and 'stale_device_policy' in str(stale), stale
        assert fleet.defaults()['computer'] is None
        _assert_selected(fleet, None, device=target_id, refusal='permission_denied')
        fleet.ceiling(target_id, exec=True)
        _assert_selected(fleet, home, device=target_id)
    finally:
        target.kill()
        spare.kill()


def test_revoked_default_device_leaves_placement_across_a_home_restart(fleet):
    default = fleet.node('default-dev')
    default_id = fleet.pair(default)
    other = fleet.node('other-dev')
    other_id = fleet.pair(other)
    fleet.defaults(computer=default_id)
    # Unaddressed: placed on the marked default, never on another online device.
    conversation, nonce = fleet.unaddressed_turn(_effect_here(fleet))
    fleet.settled(conversation)
    assert fleet.where(nonce) == [str(default.root / 'home')]
    # Revoking its credential drops it from placement and policy, durably.
    credential = fleet.nodes_listing()[default_id]['credential']['id']
    status, _ = fleet.api('DELETE', f'/api/v4/desktop/token/{credential}')
    assert status == 200
    default.wait_log('Server returned 401', RECONNECT_DEADLINE_S)
    fleet.instance.restart_api()
    fleet.edge.retarget(fleet.instance.port)
    assert fleet.defaults()['computer'] is None
    status, permissions = fleet.api('GET', '/api/v4/devices/permissions')
    assert status == 200 and default_id not in permissions['permissions']
    # With no default and several devices online the call is refused, not sent anywhere.
    fleet.wait_node(other_id, lambda n: n['online'], 'online after the restart')
    conversation, nonce = fleet.unaddressed_turn(_effect_here(fleet))
    fleet.settled(conversation)
    assert fleet.where(nonce) == []
    assert 'no default computer is marked' in fleet.tool_result(nonce), fleet.tool_result(nonce)
    default.kill()
    other.kill()


def test_an_offline_node_is_found_by_the_routing_boundary_and_keeps_its_ceiling(fleet):
    node = fleet.node('offline-ceiling')
    node_id = fleet.pair(node, confirm=False)
    fleet.confirm(node_id)
    node.kill()
    fleet.wait_node(node_id, lambda n: not n['online'], 'offline')
    # The owner grants exec while the device is offline: identity comes from the registry.
    fleet.ceiling(node_id, exec=True)
    fleet.instance.restart_api()
    fleet.edge.retarget(fleet.instance.port)
    node.serve()
    back = fleet.wait_node(node_id, lambda n: n['online'] and n['tiers']['exec'], 'online with exec')
    assert back['tiers']['exec'], back  # the ceiling granted while offline survived
    fleet.run_on(node_id)
    node.kill()


def test_a_re_paired_recreated_container_keeps_permissions_and_default(fleet):
    old = fleet.node('keeps-policy', hostname='worker-7')
    old_id = fleet.pair(old)
    fleet.ceiling(old_id, exec=True, write=True)
    fleet.defaults(computer=old_id)
    old.kill()
    fleet.wait_node(old_id, lambda n: not n['online'], 'offline')
    # The container is recreated while the Home itself restarts.
    fleet.instance.stop()
    fleet.instance.start()
    fleet.edge.retarget(fleet.instance.port)
    fleet.client = InstanceClient(fleet.instance.base_url)
    new = fleet.node('keeps-policy-2', config_path=old.config_path, hostname='worker-7')
    new_id = fleet.pair(new, confirm=False)
    assert fleet.nodes_listing()[new_id]['predecessor']['node_id'] == old_id
    fleet.confirm(new_id)
    # The same device keeps its Home ceiling and its default mark under the new ID.
    assert fleet.defaults()['computer'] == new_id
    status, permissions = fleet.api('GET', '/api/v4/devices/permissions')
    assert status == 200 and old_id not in permissions['permissions']
    home = permissions['permissions'][new_id]['home']
    assert home['exec'] and home['write']
    fleet.wait_node(new_id, lambda n: n['online'] and n['tiers']['exec'], 'online with exec')
    conversation, nonce = fleet.unaddressed_turn(_effect_here(fleet))
    fleet.settled(conversation)
    assert fleet.where(nonce) == [str(new.root / 'home')]
    new.kill()
