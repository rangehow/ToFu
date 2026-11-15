"""Storage supervisor lifecycle evidence: every transition reaches the log.

2026-09-21 production gap: the managed tofu-db daemon died and the app stayed
fenced for ~18 minutes with ZERO lines from logger ``tofu.storage.supervisor``
in app.log. Root cause was two-fold and both halves are pinned here:

* routing: ``tofu.*`` logger names were not in the business prefix set, so
  app.log/error.log dropped every supervisor record (WARNING+ leaked only to
  vendor.log, INFO vanished entirely);
* coverage: spawn, termination-escalation, exit (with signal/uptime), and
  restart scheduling transitions now all emit through
  ``lib.log.get_logger`` at appropriate levels.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading

import pytest

import lib.storage.runtime as runtime_mod
import lib.storage.supervisor as supervisor_mod
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


@pytest.fixture(autouse=True)
def _no_kernel_wait(monkeypatch):
    """The fake children's PIDs may name unrelated host processes; pin the seam."""
    monkeypatch.setattr(supervisor_mod, '_engine_kernel_wait', lambda pid: None)


# ── Routing (the silent half of the incident) ────────────────────────────


def _record(name: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg='probe', args=(), exc_info=None)


def test_tofu_storage_loggers_are_business_records_not_vendor():
    from lib.server.server_logging import _BizAndServerOnly, _BizOnly, _VendorOnly

    for name in (
        'tofu.storage.supervisor',
        'tofu.storage.runtime',
        'tofu.storage.engine',
        'tofu.control_rpc',
        'tofu.orchestrator',
    ):
        assert _BizOnly().filter(_record(name)), name
        assert _BizAndServerOnly().filter(_record(name)), name
        assert not _VendorOnly().filter(_record(name)), name


def test_underscore_namespaces_keep_their_existing_routing():
    from lib.server.server_logging import _BizOnly, _VendorOnly

    # tofu_search was already business; tofu_agent/tofu_db keep vendor
    # routing (only their own WARNING+ diagnostics exist today).
    assert _BizOnly().filter(_record('tofu_search.pipeline'))
    assert _VendorOnly().filter(_record('tofu_agent.runtime'))
    assert _VendorOnly().filter(_record('tofu_db.wire'))
    assert _VendorOnly().filter(_record('urllib3.connectionpool'))


# ── Spawn/ready evidence through a fake child ────────────────────────────


class _FakePopen:
    """Popen double over real pipes: stdin lease, stdout control, stderr."""

    def __init__(self):
        self.pid = 424243
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        self.stdin = os.fdopen(in_w, 'w')
        self.stdout = os.fdopen(out_r, 'r')
        self.stderr = os.fdopen(err_r, 'r')
        self._stdout_writer = os.fdopen(out_w, 'w')
        self._stderr_writer = os.fdopen(err_w, 'w')
        self._stdin_reader = os.fdopen(in_r, 'r')
        self._returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def emit(self, obj) -> None:
        self._stdout_writer.write(json.dumps(obj) + '\n')
        self._stdout_writer.flush()

    def emit_stderr(self, line: str) -> None:
        self._stderr_writer.write(line + '\n')
        self._stderr_writer.flush()

    def _die(self, code: int) -> None:
        # A real dying child closes its pipe write ends: stdout/stderr
        # readers observe EOF and the stderr drainer thread exits.
        self._returncode = code
        for writer in (self._stdout_writer, self._stderr_writer):
            try:
                writer.close()
            except OSError:
                pass

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is not None:
            return self._returncode
        raise subprocess.TimeoutExpired(cmd='fake-popen', timeout=timeout)

    def terminate(self):
        self.terminate_calls += 1
        self._die(-15)

    def kill(self):
        self.kill_calls += 1
        self._die(-9)

    def close_pipes(self):
        for stream in (
            self.stdin, self.stdout, self.stderr,
            self._stdout_writer, self._stderr_writer, self._stdin_reader,
        ):
            try:
                stream.close()
            except OSError:
                pass


class _ReadyClient:
    def health(self, deadline=None):
        return {'ready': True, 'backend': 'sqlite'}


