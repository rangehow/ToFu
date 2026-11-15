"""Managed-child liveness supervision for the storage sidecar.

The managed monitor must detect a live-but-unresponsive child (wedged
daemon: process alive, listener dead) and recycle it through the ordinary
exit path — fence readiness, fire crash callbacks, let the runtime restart
loop spawn a replacement. Waiting on process exit alone missed that mode
and kept storage down until an operator restarted the application.

Liveness is judged by progress, never by latency or host pressure
(docs/STORAGE.md "Managed-child liveness"): a late answer is an answer, an
engine thread the kernel holds inside a filesystem call is never signalled,
and the load average or cgroup memory never decide a recycle.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage.supervisor import (
    StorageSupervisor,
    _liveness_probe_config,
)
import lib.storage.supervisor as supervisor_mod


pytestmark = pytest.mark.unit

# Captured before the autouse fixture pins the seam, for the /proc specs.
_REAL_ENGINE_KERNEL_WAIT = getattr(supervisor_mod, '_engine_kernel_wait', None)


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


@pytest.fixture(autouse=True)
def _no_kernel_wait_by_default(monkeypatch):
    """The fake children's PIDs may name unrelated host processes; pin the seam."""
    monkeypatch.setattr(supervisor_mod, '_engine_kernel_wait', lambda pid: None,
                        raising=False)


class _OwnerChannel:
    """stdin-pipe double: close() delivers the parent-lease EOF."""

    def __init__(self, on_close):
        self._on_close = on_close
        self.closed = False

    def close(self):
        self.closed = True
        self._on_close()


class _FakeChild:
    """Popen double: alive until told to exit; EOF/SIGTERM/SIGKILL visible."""

    def __init__(self, *, die_on_eof=False, die_on_term=True, exit_code=0,
                 exit_delay=0.0):
        self._die_on_eof = die_on_eof
        self.stdin = _OwnerChannel(self._eof)
        self.pid = 424242
        self.terminate_calls = 0
        self.kill_calls = 0
        self._die_on_term = die_on_term
        self._exit_code = exit_code
        self._returncode = None
        self._exited = threading.Event()
        if exit_delay:
            threading.Timer(exit_delay, self._exit).start()

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


class _FailingHealthClient:
    def __init__(self):
        self.calls = 0

    def health(self, deadline=None):
        self.calls += 1
        raise StorageError('database_unavailable', 'authority is unavailable')


class _HealthyThenExitClient:
    def __init__(self, fail_first=0):
        self.calls = 0
        self._fail_first = fail_first

    def health(self, deadline=None):
        self.calls += 1
        if self.calls <= self._fail_first:
            raise StorageError('database_unavailable', 'transient')
        return {'ready': True, 'backend': 'sqlite'}


class _FlappingHealthClient:
    """Fails ``fail_streak`` probes, then serves one, repeatedly."""

    def __init__(self, fail_streak):
        self.calls = 0
        self._period = fail_streak + 1

    def health(self, deadline=None):
        self.calls += 1
        if self.calls % self._period:
            raise StorageError('database_unavailable', 'starved')
        return {'ready': True, 'backend': 'tofudb'}


def _wired_supervisor(*, interval=0.01, deadline=0.01, threshold=3,
                      unresponsive=0.0):
    supervisor = StorageSupervisor()
    supervisor._liveness_interval_seconds = interval
    supervisor._liveness_probe_deadline_seconds = deadline
    supervisor._liveness_failure_threshold = threshold
    # Count-threshold mechanics by default; budget specs pass their own.
    supervisor._liveness_unresponsive_seconds = unresponsive
    return supervisor


def _start_monitor(supervisor, child):
    thread = threading.Thread(
        target=supervisor._monitor_process, args=(child,),
        name='test-storage-supervisor', daemon=True)
    thread.start()
    return thread


def test_wedged_child_is_killed_and_fired_through_the_crash_path(monkeypatch):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_LIVENESS_TERMINATE_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor()
    # Wedged daemon: ignores EOF and SIGTERM, answers nothing.
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert crashes == [-9]
    assert supervisor._last_exit_code == -9
    assert supervisor._client is None
    assert supervisor._process is None
    assert not supervisor.ready


def test_wedged_child_answering_eof_needs_no_signal(monkeypatch):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 1.0)
    supervisor = _wired_supervisor()
    child = _FakeChild(die_on_eof=True, exit_code=-15)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _FailingHealthClient()

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [-15]
    assert not supervisor.ready


def test_healthy_child_exits_without_any_recycle_signal():
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=1, exit_delay=0.15)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _HealthyThenExitClient()

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [1]
    assert supervisor._last_exit_code == 1


