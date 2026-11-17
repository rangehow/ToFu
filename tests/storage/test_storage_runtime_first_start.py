"""A resident owner's first storage start retries under the crash budget.

2026-11-17 21:49: the first health preflight after a new engine deploy was not
ready once; the native storage host then reported "failed; explicit restart
required" for 17 minutes. A failed first start is one failed-start event that
the restart worker retries, exactly like a crash after readiness.
"""
from __future__ import annotations

import threading
import time

import pytest

from lib.storage.runtime import StorageRuntime

pytestmark = pytest.mark.unit


class _FlakyFirstStartSupervisor:
    """Fails its first ``failures`` starts the way the 21:49 preflight did."""

    def __init__(self, failures: int) -> None:
        self.failures, self.starts, self.ready = failures, 0, False
        self.lock = threading.Lock()

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        with self.lock:
            self.starts += 1
            if self.starts <= self.failures:
                raise RuntimeError('sidecar health preflight did not pass '
                                   '(probe_error=unclassified; unhealthy=maintenance)')
            self.ready = True
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {'ready': self.ready, 'state': 'ready' if self.ready else 'stopped',
                'backend': 'tofudb', 'pid': None, 'port': None, 'last_exit_code': None}


def _until_ready(runtime: StorageRuntime, seconds: float = 10.0) -> None:
    stop = time.monotonic() + seconds
    while not runtime.ready:
        assert time.monotonic() < stop, runtime.status()
        time.sleep(0.02)


def test_a_failed_first_start_is_retried_instead_of_failing_until_an_explicit_restart():
    supervisor = _FlakyFirstStartSupervisor(failures=1)
    runtime = StorageRuntime(supervisor)
    try:
        runtime.start_supervised()
        status = runtime.status()
        assert status['state'] in ('restarting', 'ready') and not status['degraded']
        _until_ready(runtime)
        assert supervisor.starts == 2
    finally:
        runtime.stop(timeout=5)


def test_failed_first_starts_count_against_the_same_breaker_as_crashes(monkeypatch):
    supervisor = _FlakyFirstStartSupervisor(failures=10_000)
    runtime = StorageRuntime(supervisor)
    monkeypatch.setattr(runtime, '_crash_limit', 0)
    try:
        runtime.start_supervised()
        assert runtime.status()['degraded'] is True
    finally:
        runtime.stop(timeout=5)


def test_a_runtime_without_automatic_restart_still_raises():
    supervisor = _FlakyFirstStartSupervisor(failures=1)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    with pytest.raises(RuntimeError, match='health preflight'):
        runtime.start_supervised()
