"""Restart-loop circuit breaker: crash storms enter degraded and retry slowly.

A single crash restarts fast; a storm of crashes or failed starts inside the
sliding window (TOFU_STORAGE_RESTART_CRASH_LIMIT per
TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS) means the authority cannot
currently stay up. The runtime then stays fenced, surfaces ``degraded`` in
status(), logs CRITICAL once plus a periodic reminder, notifies the trip
hook once, and retries the start on a slow backoff (first after
_RESTART_MAX_DELAY_SECONDS, doubling to one attempt per breaker window)
until a start succeeds — never a silent backend switch.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
import lib.storage.runtime as runtime_mod


pytestmark = pytest.mark.unit


class _ScriptedSupervisor:
    """Supervisor double: start() follows a fail/succeed script."""

    def __init__(self, *, always_fail=False):
        self.callback = None
        self.always_fail = always_fail
        self.starts = 0
        self.ready = False
        self.client = self

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.starts += 1
        if self.always_fail:
            raise RuntimeError('spawn refused')
        self.ready = True
        self.client = self
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


@pytest.fixture
def _breaker_env(monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '3')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '600')
    yield


def test_single_crash_restarts_quickly(_breaker_env):
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)
    thread = runtime._restart_thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert supervisor.starts == 1  # fast first restart, no breaker delay
    assert runtime.ready
    status = runtime.status()
    assert status['state'] == 'ready'
    assert status['degraded'] is False
    runtime.stop(timeout=5.0)


def test_crash_storm_trips_degraded_and_stops_spawning(_breaker_env, caplog):
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        for _ in range(4):  # 4 crashes > limit 3 → degraded
            runtime._crashed(-9)
    thread = runtime._restart_thread
    if thread is not None:
        thread.join(timeout=10)

    status = runtime.status()
    assert status['state'] == 'degraded'
    assert status['degraded'] is True
    assert runtime.ready is False
    # The breaker tripped before any spawn: one crash scheduled a restart,
    # but the storm outran it and degraded state forbids further spawns.
    assert supervisor.starts <= 1
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1  # CRITICAL exactly once at entry
    assert 'circuit breaker tripped' in criticals[0].getMessage()
    assert 'process restart' in criticals[0].getMessage()
    # Reads and writes stay fenced with the typed availability error.
    with pytest.raises(StorageError) as excinfo:
        runtime.client(write=True)
    assert excinfo.value.code == 'database_unavailable'
    # Explicit start attempts fail closed too — no un-fence surface exists.
    with pytest.raises(StorageError):
        runtime.start()
    runtime.stop(timeout=5.0)


def _wait(predicate, timeout=30.0):
    deadline = runtime_mod.time.monotonic() + timeout
    while runtime_mod.time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return False


def test_failed_start_storm_also_trips_the_breaker(_breaker_env, caplog):
    supervisor = _ScriptedSupervisor(always_fail=True)
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        runtime._crashed(-9)  # one crash; the failed attempts do the rest
        assert _wait(lambda: runtime.status()['degraded'])

    status = runtime.status()
    assert status['state'] == 'degraded'
    assert status['degraded'] is True
    starts_at_verdict = supervisor.starts
    assert starts_at_verdict >= 3  # the failed attempts fed the window
    # The worker stays for the post-trip retries, which wait the ordinary
    # ceiling first: no spawn follows the verdict at once.
    assert runtime._restart_thread is not None and runtime._restart_thread.is_alive()
    threading.Event().wait(0.5)
    assert supervisor.starts == starts_at_verdict
    runtime.stop(timeout=5.0)
    assert not runtime._restart_thread or not runtime._restart_thread.is_alive()


def test_degraded_reminder_heartbeats_until_stop(_breaker_env, monkeypatch,
                                                 caplog):
    monkeypatch.setattr(runtime_mod, '_DEGRADED_REMINDER_INTERVAL_S', 0.05)
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        for _ in range(4):
            runtime._crashed(-9)
        thread = runtime._restart_thread
        if thread is not None:
            thread.join(timeout=10)
        assert runtime.status()['degraded'] is True
        reminder = runtime._degraded_reminder_thread
        assert reminder is not None
        # Wait for at least one heartbeat reminder.
        deadline = threading.Event()
        deadline.wait(0.4)
    reminders = [r for r in caplog.records
                 if 'remains degraded' in r.getMessage()]
    assert reminders, [r.getMessage() for r in caplog.records]
    runtime.stop(timeout=5.0)
    reminder.join(timeout=5)
    assert not reminder.is_alive()


def test_client_waits_out_bounded_restart(_breaker_env):
    """A caller arriving mid-restart rides the window instead of 503ing.

    The 2026-09-24 incident: every in-flight request failed fast with
    database_unavailable during the ~10 s sidecar recycle even though the
    caller RPC deadline (20 s) would have carried it past the restart. The
    fence now parks the caller on readiness while a restart worker is
    alive, and the successful handshake releases it to a ready client.
    """
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)  # schedules the restart worker immediately
    result: dict[str, object] = {}

    def _blocked_caller():
        try:
            result['client'] = runtime.client(write=True)
        except StorageError as exc:
            result['error'] = exc

    caller = threading.Thread(target=_blocked_caller, daemon=True)
    caller.start()
    caller.join(timeout=10)
    assert not caller.is_alive()
    assert 'error' not in result, result.get('error')
    assert result.get('client') is supervisor
    assert runtime.ready
    runtime.stop(timeout=5.0)


def test_client_waits_for_an_exit_its_monitor_has_not_reported_yet(_breaker_env):
    """A lost acknowledgement replays on the restarted authority.

    The child can be gone before its monitor's next poll runs the crash
    callbacks. Until then the runtime still holds readiness and no restart
    worker exists, and the replay of a command whose acknowledgement died
    with the child was refused as if the authority were proven down: the
    caller lost an outcome that may have committed
    (tests/tofudb/test_native_lost_ack_recovery.py). The unreported exit is
    a restart in flight; the caller waits it out and gets the new client.
    """
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime.start()
    supervisor.ready = False  # the child exited; its monitor has not polled yet
    monitor = threading.Timer(0.2, runtime._crashed, args=(-9,))
    monitor.start()
    try:
        client = runtime.client(write=True)
    finally:
        monitor.join()
    assert client is supervisor
    assert runtime.ready
    assert supervisor.starts == 2
    runtime.stop(timeout=5.0)


def test_readiness_is_fenced_together_with_its_restart(_breaker_env):
    """No caller sees a fenced runtime without the worker that restores it.

    The crash path fenced readiness, ran the write-fence callback and logged
    before it started the restart worker, so a caller arriving in between
    was refused as if no restart were coming.
    """
    observed = []
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(
        supervisor, on_write_fence=lambda: observed.append(runtime.status()))
    runtime.start()
    supervisor.ready = False
    runtime._crashed(-9)
    assert [(status['ready'], status['restarting']) for status in observed] == [
        (False, True)]
    runtime._restart_thread.join(timeout=10)
    assert runtime.ready
    runtime.stop(timeout=5.0)


def test_client_fails_fast_when_no_restart_in_flight(_breaker_env):
    """Not-ready without a live restart worker keeps fail-fast semantics.

    Degraded/stopped/never-started states must never park a request thread:
    waiting there hides a real outage behind handler latency. Only the
    bounded restart window waits.
    """
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    # Never started, no restart scheduled: the fence raises immediately.
    started = runtime_mod.time.monotonic()
    with pytest.raises(StorageError) as excinfo:
        runtime.client(write=True)
    assert excinfo.value.code == 'database_unavailable'
    assert runtime_mod.time.monotonic() - started < 1.0
    runtime.stop(timeout=5.0)


def test_client_wait_releases_on_degraded_verdict(_breaker_env):
    """A parked caller wakes on the breaker verdict, not the poll cap.

    If the restart storm trips degraded mid-wait the caller must get the
    classified fence error promptly rather than sleeping the whole wait
    cap behind a terminal state that will never become ready.
    """
    supervisor = _ScriptedSupervisor(always_fail=True)
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)  # failed starts feed the breaker storm
    result: dict[str, object] = {}

    def _blocked_caller():
        try:
            runtime.client(write=True)
        except StorageError as exc:
            result['error'] = exc

    caller = threading.Thread(target=_blocked_caller, daemon=True)
    started = runtime_mod.time.monotonic()
    caller.start()
    caller.join(timeout=30)
    assert not caller.is_alive()
    assert isinstance(result.get('error'), StorageError)
    assert result['error'].code == 'database_unavailable'
    # Well under the 30 s fence-wait cap: the degraded notify released it.
    assert runtime_mod.time.monotonic() - started < 25.0
    runtime.stop(timeout=5.0)


def test_degraded_client_error_names_the_recovery(_breaker_env):
    """A degraded runtime must not read as transient unavailability.

    The 2026-10-22 incident: the breaker latched degraded, the authority
    stayed fenced for the rest of the process lifetime, and every request
    reported a generic "sidecar is unavailable" — the operator kept retrying
    instead of restarting the process. The error text now names the
    terminal state and the recovery action.
    """
    supervisor = _ScriptedSupervisor(always_fail=True)
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)
    assert _wait(lambda: runtime.status()['degraded'])

    for write in (False, True):
        with pytest.raises(StorageError) as excinfo:
            runtime.client(write=write)
        error = excinfo.value
        assert error.code == 'database_unavailable'
        assert 'degraded' in str(error)
        assert 'restart' in str(error)
        # The degraded text carries no host path (the cause stays in logs).
        assert 'tofu-db' not in str(error)
    runtime.stop(timeout=5.0)


class _GatedSupervisor(_ScriptedSupervisor):
    """start() holds until the test opens the gate, so a fence can be observed."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def start(self):
        self.gate.wait(10)
        return super().start()