def _start_with_fake_child(monkeypatch, child):
    monkeypatch.setattr(
        supervisor_mod.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(
        supervisor_mod, 'StorageClient', lambda *a, **kw: _ReadyClient())
    supervisor = StorageSupervisor(
        project_root=None, backend='sqlite', startup_timeout=5.0)
    return supervisor


def test_spawn_and_ready_transitions_are_logged(monkeypatch, caplog):
    child = _FakePopen()
    supervisor = _start_with_fake_child(monkeypatch, child)
    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        import threading as _t
        emitter = _t.Timer(0.05, lambda: child.emit({
            'type': 'storage.ready', 'protocol': 'storage.v1',
            'port': 47111, 'backend': 'sqlite'}))
        emitter.start()
        try:
            client = supervisor.start()
        finally:
            emitter.cancel()
    assert client is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any('spawned pid=424243' in m for m in messages), messages
    assert any('ready pid=424243' in m for m in messages), messages
    # stderr=PIPE with a live drainer bound to this child.
    assert supervisor._stderr_drainer is not None
    supervisor.stop(timeout=2.0)
    child.close_pipes()


# ── Monitor/termination/exit evidence ────────────────────────────────────


class _OwnerChannel:
    def __init__(self, on_close):
        self._on_close = on_close
        self.closed = False

    def close(self):
        self.closed = True
        self._on_close()


class _FakeChild:
    def __init__(self, *, die_on_eof=False, die_on_term=True, exit_code=0):
        self._die_on_eof = die_on_eof
        self.stdin = _OwnerChannel(self._eof)
        self.pid = 424244
        self.terminate_calls = 0
        self.kill_calls = 0
        self._die_on_term = die_on_term
        self._exit_code = exit_code
        self._returncode = None
        self._exited = threading.Event()

    def _eof(self):
        if self._die_on_eof:
            self._exit(self._exit_code)

    def _exit(self, code=None):
        self._returncode = self._exit_code if code is None else code
        self._exited.set()

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._exited.wait(timeout):
            return self._returncode
        raise subprocess.TimeoutExpired(cmd='fake-child', timeout=timeout)

    def terminate(self):
        self.terminate_calls += 1
        if self._die_on_term:
            self._exit(self._exit_code)

    def kill(self):
        self.kill_calls += 1
        self._exit(-9)


class _AlwaysFailingClient:
    def health(self, deadline=None):
        raise RuntimeError('authority is unavailable')


def _wired_supervisor(*, interval=0.01, deadline=0.01, threshold=3):
    supervisor = StorageSupervisor()
    supervisor._liveness_interval_seconds = interval
    supervisor._liveness_probe_deadline_seconds = deadline
    supervisor._liveness_failure_threshold = threshold
    # Count-threshold mechanics; the unresponsiveness budget has its own specs.
    supervisor._liveness_unresponsive_seconds = 0.0
    return supervisor


def test_recycle_and_exit_emit_every_lifecycle_transition(
        monkeypatch, caplog):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_LIVENESS_TERMINATE_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor()
    supervisor._process_started_at = 1.0  # any past marker → uptime note
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _AlwaysFailingClient()

    with caplog.at_level(logging.DEBUG, logger='tofu.storage.supervisor'):
        thread = threading.Thread(
            target=supervisor._monitor_process, args=(child,), daemon=True)
        thread.start()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert crashes == [-9]
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    text = '\n'.join(message for _level, message in records)
    # liveness probe failures (count/threshold)
    assert 'liveness probe failed (1/3)' in text
    assert 'liveness probe failed (2/3)' in text
    assert 'failed 3 consecutive liveness probes' in text
    # termination escalation steps, in order
    assert 'closing the owner channel' in text
    assert 'sending SIGTERM' in text
    assert 'sending SIGKILL' in text
    assert text.index('owner channel') < text.index('SIGTERM') \
        < text.index('SIGKILL')
    # child exit with code, decoded signal, and uptime
    assert 'exited unexpectedly exit_code=-9' in text
    assert 'signal=9' in text
    assert 'uptime=' in text
    levels = {message: level for level, message in records}
    assert any(level == logging.CRITICAL and 'exited unexpectedly' in message
               for level, message in records)


def test_clean_stop_exit_is_logged_at_info(caplog):
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=0)
    supervisor._process = child
    supervisor._client = None
    supervisor._intentional_stop = True
    child._exit(0)

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        supervisor._monitor_process(child)
    messages = [r.getMessage() for r in caplog.records]
    assert any('stopped exit_code=0' in m for m in messages), messages
    assert not any('exited unexpectedly' in m for m in messages), messages


