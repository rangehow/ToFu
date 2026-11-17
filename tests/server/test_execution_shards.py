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
    launched, warmed = [], []
    monkeypatch.setattr(execution_shards.ShardPool, '_launch', lambda self, shard: launched.append(shard))
    monkeypatch.setattr(execution_shards.ShardPool, '_warm_fork_server', lambda self: warmed.append(True))
    clock = [100.0]
    value = execution_shards.ShardPool(source=_Source(), plan={'python': sys.executable}, checkout=REPO,
                                       python=sys.executable, launch_environment={},
                                       directory=tmp_path / 'execution-shards', idle_seconds=600,
                                       clock=lambda: clock[0])
    value.launched, value.warmed, value.now = launched, warmed, clock
    yield value
    for shard in launched:
        for thread in shard.threads:
            thread.join()


class _Placed:
    """A claim placed on another thread, as the runner places it under its lease."""

    def __init__(self, pool, stopping=None):
        self.stopping = stopping or threading.Event()
        self.ticket = None
        self.thread = threading.Thread(target=self._run, args=(pool,), daemon=True)
        self.thread.start()

    def _run(self, pool):
        self.ticket = pool.place(None, stopping=self.stopping)

    def decided(self, timeout=5.0):
        self.thread.join(timeout)
        return None if self.thread.is_alive() else self.ticket


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def test_primary_alone_spawns_nothing_and_always_claims(pool):
    for _ in range(3):
        assert pool.admit(running=0, inflight=0) == (True, None)
        ticket = _Placed(pool).decided()
        assert ticket.entered and not ticket.released
        pool.leave(ticket)
    assert pool.launched == []


def test_a_claim_beside_the_primary_job_parks_and_starts_exactly_one_shard(pool):
    own = _Placed(pool).decided()
    assert own.entered
    parked = [_Placed(pool) for _ in range(3)]
    assert _until(lambda: len(pool.launched) == 3)
    assert all(placed.decided(0.2) is None for placed in parked), 'parked until a shard can take it'
    # A shard turning idle takes exactly one parked claim back to the queue.
    pool._reported(pool.launched[0], 'idle')
    released = [placed for placed in parked if placed.decided(0.5) is not None]
    assert len(released) == 1 and released[0].ticket.released and not released[0].ticket.entered
    assert pool.launched[0].reserved
    # The reserved shard is not offered a second claim before it claims one.
    pool._reported(pool.launched[0], 'idle')
    assert sum(placed.decided(0.2) is not None for placed in parked) == 1
    pool._reported(pool.launched[0], 'busy')
    assert not pool.launched[0].reserved
    assert len(pool.launched) == 3, 'parked claims never start a shard twice'
    # The primary's own job ending lets it enter the oldest parked claim itself.
    pool.leave(own)
    entered = [placed for placed in parked if placed.decided(0.5) is not None and placed.ticket.entered]
    assert len(entered) == 1


def test_busy_primary_leaves_arrivals_to_an_idle_shard_never_to_a_starting_one(pool):
    own = _Placed(pool).decided()
    parked = _Placed(pool)
    assert _until(lambda: len(pool.launched) == 1)
    (shard,) = pool.launched
    assert shard.state == 'starting'
    assert pool.admit(running=2, inflight=2)[0] is True
    pool._reported(shard, 'idle')
    assert parked.decided().released
    assert pool.admit(running=1, inflight=1)[0] is False
    pool._reported(shard, 'busy')
    assert pool.admit(running=1, inflight=1)[0] is True
    pool.leave(own)
    # An idle shard never keeps a free primary from its next job.
    pool._reported(shard, 'idle')
    assert pool.admit(running=0, inflight=0)[0] is True


def test_refusal_enters_every_parked_claim_and_spawns_nothing_more(pool):
    _Placed(pool).decided()
    parked = [_Placed(pool) for _ in range(2)]
    assert _until(lambda: len(pool.launched) == 2)
    pool._exited(pool.launched[0], ready=False, reason='exited before it was ready')
    assert all(placed.decided().entered for placed in parked)
    _Placed(pool).decided()
    assert len(pool.launched) == 2


def test_stopping_pool_enters_claims_and_an_interrupted_one_hands_them_back(pool):
    _Placed(pool).decided()
    parked = _Placed(pool)
    assert _until(lambda: len(pool.launched) == 1)
    pool.begin_stop()
    assert parked.decided().entered
    assert _Placed(pool).decided().entered
    assert len(pool.launched) == 1
    queued = _Placed(pool)
    pool.interrupt()
    assert queued.decided().entered, 'decided before the interrupt'
    assert _Placed(pool).decided().released
    stopping = threading.Event()
    stopping.set()
    assert _Placed(pool, stopping).decided().released


def test_first_job_warms_the_fork_server_once(pool):
    own = _Placed(pool).decided()
    assert _until(lambda: pool.warmed == [True])
    pool.leave(own)
    _Placed(pool).decided()
    time.sleep(0.05)
    assert pool.warmed == [True]