def test_transient_probe_failures_below_threshold_do_not_recycle():
    supervisor = _wired_supervisor(threshold=3)
    child = _FakeChild(exit_code=0, exit_delay=0.4)
    client = _HealthyThenExitClient(fail_first=2)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls >= 2
    assert child.terminate_calls == 0
    assert child.kill_calls == 0


def test_intentional_stop_never_probes_or_recycles():
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=0, exit_delay=0.2)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client
    supervisor._intentional_stop = True

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls == 0
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    # Intentional stop is not a crash: no callbacks, exit code recorded.
    assert crashes == []
    assert supervisor._last_exit_code == 0


def test_liveness_probe_config_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS',
                       raising=False)
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS',
                       raising=False)
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD',
                       raising=False)
    assert _liveness_probe_config({}) == (1.0, 1.5, 3)

    assert _liveness_probe_config({
        'TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS': '0.5',
        'TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS': '2.5',
        'TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD': '5',
    }) == (0.5, 2.5, 5)

    # Invalid values fall back; out-of-range values clamp into bounds.
    interval, deadline, threshold = _liveness_probe_config({
        'TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS': 'bogus',
        'TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS': '999',
        'TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD': '0',
    })
    assert interval == 1.0
    assert deadline == 10.0
    assert threshold == 1


# ── Progress, not latency or host pressure (2026-11-11) ───────────────────


class _LateAnswerClient:
    """Answers every probe, later than the probe deadline: slow, not dead.

    Like the storage.v2 client it honors the caller's deadline, so a probe
    abandoned at the deadline fails while a waited-for probe is answered.
    """

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0

    def health(self, deadline=None):
        self.calls += 1
        if deadline is not None and deadline < self.delay:
            time.sleep(deadline)
            raise StorageError('database_timeout', 'deadline elapsed', True)
        time.sleep(self.delay)
        return {'ready': True, 'backend': 'tofudb'}


def test_engine_answering_after_the_probe_deadline_is_never_recycled(caplog):
    """A slow engine answers; only an engine that stops answering is dead.

    Before 2026-11-11 an answer that missed the 1.5 s probe deadline counted
    as a failure, so an engine that answered every probe in 2 s under load
    was recycled once the unresponsive budget was spent.
    """
    supervisor = _wired_supervisor(deadline=0.02, unresponsive=0.3)
    child = _FakeChild(exit_code=0, exit_delay=1.5)
    client = _LateAnswerClient(delay=0.1)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _start_monitor(supervisor, child)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls >= 3
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [0]
    assert any('answered its liveness probe after' in record.getMessage()
               for record in caplog.records)


def test_engine_blocked_in_a_filesystem_call_is_kept_until_it_returns(
        monkeypatch, caplog):
    """A signal cannot recycle a thread the kernel holds in a filesystem call.

    2026-11-11 19:54: SIGTERM interrupted the engine's stalled volume reads
    and SIGKILL then left it unreapable while it kept the project lease; the
    restart loop failed on that lease until the breaker fenced storage for
    97 minutes. While the evidence names such a thread the engine is kept and
    probed; once no thread is held and it still does not answer, it is
    recycled as before.
    """
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    blocked = {'detail': 'thread 7 in pread64 on /data/tofudb-authority/blocks/ab'}
    monkeypatch.setattr(supervisor_mod, '_engine_kernel_wait',
                        lambda pid: blocked['detail'], raising=False)
    supervisor = _wired_supervisor(unresponsive=0.2)
    supervisor._backend = 'tofudb'
    child = _FakeChild(die_on_eof=False, die_on_term=True, exit_code=-15)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _start_monitor(supervisor, child)
        time.sleep(1.0)  # five budgets
        assert thread.is_alive()
        assert child.terminate_calls == 0
        assert child.kill_calls == 0
        assert child.stdin.closed is False
        # The kept engine is never silent: status() says why storage stalls.
        liveness = supervisor.status()['liveness']
        assert liveness['state'] == 'kernel-blocked'
        assert '/data/tofudb-authority/blocks/ab' in liveness['detail']
        assert liveness['unansweredSeconds'] >= 0.2
        assert liveness['since'] <= time.time() - 0.2
        blocked['detail'] = None
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 1
    assert crashes == [-15]
    assert any('blocked in the kernel' in record.getMessage()
               for record in caplog.records)


