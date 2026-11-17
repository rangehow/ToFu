"""Execution shards: claim placement, demand spawning, control fan-out and one generation.

Contract: serverctl_pkg/execution_shards.py (decision record in its module
header and docs/HARNESS_PERFORMANCE.md).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


# ── shard claim gate ────────────────────────────────────────────────────────

def _reports(fd):
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 1024)
    except BlockingIOError:
        return b''


def _gate(idle_seconds=0, clock=None):
    from serverctl_pkg.execution_shards import ShardGate
    read, write = os.pipe()
    retired = []
    gate = ShardGate(write, idle_seconds=idle_seconds, retire=lambda: retired.append(True),
                     **({'clock': clock} if clock else {}))
    wakes = []
    gate.bind(lambda: wakes.append(True))
    return gate, read, write, retired, wakes


def test_shard_claims_nothing_before_its_control_endpoint_is_published():
    gate, read, write, _retired, wakes = _gate()
    try:
        assert gate.admit(running=0, inflight=0) == (False, None)
        assert _reports(read) == b''
        gate.open()
        assert wakes
        assert gate.admit(running=0, inflight=0) == (True, None)
        assert _reports(read) == b'i'
    finally:
        os.close(read)
        os.close(write)


def test_shard_runs_one_job_at_a_time_and_reports_each_transition_once():
    gate, read, write, _retired, _wakes = _gate()
    try:
        gate.open()
        assert gate.admit(running=0, inflight=0)[0]
        assert gate.admit(running=0, inflight=1)[0] is True  # its own probe in flight
        assert gate.admit(running=1, inflight=1)[0] is False
        assert gate.admit(running=1, inflight=1)[0] is False
        assert gate.admit(running=0, inflight=0)[0] is True
        assert _reports(read) == b'ibi'
    finally:
        os.close(read)
        os.close(write)


def test_idle_shard_retires_itself_after_the_executor_idle_retention():
    now = [100.0]
    gate, read, write, retired, _wakes = _gate(idle_seconds=600, clock=lambda: now[0])
    try:
        gate.open()
        admitted, wait = gate.admit(running=0, inflight=0)
        assert admitted and wait == pytest.approx(600)
        now[0] += 599
        assert gate.admit(running=0, inflight=0) == (True, pytest.approx(1))
        now[0] += 1
        assert gate.admit(running=0, inflight=0) == (False, None)
        assert retired == [True]
        # Retirement is decided on the coordinator thread: no later claim.
        assert gate.admit(running=0, inflight=0) == (False, None)
        assert _reports(read) == b'ir'
    finally:
        os.close(read)
        os.close(write)


def test_work_restarts_the_idle_retention_and_zero_disables_it():
    now = [0.0]
    gate, read, write, retired, _wakes = _gate(idle_seconds=10, clock=lambda: now[0])
    try:
        gate.open()
        gate.admit(running=0, inflight=0)
        now[0] = 9
        gate.admit(running=1, inflight=1)
        now[0] = 15
        assert gate.admit(running=0, inflight=0)[0] is True
        assert not retired
    finally:
        os.close(read)
        os.close(write)
    gate, read, write, retired, _wakes = _gate(idle_seconds=0, clock=lambda: 1e9)
    try:
        gate.open()
        assert gate.admit(running=0, inflight=0) == (True, None)
        assert not retired
    finally:
        os.close(read)
        os.close(write)


def test_closing_shard_stops_claiming_and_tells_the_primary():
    gate, read, write, _retired, _wakes = _gate()
    try:
        gate.open()
        gate.admit(running=0, inflight=0)
        gate.close()
        assert gate.admit(running=0, inflight=0) == (False, None)
        assert _reports(read) == b'ir'
    finally:
        os.close(read)
        os.close(write)


def test_lost_primary_never_breaks_the_shard_gate():
    gate, read, write, _retired, _wakes = _gate()
    os.close(read)
    try:
        gate.open()
        assert gate.admit(running=0, inflight=0) == (True, None)
        assert gate.admit(running=1, inflight=1) == (False, None)
    finally:
        os.close(write)


# ── primary pool: placement and demand ──────────────────────────────────────

class _Source:
    def snapshot(self):
        return {'format': 'x', 'roots': [], 'packages': [], 'pins': {}}


@pytest.fixture
def pool(tmp_path, monkeypatch):
    from serverctl_pkg import execution_shards
    launched = []
    monkeypatch.setattr(execution_shards.ShardPool, '_launch', lambda self, shard: launched.append(shard))
    value = execution_shards.ShardPool(source=_Source(), plan={'python': sys.executable}, checkout=REPO,
                                       python=sys.executable, launch_environment={},
                                       directory=tmp_path / 'execution-shards')
    value.launched = launched
    yield value
    for shard in launched:
        for thread in shard.threads:
            thread.join()


def test_primary_alone_spawns_nothing_and_always_claims(pool):
    for running in (0, 1, 0, 1):
        assert pool.admit(running=running, inflight=running) == (True, None)
    assert pool.launched == []


def test_each_overflow_claim_starts_one_shard(pool):
    pool.admit(running=1, inflight=1)
    pool.admit(running=2, inflight=2)
    pool.admit(running=5, inflight=5)
    assert len(pool.launched) == 4
    # Jobs finishing on the primary never start more.
    pool.admit(running=3, inflight=3)
    pool.admit(running=4, inflight=4)
    assert len(pool.launched) == 5


def test_busy_primary_defers_to_an_idle_shard_but_never_to_a_starting_one(pool):
    pool.admit(running=1, inflight=1)
    pool.admit(running=2, inflight=2)
    (shard,) = pool.launched
    assert shard.state == 'starting'
    assert pool.admit(running=2, inflight=2)[0] is True
    shard.state = 'idle'
    assert pool.admit(running=2, inflight=2)[0] is False
    # An idle shard never keeps the primary from its first job.
    assert pool.admit(running=0, inflight=0)[0] is True
    shard.state = 'busy'
    assert pool.admit(running=2, inflight=2)[0] is True


def test_refused_or_stopping_pool_spawns_nothing(pool):
    pool._refusal = 'exited before it was ready'
    pool.admit(running=1, inflight=1)
    pool.admit(running=3, inflight=3)
    assert pool.launched == []
    pool._refusal = ''
    pool.begin_stop()
    pool.admit(running=5, inflight=5)
    assert pool.launched == []


def test_primary_pool_runs_shards_in_the_manager_resolved_root(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lib.durable_worker.runtime import CLAIM_GATE_EXTENSION
    from serverctl_pkg import execution_host
    monkeypatch.delenv('TOFU_PROJECT_PATH', raising=False)
    app = SimpleNamespace(extensions={'tofu_execution_import_source': _Source()})
    args = SimpleNamespace(report_fd=None, launch_environment={'X': '1'},
                           state_file=str(tmp_path / 'execution' / 'state.json'))
    record = {'plan': {'python': sys.executable}, 'projectPath': str(REPO)}

    gate = execution_host._claim_gate(args, app, record, request_stop=None, loop=None)

    assert app.extensions[CLAIM_GATE_EXTENSION] is gate
    assert gate._checkout == REPO
    gate.join()


def test_primary_boot_without_orphans_touches_nothing(tmp_path):
    from serverctl_pkg import execution_shards
    directory = tmp_path / 'execution-shards'

    def boot():
        return execution_shards.ShardPool(
            source=_Source(), plan={'python': sys.executable}, checkout=REPO,
            python=sys.executable, launch_environment={}, directory=directory)

    boot()
    assert not os.path.lexists(directory)
    directory.mkdir()
    before = directory.stat().st_mtime_ns
    boot()
    assert list(directory.iterdir()) == [] and directory.stat().st_mtime_ns == before


def test_snapshot_file_is_private_and_written_once(pool, tmp_path):
    first = pool._snapshot_path()
    assert first == pool._snapshot_path()
    assert first.stat().st_mode & 0o777 == 0o600
    pool.join()
    assert not first.exists()


# ── command controls ────────────────────────────────────────────────────────

@pytest.fixture
def controls(pool, monkeypatch):
    from lib.tasks_pkg import command_control_transport, command_controls
    local = {'value': None}
    asked = []
    answers = {}

    def apply(method, task_id, *, user_id, deadline_unix_ms=None, **params):
        if method == 'mcp_reconcile':
            return {'scheduled': True}
        return local['value'] if method != 'background_activity' else {
            'swarms': [{'swarm_id': 'local'}], 'backgroundCommands': []}

    def call(endpoint, method, task_id, *, user_id, params=None, deadline_unix_ms=None):
        asked.append((endpoint['port'], method, deadline_unix_ms))
        answer = answers[endpoint['port']]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(command_controls, 'apply_command_control', apply)
    monkeypatch.setattr(command_control_transport, 'call', call)
    monkeypatch.setattr(pool, '_endpoints', lambda: [{'port': port} for port in sorted(answers)])
    return local, asked, answers


def test_local_task_answers_without_asking_shards(pool, controls):
    local, asked, answers = controls
    local['value'] = {'interrupted': True, 'pid': 7}
    answers[1] = {'interrupted': False}
    assert pool.apply_command_control('interrupt', 't', user_id=1) == {'interrupted': True, 'pid': 7}
    assert asked == []


def test_shard_holding_the_task_answers_within_the_forwarded_deadline(pool, controls):
    _local, asked, answers = controls
    answers[1] = None
    answers[2] = {'aborting': True, 'status': 'running'}
    assert pool.apply_command_control('task_abort', 't', user_id=1, deadline_unix_ms=123) == {
        'aborting': True, 'status': 'running'}
    assert asked == [(1, 'task_abort', 123), (2, 'task_abort', 123)]


def test_unreachable_shard_makes_the_answer_unavailable_never_not_found(pool, controls):
    from lib.tasks_pkg.command_control_transport import CommandControlUnavailable
    _local, _asked, answers = controls
    answers[1] = CommandControlUnavailable('lost')
    answers[2] = None
    with pytest.raises(CommandControlUnavailable):
        pool.apply_command_control('swarm_status', 't', user_id=1)
    answers[3] = {'active': True, 'known': True, 'agents': []}
    assert pool.apply_command_control('swarm_status', 't', user_id=1)['active'] is True


def test_owner_views_union_every_process(pool, controls):
    _local, _asked, answers = controls
    answers[1] = {'swarms': [{'swarm_id': 'shard'}, {'swarm_id': 'local'}], 'backgroundCommands': []}
    view = pool.apply_command_control('background_activity', '', user_id=1)
    assert [row['swarm_id'] for row in view['swarms']] == ['local', 'shard']


def test_mcp_management_applies_once_on_the_host_its_shards_relay_to(pool, controls):
    """Shards own no MCP connections (lib/mcp/bridge_relay.py): the primary's
    bridge is the only one to reconcile, and an unreachable shard cannot make
    the answer unconfirmed."""
    from lib.tasks_pkg.command_control_transport import CommandControlUnavailable
    _local, asked, answers = controls
    answers[1] = CommandControlUnavailable('lost')
    params = {'server': 'docs', 'action': 'connect'}
    assert pool.apply_command_control('mcp_reconcile', '', user_id=1, **params) == {'scheduled': True}
    assert asked == []


# ── durable worker coordinator consults the gate ───────────────────────────

def test_coordinator_probes_only_while_its_gate_admits():
    from lib.durable_worker import WorkerRunResult
    from lib.durable_worker.runtime_policy import runtime_policy
    from lib.durable_worker.service import DurableWorkerService
    from lib.identity import PrincipalContext
    from tests.storage.test_durable_worker_foundation import _registration
    policy = runtime_policy()

    class Gate:
        def __init__(self):
            self.calls = []

        def bind(self, wake):
            self.wake = wake

        def admit(self, *, running, inflight):
            self.calls.append((running, inflight))
            return running == 0, None

    class Source:
        signals = None

        def start(self, hint):
            self.hint = hint
            return lambda: None

        def interrupt(self):
            pass
    gate, source = Gate(), Source()
    service = DurableWorkerService(
        principal=PrincipalContext.system(subject_id='test-control', scopes={policy['controlScope']}),
        client_factory=lambda: None, registrations=[_registration(lambda *args: None)],
        max_workers=4, now_ms=lambda: 0, policy=policy, wake_source=source, claim_gate=gate)
    gate.bind(service.wake)
    entered, release = [], threading.Event()

    def execute(observation):
        entered.append(observation)
        observation.claimed = True
        observation.claimable_in_ms = 0
        observation.known.set()
        service.wake()
        release.wait(5)
        return WorkerRunResult(state='succeeded', task_id='job')
    service._execute = execute
    service.start()
    try:
        deadline = time.monotonic() + 5
        while not hasattr(source, 'hint') and time.monotonic() < deadline:
            time.sleep(0.01)
        source.hint('hint')
        while not entered and time.monotonic() < deadline:
            time.sleep(0.01)
        for _ in range(3):
            source.hint('another job')
            time.sleep(0.05)
        assert len(entered) == 1, 'a refusing gate keeps the claim hint for later'
        assert (1, 1) in gate.calls
        release.set()
        while len(entered) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(entered) >= 2, 'the retained hint probes once the gate admits again'
    finally:
        release.set()
        assert service.stop(timeout=10)


# ── one generation across processes ────────────────────────────────────────

def test_shard_bootstrap_serves_the_exported_generation_not_the_rewritten_checkout(tmp_path):
    """A merge after the primary froze its pins never reaches a shard it starts."""
    import marshal
    from lib.runtime.pinned_import_source import PinnedImportSource
    root = tmp_path / 'checkout'
    package = root / 'genpkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'value.py').write_text('REVISION = "frozen"\n')
    (package / 'main.py').write_text(textwrap.dedent('''
        import sys
        from lib.runtime.pinned_import_source import bootstrapped
        from genpkg.value import REVISION
        print(REVISION, bootstrapped() is not None, sys.argv[1:])
    '''))
    # The application package the bootstrap loads from the snapshot as well.
    runtime_sources = {}
    for name in ('lib/__init__.py', 'lib/log.py', 'lib/runtime/__init__.py',
                 'lib/runtime/pinned_import_source.py'):
        runtime_sources[name] = (REPO / name).read_bytes()
    source = PinnedImportSource([str(root), str(REPO)])
    source.install()
    try:
        source.freeze()
    finally:
        source.uninstall()
    snapshot = source.snapshot()
    snapshot_path = tmp_path / 'snapshot.marshal'
    descriptor = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        marshal.dump(snapshot, stream)
    (package / 'value.py').write_text('REVISION = "merged later"\n')
    (package / 'added.py').write_text('')
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run(
        [sys.executable, str(REPO / 'serverctl_pkg/execution_shard_boot.py'), str(snapshot_path),
         'genpkg.main', '--flag'],
        cwd=root, env={**environment, 'PYTHONPATH': str(REPO)}, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split('\n')[-2] == "frozen True ['--flag']"
    assert source.identity() == PinnedImportSource.from_snapshot(snapshot).identity()


def test_snapshot_names_the_application_modules_the_primary_imported(tmp_path, monkeypatch):
    """A shard imports them before admitting work, so its first job is as warm as the primary's."""
    import importlib
    from lib.runtime.pinned_import_source import PinnedImportSource
    root = tmp_path / 'checkout'
    (root / 'warmpkg').mkdir(parents=True)
    (root / 'warmpkg' / '__init__.py').write_text('')
    (root / 'warmpkg' / 'used.py').write_text('')
    (root / 'warmpkg' / 'unused.py').write_text('')
    monkeypatch.syspath_prepend(str(root))
    source = PinnedImportSource([str(root)])
    source.install()
    try:
        source.freeze()
        importlib.import_module('warmpkg.used')
        snapshot = source.snapshot()
    finally:
        source.uninstall()
        for name in ('warmpkg.used', 'warmpkg'):
            sys.modules.pop(name, None)
    assert {'warmpkg', 'warmpkg.used'} <= set(snapshot['loaded'])
    assert 'warmpkg.unused' not in snapshot['loaded']
    assert set(PinnedImportSource.from_snapshot(snapshot).loaded_modules) == set(snapshot['loaded'])


def test_shard_bootstrap_refuses_a_foreign_readable_snapshot(tmp_path):
    snapshot_path = tmp_path / 'snapshot.marshal'
    snapshot_path.write_bytes(b'')
    snapshot_path.chmod(0o644)
    result = subprocess.run(
        [sys.executable, str(REPO / 'serverctl_pkg/execution_shard_boot.py'), str(snapshot_path), 'x'],
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert 'not private' in result.stderr


def test_shard_role_owns_attempt_execution_and_no_singleton():
    from lib.process_roles import (
        CAPABILITY_ATTEMPT_EXECUTION, CAPABILITY_NETWORK_CONFIGURATION, capabilities_for_role)
    assert capabilities_for_role('execution_shard') == frozenset({
        CAPABILITY_NETWORK_CONFIGURATION, CAPABILITY_ATTEMPT_EXECUTION})
    for role in ('all', 'worker', 'execution'):
        assert CAPABILITY_ATTEMPT_EXECUTION in capabilities_for_role(role)