class _Server:
    pid = 1

    def __init__(self):
        self.closed = False

    def running(self):
        return not self.closed

    def close(self):
        self.closed = True


def test_idle_fork_server_retires_after_the_executor_idle_retention(pool):
    server = pool._fork_server = _Server()
    own = _Placed(pool).decided()
    assert pool.admit(running=1, inflight=1) == (True, None), 'never retires while work runs'
    pool.leave(own)
    assert pool.admit(running=0, inflight=0) == (True, 600)
    pool.now[0] += 599
    assert pool.admit(running=0, inflight=0) == (True, 1)
    pool.now[0] += 1
    assert pool.admit(running=0, inflight=0) == (True, None)
    assert _until(lambda: server.closed and pool._fork_server is None and pool._retiring is None)
    # Work before the deadline restarts the idle period.
    pool._fork_server = later = _Server()
    assert pool.admit(running=0, inflight=0)[1] == 600
    pool.now[0] += 300
    pool.leave(_Placed(pool).decided())
    assert pool.admit(running=0, inflight=0)[1] == 600
    assert not later.closed


def test_exported_snapshot_is_private_and_holds_the_retained_generation(pool):
    import marshal
    exported = pool._export_snapshot()
    assert exported.stat().st_mode & 0o777 == 0o600
    with exported.open('rb') as stream:
        assert marshal.load(stream) == _Source().snapshot()
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


def _fork_server_checkout(tmp_path, preload):
    """A toy generation whose fork-server preload is ``preload`` (Python source)."""
    import marshal
    from lib.runtime.pinned_import_source import PinnedImportSource
    root = tmp_path / 'checkout'
    for package in ('genpkg', 'serverctl_pkg'):
        (root / package).mkdir(parents=True)
        (root / package / '__init__.py').write_text('')
    (root / 'genpkg' / 'value.py').write_text('REVISION = "frozen"\n')
    (root / 'genpkg' / 'main.py').write_text(textwrap.dedent('''
        import os, sys
        from genpkg.value import REVISION
        report = int(sys.argv[sys.argv.index('--report-fd') + 1])
        os.write(report, f"{REVISION} {os.getpid()} {os.getppid()} {os.getsid(0) == os.getpid()} "
                         f"{sys.argv[1:]}".encode())
    '''))
    (root / 'serverctl_pkg' / 'execution_shards.py').write_text(textwrap.dedent(preload) + textwrap.dedent('''
        def import_primary_modules(names):
            import importlib
            for name in names:
                importlib.import_module(name)
    '''))
    source = PinnedImportSource([str(root), str(REPO)])
    source.install()
    try:
        source.freeze()
    finally:
        source.uninstall()
    snapshot_path = tmp_path / 'snapshot.marshal'
    descriptor = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        marshal.dump(source.snapshot(), stream)
    # A merge after the primary froze its pins: no shard may see it.
    (root / 'genpkg' / 'value.py').write_text('REVISION = "merged later"\n')
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    return root, snapshot_path, {**environment, 'PYTHONPATH': str(REPO)}


def test_fork_server_preloads_once_and_forks_each_shard_with_its_own_descriptors(tmp_path):
    from serverctl_pkg.execution_shards import _ForkServer
    root, snapshot, environment = _fork_server_checkout(tmp_path, '''
        def preload_shard_image(source):
            import genpkg.value  # noqa: F401
    ''')
    server = _ForkServer([sys.executable, str(REPO / 'serverctl_pkg/execution_shard_boot.py'),
                          str(snapshot), 'genpkg.main'],
                         cwd=root, env=environment, log=tmp_path / 'logs' / 'console.log')
    try:
        server.await_ready()
        snapshot.unlink()  # Loaded whole: forked shards never read it.
        for flag in ('a', 'b'):
            read, write = os.pipe()
            try:
                pid = server.fork(['--service-id', flag, '--report-fd', str(write)], (write,),
                                  ('--report-fd',))
            finally:
                os.close(write)
            with os.fdopen(read, 'rb') as stream:
                report = stream.read().decode()
            revision, child, parent, leader, argv = report.split(' ', 4)
            assert int(child) == pid != server.pid and int(parent) == server.pid
            assert (revision, leader) == ('frozen', 'True'), 'a new session from the frozen image'
            assert argv.startswith(f"['--service-id', '{flag}', '--report-fd', ")
        assert _until(lambda: not Path(f'/proc/{pid}').exists()), 'the fork server reaps its shards'
        spare = os.dup(0)
        try:
            with pytest.raises(RuntimeError, match='has no value'):
                server.fork(['--report-fd'], (spare,), ('--report-fd',))
        finally:
            os.close(spare)
    finally:
        server.close()
    assert not server.running(), 'EOF from the primary ends the fork server'


