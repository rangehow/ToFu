"""Storage shutdown must not certify while an auto-restart can reacquire, and
calls waiting out a transient failure learn that the runtime is stopping."""

from __future__ import annotations

import logging
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime


pytestmark = pytest.mark.unit


class _BlockingRestartSupervisor:
    """Minimal supervisor double for the restart-vs-stop race."""

    ready = False

    def __init__(self) -> None:
        self.callback = None
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.start_entered.set()
        self.release_start.wait(5)
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {
            'ready': self.ready,
            'state': 'ready' if self.ready else 'stopped',
            'backend': 'sqlite',
            'pid': None,
            'port': None,
            'last_exit_code': None,
        }


def test_stop_fails_closed_while_restart_worker_is_still_starting():
    supervisor = _BlockingRestartSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(9)
    assert supervisor.start_entered.wait(1)

    with pytest.raises(StorageError, match='restart worker did not stop'):
        runtime.stop(timeout=0.1)

    supervisor.release_start.set()
    thread = runtime._restart_thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert runtime.ready is False


def test_late_restart_handshake_cannot_publish_ready_after_stop():
    supervisor = _BlockingRestartSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(9)
    assert supervisor.start_entered.wait(1)

    errors = []

    def stop_runtime():
        try:
            runtime.stop(timeout=2)
        except Exception as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    stopper = threading.Thread(target=stop_runtime)
    stopper.start()
    supervisor.release_start.set()
    stopper.join(timeout=3)

    assert not stopper.is_alive()
    assert errors == []
    assert runtime.ready is False
    assert runtime.status()['state'] == 'stopped'


class _RecoveryPortSupervisor:
    """Supervisor double whose client takes the runtime's recovery port at
    start, as the native storage.v2 client does."""

    def __init__(self) -> None:
        self.callback = None
        self.ready = False
        self.client = self
        self.recover = None

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def bind_runtime_recovery(self, recover) -> None:
        self.recover = recover

    def start(self):
        self.ready = True
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {
            'ready': self.ready,
            'state': 'ready' if self.ready else 'stopped',
            'backend': 'tofudb',
            'pid': None,
            'port': None,
            'last_exit_code': None,
        }


# Outlasts the test: only a runtime state change can end a caller's pause.
_PAUSE_LONGER_THAN_THE_TEST_S = 600.0


def _stop_with_callers_mid_retry(runtime, recover, count):
    """Stop ``runtime`` while ``count`` callers pause on its recovery port,
    as the client's retry loop does between attempts; return their outcomes."""
    outcomes = []

    def wait_out_a_transient_failure():
        try:
            outcomes.append(recover(_PAUSE_LONGER_THAN_THE_TEST_S))
        except StorageError as error:
            outcomes.append(error)

    callers = [threading.Thread(target=wait_out_a_transient_failure, daemon=True)
               for _ in range(count)]
    for caller in callers:
        caller.start()
    # Let them park. A caller arriving after the stop gets the same answer.
    threading.Event().wait(0.2)
    assert all(caller.is_alive() for caller in callers)
    runtime.stop(timeout=5.0)
    for caller in callers:
        caller.join(timeout=5.0)
    assert not any(caller.is_alive() for caller in callers)
    return outcomes


def test_stop_tells_a_caller_mid_retry_that_the_runtime_is_stopping():
    """A stop must not read like a breaker trip to a call waiting it out.

    r4b-n2000-1 (2026-11-14): the harness stopped the app at its tier timeout,
    and every call then pausing between replays failed with the one text a
    breaker trip also produced, so the stop read as a breaker trip. The stop
    names itself and stays final.
    """
    supervisor = _RecoveryPortSupervisor()
    runtime = StorageRuntime(supervisor)
    assert runtime.start() is supervisor

    [error] = _stop_with_callers_mid_retry(runtime, supervisor.recover, 1)

    assert isinstance(error, StorageError)
    assert error.code == 'database_unavailable'
    assert error.message == 'Storage runtime is stopping'
    assert error.retryable is False


def test_each_stop_logs_once_that_it_ended_calls_mid_retry(caplog):
    """One warning per stop names the state, however many calls it ends."""
    supervisor = _RecoveryPortSupervisor()
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        for callers in (3, 1):  # two stop episodes
            assert runtime.start() is supervisor
            outcomes = _stop_with_callers_mid_retry(
                runtime, supervisor.recover, callers)
            assert [error.code for error in outcomes] == [
                'database_unavailable'] * callers

    lines = [record.getMessage() for record in caplog.records
             if record.name == 'tofu.storage.runtime'
             and 'mid-retry' in record.getMessage()]
    assert len(lines) == 2, lines
    for line in lines:
        assert 'stopping' in line
        assert 'degraded' not in line
