"""An event-loop submitter never waits for a carrier's bootstrap.

2026-11-13 API-host stalls (/tmp/fd1_stalls, 3 of 7 episodes): MainThread sat
in threading.py:999 ``_started.wait()`` via ``_start_reserved_workers`` <-
``submit_task`` <- ``asyncio.to_thread`` while a carrier held the GIL.
``Thread.start()`` returns only once the new thread has run, which needs the
GIL, so a serving loop that starts carriers waits for the busiest thread.
"""

from __future__ import annotations

import _thread
import asyncio
import threading
import time

import pytest

from tofu_workers.executor import DemandExecutor


pytestmark = pytest.mark.unit


def _executor(prefix: str) -> DemandExecutor:
    return DemandExecutor(max_workers=1, queue_capacity=0,
                          max_abandoned_workers=1, thread_name_prefix=prefix)


@pytest.fixture()
def creations(monkeypatch):
    """Every thread creation: (kind, creating thread ident, created name)."""
    seen = []
    start = threading.Thread.start
    raw = _thread.start_new_thread

    def recorded_start(thread):
        seen.append(("start", threading.get_ident(), thread.name))
        return start(thread)

    def recorded_raw(function, args, kwargs=None):
        seen.append(("raw", threading.get_ident(), ""))
        return raw(function, args, kwargs or {})

    monkeypatch.setattr(threading.Thread, "start", recorded_start)
    monkeypatch.setattr(_thread, "start_new_thread", recorded_raw)
    return seen


def _concurrent_batch(executors, jobs_per_executor: int):
    """Run blocking jobs that each need their own carrier, from one loop."""
    release = threading.Event()
    running = []

    def job(index):
        running.append(index)
        release.wait(10)
        return index

    async def main():
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(executor, job, index)
                   for executor in executors for index in range(jobs_per_executor)]
        deadline = time.monotonic() + 10
        while len(running) < len(futures) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        release.set()
        return threading.get_ident(), await asyncio.gather(*futures)

    try:
        return asyncio.run(main())
    finally:
        release.set()


def test_loop_submissions_create_no_thread_on_the_loop(creations):
    first, second = _executor("loop-submit-a"), _executor("loop-submit-b")
    try:
        loop_ident, results = _concurrent_batch([first, second], 6)
        warm = len(creations)
        later_loop, later = _concurrent_batch([first, second], 6)
    finally:
        first.shutdown(wait=True)
        second.shutdown(wait=True)

    assert results == later == list(range(6)) * 2
    carriers = [entry for entry in creations
                if entry[0] == "start" and entry[2].startswith("loop-submit")]
    # Demand still starts a carrier for every concurrently blocked job.
    assert len(carriers) >= 12
    # The loop never calls Thread.start, hence never waits on _started.
    assert not [entry for entry in creations
                if entry[0] == "start" and entry[1] in {loop_ident, later_loop}]
    # At most the process's one resident starter is created from a loop,
    # once; afterwards a loop submission creates no thread at all.
    assert len([entry for entry in creations[:warm]
                if entry[0] == "raw" and entry[1] == loop_ident]) <= 1
    assert not [entry for entry in creations[warm:] if entry[1] == later_loop]
    # One resident starter serves every executor.
    assert len({entry[1] for entry in carriers}) == 1


def test_loop_start_failure_reaches_the_awaiting_coroutine(monkeypatch):
    executor = _executor("loop-refusal")
    start = threading.Thread.start

    def failing_start(thread):
        if thread.name.startswith("loop-refusal"):
            raise RuntimeError("can't start new thread")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    async def main():
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="can't start new thread") as refused:
            await loop.run_in_executor(executor, lambda: 1)
        return refused.value

    try:
        refusal = asyncio.run(main())
        snapshot = executor.scheduling_snapshot()
    finally:
        executor.shutdown(wait=True)
    # The one startup recovery ran; its first cause is retained.
    assert isinstance(refusal.__cause__, RuntimeError)
    assert (snapshot["queued"], snapshot["active"], snapshot["startingWorkers"]) == (0, 0, 0)


def test_closed_executor_refuses_loop_submissions_synchronously():
    executor = _executor("loop-closed")
    executor.shutdown(wait=True)

    async def main():
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="after shutdown"):
            loop.run_in_executor(executor, lambda: 1)

    asyncio.run(main())


def test_cancelling_a_loop_submission_before_its_carrier_starts_withdraws_it(monkeypatch):
    executor = _executor("loop-cancel")
    entered, gate = threading.Event(), threading.Event()
    start_reserved = DemandExecutor._start_reserved_workers

    def gated(self, threads, admit=None):
        if admit is not None:
            entered.set()
            gate.wait(2)
        return start_reserved(self, threads, admit=admit)

    monkeypatch.setattr(DemandExecutor, "_start_reserved_workers", gated)
    ran = []

    async def main():
        future = executor.submit(ran.append, "ran")
        assert entered.wait(5)
        cancelled = future.cancel()
        gate.set()
        return cancelled

    try:
        cancelled = asyncio.run(main())
    finally:
        gate.set()
        executor.shutdown(wait=True)
    assert cancelled is True
    assert ran == []
    assert executor.scheduling_snapshot()["queued"] == 0


def test_off_loop_submitters_still_start_their_own_carriers(creations):
    executor = _executor("off-loop")
    try:
        assert executor.submit(lambda: 1).result(10) == 1
    finally:
        executor.shutdown(wait=True)
    carriers = [entry for entry in creations
                if entry[0] == "start" and entry[2].startswith("off-loop")]
    assert carriers and {entry[1] for entry in carriers} == {threading.get_ident()}