def test_fork_server_refuses_an_image_that_started_a_thread(tmp_path):
    from serverctl_pkg.execution_shards import _ForkServer
    root, snapshot, environment = _fork_server_checkout(tmp_path, '''
        import threading
        def preload_shard_image(source):
            threading.Thread(target=threading.Event().wait, daemon=True).start()
    ''')
    server = _ForkServer([sys.executable, str(REPO / 'serverctl_pkg/execution_shard_boot.py'),
                          str(snapshot), 'genpkg.main'],
                         cwd=root, env=environment, log=tmp_path / 'logs' / 'console.log')
    try:
        with pytest.raises(RuntimeError, match='fork is unsafe'):
            server.await_ready()
    finally:
        server.close()


def test_real_shard_image_preloads_without_starting_a_thread(tmp_path):
    """The fork server's safety premise for the application itself."""
    script = textwrap.dedent('''
        import sys, threading
        from serverctl_pkg.execution_shards import preload_shard_image
        class Source:
            loaded_modules = ('lib.tasks_pkg.orchestrator.api', 'lib.llm.stream')
        preload_shard_image(Source())
        print('threads', threading.active_count(), [thread.name for thread in threading.enumerate()])
    ''')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('TOFU_') and key not in ('PORT', 'PYTHONPATH')}
    environment.update(PYTHONPATH=str(REPO), HOME=str(tmp_path), TOFU_PROCESS_ROLE='execution_shard',
                       TOFU_PROJECT_PATH=str(REPO), TOFU_DATA_DIR=str(tmp_path / 'data'),
                       TOFU_HEARTBEAT_DIR=str(tmp_path / 'heartbeat'))
    result = subprocess.run([sys.executable, '-c', script], cwd=REPO, env=environment,
                            capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "threads 1 ['MainThread']"


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


def test_a_forked_shard_never_shares_its_fork_server_process_identity():
    """Import-time process identities are re-derived in every forked child."""
    script = textwrap.dedent('''
        import os
        from lib import boot_identity, turn_lifecycle
        from lib.conversation_sync import service
        def identity():
            return (boot_identity.BOOT_ID, boot_identity.PID, turn_lifecycle._ATTEMPT_DISPATCH_OWNER_ID,
                    service.boot_identity.BOOT_ID)
        parent = identity()
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.write(write, repr(identity()).encode())
            os._exit(0)
        os.close(write)
        child = eval(os.read(read, 4096).decode())
        os.waitpid(pid, 0)
        assert child[1] == pid and parent[1] == os.getpid()
        assert child[0] != parent[0] and child[2] != parent[2], (parent, child)
        assert child[3] == child[0], 'the sync service reads the live boot identity'
        assert identity() == parent
        print('ok')
    ''')
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, '-c', script], cwd=REPO, capture_output=True, text=True,
                            env={**environment, 'PYTHONPATH': str(REPO)}, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith('ok')


# Identities a module computes at import, each re-derived in a forked child
# (``os.register_at_fork``). A new one fails the audit below until it is too.
_FORK_REDERIVED_IDENTITIES = {
    'pid_values': {'lib.boot_identity.PID'},
    'random_identities': {'lib.boot_identity.BOOT_ID', 'lib.turn_lifecycle._ATTEMPT_DISPATCH_OWNER_ID'},
}


def test_fork_audit_of_the_real_shard_image(tmp_path):
    """docs/DEFENSIVE_PATTERNS.md fork rules, checked on the preloaded application image.

    At the fork point the image runs one thread and holds no module lock (so no
    child inherits a lock some other thread owned), opens no socket or storage
    connection (each shard opens its own), and its only process identities are
    the ones re-derived after fork. Its open descriptors are the append-mode log
    files, which every process of the instance opens the same way.
    """
    import json
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('TOFU_') and key not in ('PORT', 'PYTHONPATH')}
    environment.update(PYTHONPATH=str(REPO), HOME=str(tmp_path), TOFU_PROCESS_ROLE='execution_shard',
                       TOFU_PROJECT_PATH=str(REPO), TOFU_DATA_DIR=str(tmp_path / 'data'),
                       TOFU_HEARTBEAT_DIR=str(tmp_path / 'heartbeat'))
    result = subprocess.run(
        [sys.executable, str(REPO / 'tests/server/fork_audit_probe.py'), 'lib.tasks_pkg.orchestrator.api',
         'lib.llm.stream', 'lib.project_mod.run_command', 'lib.tasks_pkg.event_delivery',
         'lib.conversation_sync.worker_execution'],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr[-2000:]
    audit = json.loads(result.stdout.strip().splitlines()[-1])
    assert audit['threads'] == ['MainThread']
    assert audit['held_locks'] == []
    assert audit['sockets'] == [] and audit['connections'] == []
    for kind, allowed in _FORK_REDERIVED_IDENTITIES.items():
        assert set(audit[kind]) <= allowed, f'{kind} not re-derived after fork: {set(audit[kind]) - allowed}'
    assert all(path.endswith('.log') for path in audit['descriptors']), audit['descriptors']
