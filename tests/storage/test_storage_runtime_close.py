"""Closing a storage owner ends in-flight and later starts; stopping does not.

A process shutdown discards its storage runtime. Before close existed, a boot
retry that reached the runtime after shutdown attached a new authority with a
fresh startup budget, and the execution host outlived its stop budget
(2026-11-17 22:08). Crash restarts still rely on start-after-stop re-arming.
"""
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor

pytestmark = pytest.mark.unit

# Far longer than the bounds asserted below: an attach that ignored close
# would hold the start thread for this whole budget.
_ATTACH_BUDGET_S = 600.0


def _attaching_supervisor(tmp_path):
    (tmp_path / 'storage').mkdir()
    return StorageSupervisor(connection_file=tmp_path / 'storage/connection.json',
                             startup_timeout=_ATTACH_BUDGET_S)


def _start_in_thread(target):
    outcome = {}

    def run():
        try:
            target()
        except BaseException as exc:
            outcome['error'] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_close_ends_an_in_flight_attach_with_a_non_retryable_refusal(tmp_path):
    supervisor = _attaching_supervisor(tmp_path)
    thread, outcome = _start_in_thread(supervisor.start)
    deadline = time.monotonic() + 30
    while not supervisor._starting and time.monotonic() < deadline:
        time.sleep(.01)
    started = time.monotonic()
    supervisor.close(timeout=5)
    thread.join(30)
    assert not thread.is_alive()
    assert time.monotonic() - started < 30
    assert isinstance(outcome.get('error'), StorageError)
    assert outcome['error'].retryable is False
    with pytest.raises(StorageError) as later:
        supervisor.start()
    assert later.value.retryable is False


def test_closed_runtime_refuses_every_later_start(tmp_path):
    runtime = StorageRuntime(_attaching_supervisor(tmp_path), auto_restart=False)
    runtime.close(timeout=5)
    started = time.monotonic()
    with pytest.raises(StorageError) as refused:
        runtime.start()
    assert refused.value.retryable is False
    assert time.monotonic() - started < 5


def test_stopped_runtime_start_still_rearms_its_supervisor(tmp_path):
    supervisor = _attaching_supervisor(tmp_path)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    runtime.stop(timeout=5)
    thread, outcome = _start_in_thread(runtime.start)
    deadline = time.monotonic() + 30
    while not supervisor._starting and time.monotonic() < deadline:
        time.sleep(.01)
    # Re-armed: the attach is waiting for its connection file, not refused.
    assert supervisor._starting and thread.is_alive() and not outcome
    runtime.close(timeout=5)
    thread.join(30)
    assert not thread.is_alive()
    assert outcome['error'].retryable is False