def test_host_pressure_never_decides_a_recycle(monkeypatch, caplog):
    """Load average and cgroup memory neither excuse nor condemn an engine.

    The removed overload gate withheld recycles while loadavg1 was at least
    twice the CPU count, so the same frozen engine was recycled at loadavg 139
    and kept at 140 (2026-11-11 23:19), and a forced recycle followed a fixed
    count of excused probes whatever the engine was doing.
    """
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(supervisor_mod.os, 'getloadavg', lambda: (10_000.0,) * 3)
    supervisor = _wired_supervisor(unresponsive=0.2)
    child = _FakeChild(die_on_eof=False, die_on_term=True, exit_code=-15)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _start_monitor(supervisor, child)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 1
    assert crashes == [-15]
    messages = [record.getMessage() for record in caplog.records]
    assert not any('overload' in message for message in messages)
    assert any('unresponsive for' in message for message in messages)


def test_late_answers_between_failures_end_the_unresponsive_streak():
    """Each answer, however late, restarts the budget."""

    class _SlowAndFailingClient:
        def __init__(self):
            self.calls = 0

        def health(self, deadline=None):
            self.calls += 1
            if self.calls % 3:
                raise StorageError('database_unavailable', 'no answer')
            time.sleep(0.05)
            return {'ready': True, 'backend': 'tofudb'}

    supervisor = _wired_supervisor(deadline=0.01, unresponsive=0.25)
    child = _FakeChild(exit_code=0, exit_delay=1.5)
    client = _SlowAndFailingClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls >= 9
    assert child.terminate_calls == 0
    assert crashes == [0]


_BLOCKED_READER = """
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
print('ready', flush=True)
os.read(fd, 1)
"""
_SOCKET_READER = """
import socket
left, right = socket.socketpair()
print('ready', flush=True)
left.recv(1)
"""
_SLEEPER = """
import time
print('ready', flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='per-thread syscall evidence is Linux /proc')
def test_kernel_wait_evidence_names_a_thread_inside_a_filesystem_call(tmp_path):
    assert _REAL_ENGINE_KERNEL_WAIT is not None, 'progress evidence reader missing'
    fifo = tmp_path / 'stalled-volume.fifo'
    os.mkfifo(fifo, 0o600)
    children = {
        name: subprocess.Popen([sys.executable, '-c', source, str(fifo)],
                               stdout=subprocess.PIPE, text=True)
        for name, source in (('file', _BLOCKED_READER), ('socket', _SOCKET_READER),
                             ('sleep', _SLEEPER))
    }
    try:
        for child in children.values():
            assert child.stdout.readline().strip() == 'ready'
        evidence = {}
        until = time.monotonic() + 20
        while time.monotonic() < until:
            evidence = {name: _REAL_ENGINE_KERNEL_WAIT(child.pid)
                        for name, child in children.items()}
            if (evidence['file'] and 'stalled-volume.fifo' in evidence['file']
                    and evidence['socket'] is None and evidence['sleep'] is None):
                break
            time.sleep(0.05)
        assert evidence['file'] and 'stalled-volume.fifo' in evidence['file'], evidence
        assert 'read' in evidence['file']
        # A socket receive or a timed sleep is no filesystem wait.
        assert evidence['socket'] is None, evidence
        assert evidence['sleep'] is None, evidence
    finally:
        for child in children.values():
            child.kill()
            child.wait(timeout=10)
    # A process that is gone holds nothing.
    assert _REAL_ENGINE_KERNEL_WAIT(children['file'].pid) is None


# ── Unresponsiveness budget (2026-10-27) ──────────────────────────────────


def test_starved_child_on_a_healthy_host_survives_inside_the_budget():
    """A live child that stops answering for seconds is starved, not dead.

    2026-10-26 23:42 the managed engine failed 25 probes within 61 s and then
    recovered on its own; a three-probe verdict would have killed it.
    """
    supervisor = _wired_supervisor(unresponsive=30.0)
    child = _FakeChild(exit_code=0, exit_delay=0.6)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls > supervisor._liveness_failure_threshold * 3
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [0]


def test_wedged_child_is_recycled_once_the_budget_is_spent(monkeypatch, caplog):
    import logging
    import time

    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor(unresponsive=0.3)
    child = _FakeChild(die_on_eof=False, die_on_term=True, exit_code=-15)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _start_monitor(supervisor, child)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - started >= 0.3
    assert child.terminate_calls == 1
    assert crashes == [-15]
    assert any('unresponsive for' in record.getMessage()
               for record in caplog.records if record.levelname == 'CRITICAL')


def test_liveness_unresponsive_budget_defaults_and_bounds():
    from lib.storage.supervisor import _liveness_unresponsive_budget

    assert _liveness_unresponsive_budget({}) == 120.0
    assert _liveness_unresponsive_budget(
        {'TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS': '300'}) == 300.0
    assert _liveness_unresponsive_budget(
        {'TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS': 'bogus'}) == 120.0
    assert _liveness_unresponsive_budget(
        {'TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS': '1'}) == 10.0
    assert _liveness_unresponsive_budget(
        {'TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS': '99999'}) == 3600.0