def test_fence_episode_is_announced_once_off_the_restart_thread(_breaker_env):
    """A fence reports since when it holds, then hands its episode on once.

    2026-11-11: storage was fenced for 92 minutes and nothing outside the logs
    knew since when or why. The runtime keeps the first fence start across
    further crashes, exposes it in status(), and announces the closed episode
    on its own thread: a reporter that blocks the restart worker would keep
    the restart slot occupied and a later crash would schedule no restart.
    """
    supervisor = _GatedSupervisor()
    runtime = StorageRuntime(supervisor)
    episodes, threads = [], []
    announced = threading.Event()

    def on_recovered(episode):
        episodes.append(episode)
        threads.append(threading.current_thread().name)
        announced.set()

    runtime.add_fence_recovery_callback(on_recovered)
    runtime.add_fence_recovery_callback(on_recovered)
    before = runtime_mod.time.time()
    runtime._crashed(-1)
    fenced = runtime.status()
    runtime._crashed(-1)
    assert fenced['ready'] is False
    assert fenced['unavailable_since'] >= before
    assert runtime.status()['unavailable_since'] == fenced['unavailable_since']

    supervisor.gate.set()
    assert announced.wait(10)
    threading.Event().wait(0.2)
    [episode] = episodes
    assert episode['since'] == fenced['unavailable_since']
    assert episode['until'] >= episode['since']
    assert episode['cause'] == 'sidecar exited unexpectedly (-1)'
    assert threads == ['storage-fence-recovered']
    assert runtime.ready
    assert runtime.status()['unavailable_since'] is None
    runtime.stop(timeout=5.0)