def test_unreapable_stop_is_reported_and_leaves_cleanup_to_the_os(
        monkeypatch, caplog):
    """SIGKILL cannot interrupt uninterruptible I/O; stop must still return.

    2026-11-06: on a saturated FUSE authority the daemon ignored SIGKILL for
    longer than the confirm window. ``process.wait`` raised out of the
    resident host's finalizer, the host died without writing its terminal
    record, and the manager latched the explicit restart as a crash loop with
    the application down. A kernel-state child no software recycle can help
    is a reported bounded outcome, never a new exception.
    """
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_KILL_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor()
    child = _FakeChild(die_on_eof=False, die_on_term=False)

    def ignore_kill():
        child.kill_calls += 1

    child.kill = ignore_kill
    supervisor._process = child
    supervisor._client = None
    supervisor._intentional_stop = True

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        supervisor.stop(timeout=0.05)

    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any('state=unreapable' in m for m in messages), messages
    assert any(r.levelno == logging.CRITICAL and 'unreapable' in r.getMessage()
               for r in caplog.records), messages
    assert supervisor._process is None


def _eventually(predicate, timeout: float = 10.0) -> bool:
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not predicate():
            if deadline.wait(0.01):
                return predicate()
        return True
    finally:
        timer.cancel()


def test_unreapable_recycle_defers_its_successor_without_feeding_the_breaker(
        monkeypatch, caplog):
    """A successor starts only after the kernel reaps the SIGKILLed engine.

    2026-11-11: a starved engine hung in uninterruptible FUSE I/O and ignored
    SIGKILL. Its descriptors, and the project lease with them, survived until
    the kernel finished its exit about five minutes later. Meanwhile the
    restart loop spawned five successors in 8.5 s; each died on "Another
    storage sidecar holds the project lease", the breaker counted them as a
    crash storm, and storage stayed fenced for 92 minutes until a manual
    restart. A successor started before the reap can only lose the lease, so
    the start waits for the reap, and waiting is not crash pressure.
    """
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '2')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '600')
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.02)
    for grace in ('_LIVENESS_EOF_GRACE_SECONDS',
                  '_LIVENESS_TERMINATE_GRACE_SECONDS',
                  '_LIVENESS_KILL_GRACE_SECONDS'):
        monkeypatch.setattr(supervisor_mod, grace, 0.02)
    predecessor = _FakeChild(die_on_eof=False, die_on_term=False)
    predecessor.kill = lambda: None
    spawned = []

    def spawn(*_args, **_kwargs):
        child = _FakePopen()
        if predecessor.poll() is None:
            # The real successor exits 1 when the predecessor holds the lease.
            child._die(1)
        else:
            child.emit({'type': 'storage.ready', 'protocol': 'storage.v1',
                        'port': 47111, 'backend': 'sqlite'})
        spawned.append(child)
        return child

    monkeypatch.setattr(supervisor_mod.subprocess, 'Popen', spawn)
    monkeypatch.setattr(
        supervisor_mod, 'StorageClient', lambda *a, **kw: _ReadyClient())
    supervisor = StorageSupervisor(
        project_root=None, backend='sqlite', startup_timeout=5.0)
    wired = _wired_supervisor()
    for knob in ('_liveness_interval_seconds',
                 '_liveness_probe_deadline_seconds',
                 '_liveness_failure_threshold',
                 '_liveness_unresponsive_seconds'):
        setattr(supervisor, knob, getattr(wired, knob))
    runtime = StorageRuntime(supervisor)
    supervisor._process = predecessor
    supervisor._client = _AlwaysFailingClient()
    try:
        with caplog.at_level(logging.INFO):
            monitor = threading.Thread(
                target=supervisor._monitor_process, args=(predecessor,),
                daemon=True)
            monitor.start()
            monitor.join(timeout=10)
            assert not monitor.is_alive()
            _eventually(lambda: runtime.status()['restart_attempts'] > 4
                        or runtime.status()['degraded'])
            waiting = runtime.status()
            assert spawned == []
            assert waiting['degraded'] is False
            assert waiting['state'] == 'restarting'
            assert f'pid={predecessor.pid}' in waiting['last_error']

            predecessor._exit(-9)
            assert _eventually(lambda: runtime.ready)
        assert len(spawned) == 1
        assert runtime.status()['degraded'] is False
        assert any(f'pid={predecessor.pid} was reaped' in r.getMessage()
                   for r in caplog.records)
    finally:
        runtime.stop(timeout=2.0)
        for child in spawned:
            child.close_pipes()


class _LeaseHoldingChild(_FakePopen):
    """A fake child holding the project lease's OS lock while it lives, as the real one does."""

    def __init__(self, data_dir):
        import fcntl
        super().__init__()
        self._lease = (data_dir / '.storage-sidecar.lock').open('a+b')
        # A second child started while this one lives could only lose the lease.
        fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.emit({'type': 'storage.ready', 'protocol': 'storage.v1',
                   'port': 47111, 'backend': 'sqlite'})

    def _die(self, code: int) -> None:
        super()._die(code)
        self._lease.close()

    def close_pipes(self):
        super().close_pipes()
        self._lease.close()


def _lease_holding_supervisor(tmp_path, monkeypatch, client_factory=_ReadyClient):
    (tmp_path / 'data').mkdir()
    spawned = []

    def spawn(*_args, **_kwargs):
        spawned.append(_LeaseHoldingChild(tmp_path / 'data'))
        return spawned[-1]

    monkeypatch.setattr(supervisor_mod.subprocess, 'Popen', spawn)
    monkeypatch.setattr(
        supervisor_mod, 'StorageClient', lambda *a, **kw: client_factory())
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=5.0)
    return supervisor, spawned


def test_a_start_on_a_serving_supervisor_returns_its_child_and_keeps_writes(
        tmp_path, monkeypatch):
    """A start on a supervisor that serves its own child returns that child.

    2026-11-12: the lease probe ran before start()'s ready check, so a second
    start() on a supervisor serving its child (the harness's start_storage()
    over an installed runtime) saw that child's own lease and raised
    StorageStartDeferred, and StorageRuntime then marked the healthy runtime
    not ready: every later write was fenced. The probe guards a spawn, never
    the running child.
    """
    supervisor, spawned = _lease_holding_supervisor(tmp_path, monkeypatch)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    try:
        first = runtime.start()
        assert supervisor.start() is first
        assert runtime.start() is first
        assert len(spawned) == 1
        assert runtime.ready
        runtime.client(write=True)
    finally:
        runtime.stop(timeout=2.0)
        for child in spawned:
            child.close_pipes()


def test_a_restart_whose_ready_check_fails_keeps_its_serving_successor(
        tmp_path, monkeypatch):
    """A crash restart never defers on the lease of the successor it started.

    The successor answered its handshake, but the runtime's own readiness
    check then failed (a timeout under host load), so the next restart
    attempt met a supervisor serving that successor. Before the fix that
    attempt deferred on the successor's own lease, and a deferral is never
    a crash strike: the restart worker deferred forever while a healthy
    engine served, and storage stayed fenced until an explicit restart.
    """
    from lib.storage.errors import StorageError
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.02)
    readiness = {'timeouts': 1}

    class _FirstReadinessCheckTimesOut(_ReadyClient):
        def health(self, deadline=None):
            # Supervisor handshakes and liveness probes pass a deadline; only
            # the runtime's readiness check after a start does not.
            if deadline is None and readiness['timeouts']:
                readiness['timeouts'] -= 1
                raise StorageError('database_timeout', 'storage health timed out',
                                   retryable=True)
            return super().health(deadline)

    supervisor, spawned = _lease_holding_supervisor(
        tmp_path, monkeypatch, _FirstReadinessCheckTimesOut)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-1)
        assert _eventually(lambda: runtime.ready), runtime.status()['last_error']
        assert readiness['timeouts'] == 0
        assert len(spawned) == 1
        assert runtime.status()['degraded'] is False
    finally:
        runtime.stop(timeout=2.0)
        for child in spawned:
            child.close_pipes()