def test_breaker_knob_parsing_and_clamps(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS',
                       raising=False)
    assert runtime_mod._restart_breaker_config() == (600.0, 5)
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '0')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '10')
    window, limit = runtime_mod._restart_breaker_config()
    assert (window, limit) == (60.0, 1)  # clamped into hard bounds
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', 'bogus')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', 'bogus')
    assert runtime_mod._restart_breaker_config() == (600.0, 5)


class _RecoveringSupervisor(_ScriptedSupervisor):
    """Fails ``failures`` starts, then succeeds (the lease freed after the trip)."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def start(self):
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError('Another storage sidecar holds the project lease')
        self.ready = True
        return self


def test_a_tripped_breaker_retries_once_after_the_ceiling_and_recovers(_breaker_env, monkeypatch, caplog):
    """2026-11-11/12: lease-held storms latched storage for 92 min and 2 h 22 min
    although the lease was free within a minute; a post-trip retry heals it."""
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.2)
    trips = []
    supervisor = _RecoveringSupervisor(failures=4)  # 4 failed starts > limit 3 trips
    runtime = StorageRuntime(supervisor, on_degraded=trips.append)
    with caplog.at_level(logging.INFO, logger='tofu.storage.runtime'):
        runtime._crashed(-9)
        assert _wait(lambda: runtime.status()['degraded'])
        assert _wait(lambda: runtime.ready)
    status = runtime.status()
    assert status['state'] == 'ready' and status['degraded'] is False
    assert supervisor.starts == 5  # four failures, then one post-trip attempt
    assert len(trips) == 1 and trips[0].startswith('4 storage crashes/failed starts within 600s')
    messages = [record.getMessage() for record in caplog.records]
    assert any('after the breaker trip' in message for message in messages)
    assert any('circuit breaker cleared' in message for message in messages)
    assert runtime.client(write=True) is supervisor
    runtime.stop(timeout=5.0)


def test_a_crash_storm_trip_starts_the_post_trip_worker(_breaker_env, monkeypatch):
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.2)
    trips = []
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor, on_degraded=trips.append)
    for _ in range(4):  # the storm trips from the crash callback
        runtime._crashed(-9)
    assert _wait(lambda: runtime.ready)
    assert runtime.status()['degraded'] is False and len(trips) == 1
    runtime.stop(timeout=5.0)


def test_post_trip_backoff_doubles_to_one_attempt_per_breaker_window(_breaker_env):
    runtime = StorageRuntime(_ScriptedSupervisor())
    delays, delay = [], runtime_mod._RESTART_MAX_DELAY_SECONDS
    for _ in range(6):
        delay = runtime._next_restart_delay(delay, after_trip=True)
        delays.append(delay)
    assert delays == [120.0, 240.0, 480.0, 600.0, 600.0, 600.0]
    assert runtime._next_restart_delay(0.1, after_trip=True) == runtime_mod._RESTART_MAX_DELAY_SECONDS
    assert runtime._next_restart_delay(40.0, after_trip=False) == runtime_mod._RESTART_MAX_DELAY_SECONDS


def test_a_failing_trip_hook_never_breaks_the_breaker(_breaker_env, caplog):
    def broken(reason):
        raise OSError('no process slots')
    runtime = StorageRuntime(_ScriptedSupervisor(), on_degraded=broken)
    with caplog.at_level(logging.ERROR, logger='tofu.storage.runtime'):
        runtime._enter_degraded('storm')
    assert runtime.status()['degraded'] is True
    assert any('breaker-trip notification failed' in record.getMessage() for record in caplog.records)
    runtime.stop(timeout=5.0)