def test_a_held_project_lease_defers_the_start_without_feeding_the_breaker(
        tmp_path, monkeypatch):
    """A start waits for any holder of the project lease, then recovers by itself.

    Every storage child takes the project lease's OS lock before it serves; a
    child started while another process holds it can only exit "Another
    storage sidecar holds the project lease". 2026-11-11 (19:54 and 23:53
    CST) five such starts in 8 s latched the breaker about 50 s before the
    lease freed, and storage stayed fenced until an explicit restart. While
    the lease is held the start defers without spawning or a crash strike;
    once it frees, the successor starts and storage recovers by itself.
    """
    import fcntl
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '2')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '600')
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.02)
    data = tmp_path / 'data'
    data.mkdir()
    holder = (data / '.storage-sidecar.lock').open('a+b')
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    spawned = []

    def spawn(*_args, **_kwargs):
        child = _FakePopen()
        child.emit({'type': 'storage.ready', 'protocol': 'storage.v1',
                    'port': 47111, 'backend': 'sqlite'})
        spawned.append(child)
        return child

    monkeypatch.setattr(supervisor_mod.subprocess, 'Popen', spawn)
    monkeypatch.setattr(
        supervisor_mod, 'StorageClient', lambda *a, **kw: _ReadyClient())
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=5.0)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-1)
        _eventually(lambda: runtime.status()['restart_attempts'] > 4
                    or runtime.status()['degraded'])
        held = runtime.status()
        assert spawned == []
        assert held['degraded'] is False
        assert held['state'] == 'restarting'
        assert 'project lease is held' in held['last_error']

        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        assert _eventually(lambda: runtime.ready)
        assert len(spawned) == 1
        assert runtime.status()['degraded'] is False
    finally:
        holder.close()
        runtime.stop(timeout=2.0)
        for child in spawned:
            child.close_pipes()


def test_unreapable_recycle_reports_kernel_wait_evidence_not_a_native_crash(
        monkeypatch, caplog):
    """An unreaped SIGKILL logs what the child waits on and claims no crash.

    2026-11-11: both unreapable engines were in uninterruptible kernel waits,
    and no state, wchan or stack was captured, so a FUSE stall could not be
    told from memory-cgroup reclaim. The synthetic -1 that fences storage was
    also logged as "the engine died from a native signal", which it did not.
    """
    import re
    for grace in ('_LIVENESS_EOF_GRACE_SECONDS',
                  '_LIVENESS_TERMINATE_GRACE_SECONDS',
                  '_LIVENESS_KILL_GRACE_SECONDS'):
        monkeypatch.setattr(supervisor_mod, grace, 0.02)
    supervisor = _wired_supervisor()
    supervisor._process_started_at = 1.0
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    child.kill = lambda: None
    # A live process whose procfs facts are readable stands in for the engine.
    child.pid = os.getpid()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _AlwaysFailingClient()

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        monitor = threading.Thread(
            target=supervisor._monitor_process, args=(child,), daemon=True)
        monitor.start()
        monitor.join(timeout=10)

    assert not monitor.is_alive()
    assert crashes == [-1]
    critical = [r.getMessage() for r in caplog.records
                if r.levelno == logging.CRITICAL]
    [unreapable] = [m for m in critical if 'ignored SIGKILL' in m]
    assert re.search(r'state=[A-Za-z]\b', unreapable), unreapable
    assert 'wchan=' in unreapable and 'stack=' in unreapable
    assert not any('native signal' in m for m in critical), critical
    assert any(f'pid={os.getpid()} did not exit after SIGKILL' in m
               for m in critical), critical


def test_unreapable_evidence_names_the_thread_the_kernel_holds(monkeypatch):
    """The unreapable line reuses the liveness verdict's per-thread reader.

    wchan and the kernel stack separate a network-filesystem stall from
    memory-cgroup reclaim; ``_engine_kernel_wait`` names the held thread and
    the file it waits on. One reader serves the verdict and the evidence.
    """
    pid = os.getpid()
    monkeypatch.setattr(
        supervisor_mod, '_engine_kernel_wait',
        lambda target: f'thread {target} is inside read() on /data/segment')
    evidence = supervisor_mod._kernel_wait_evidence(pid)
    assert 'state=' in evidence and 'wchan=' in evidence
    assert evidence.endswith(
        f'held=thread {pid} is inside read() on /data/segment')
    monkeypatch.setattr(supervisor_mod, '_engine_kernel_wait', lambda _pid: None)
    assert 'held=' not in supervisor_mod._kernel_wait_evidence(pid)


def test_attach_timeout_names_the_last_attach_error(tmp_path):
    """An attachment that times out says why it never attached.

    2026-11-11: both application hosts logged "external storage sidecar
    startup timed out" every 15 minutes for 92 minutes. The error each
    attempt last saw was dropped, so the logs could not tell an owner that
    published no engine from an engine that never answered.
    """
    supervisor = StorageSupervisor(
        connection_file=str(tmp_path / 'storage-connection.json'),
        startup_timeout=1.0)
    with pytest.raises(RuntimeError) as excinfo:
        supervisor.start()
    message = str(excinfo.value)
    assert message.startswith('external storage sidecar startup timed out')
    assert ('last error: RuntimeError: storage connection file is not '
            'available') in message


# ── Restart scheduling/attempt evidence ──────────────────────────────────


class _FlappingSupervisor:
    """Supervisor double: start() fails a scripted number of times."""

    ready = False

    def __init__(self, failures: int):
        self.callback = None
        self.failures = failures
        self.starts = 0

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError('spawn refused')
        self.ready = True
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {
            'ready': self.ready,
            'state': 'ready' if self.ready else 'exited',
            'backend': 'sqlite',
            'pid': None,
            'port': None,
            'last_exit_code': -9,
        }


def test_restart_scheduling_and_attempts_are_logged(caplog):
    runtime = StorageRuntime(_FlappingSupervisor(failures=2))
    with caplog.at_level(logging.INFO, logger='tofu.storage.runtime'):
        runtime._crashed(-9)
        thread = runtime._restart_thread
        assert thread is not None
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert runtime.ready
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    text = '\n'.join(message for _level, message in records)
    assert 'writes fenced after sidecar exit code=-9; scheduling restart' in text
    assert 'restart attempt 1 failed' in text
    assert 'retrying in' in text
    assert 'restart succeeded after 3 attempt(s)' in text
    assert any(level == logging.WARNING and 'scheduling restart' in message
               for level, message in records)
    runtime.stop(timeout=5.0)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contract')
def test_lease_probe_defers_a_spawn_without_the_host_lifecycle_manager(
        tmp_path, monkeypatch):
    """Packaged consumers (plugins on tofu-agent) ship no ``server_manager``.

    The spawn guard imported the lease reader from that host-only module, so
    every packaged supervisor start failed with ModuleNotFoundError before it
    could defer or spawn (tofu-trading CI, 2026-11-15).
    """
    import fcntl
    import sys

    from lib.storage.errors import StorageStartDeferred

    monkeypatch.setitem(sys.modules, 'server_manager', None)

    def refuse_spawn(*_args, **_kwargs):
        raise AssertionError('a held lease must defer before any spawn')

    monkeypatch.setattr(supervisor_mod.subprocess, 'Popen', refuse_spawn)
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    with (data_dir / '.storage-sidecar.lock').open('a+b') as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor = StorageSupervisor(
            project_root=tmp_path, backend='sqlite', startup_timeout=5.0)
        with pytest.raises(StorageStartDeferred, match='lease is held'):
            supervisor.start()
