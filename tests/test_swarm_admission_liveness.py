#!/usr/bin/env python3
"""Admission backpressure must read as ALIVE, and aborts must not retry.

Two production failure classes pinned together:

1. A wave parked at admission (the rate-limiter semaphore or the process-wide
   owner-fair execution gate) produced zero LLM rounds and zero tool calls —
   the liveness beacon went stale and the whole wave was stall-aborted at the
   900 s wall while it was merely QUEUED behind other owners. The wait loops
   now heartbeat the bound progress sink, so a queued agent's
   ``seconds_since_activity`` stays near zero no matter how long the queue is.

2. An ``AbortedError`` raised from that admission wait (the sticky abort flag)
   used to fall into the generic ``except Exception`` branch: the scheduler
   logged a spurious AGENT_RETRY, retried into the same gate, and settled the
   agent FAILED. Cancellation is terminal — it must settle CANCELLED on the
   first attempt and never invoke ``on_retry``.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _spec(sid, **kw):
    from lib.swarm.protocol import SubTaskSpec
    kw.setdefault('role', 'coder')
    kw.setdefault('objective', 'do a thing')
    return SubTaskSpec(id=sid, **kw)


class _FakeAgent:
    model = 'test-model'

    def __init__(self, spec, run_impl):
        self.spec = spec
        self.agent_id = spec.id
        self._run_impl = run_impl

    def run(self):
        return self._run_impl(self)


# ── 1. gate queue wait heartbeats the bound sink ───────────────

def test_gate_wait_heartbeats_bound_sink():
    from lib.swarm.execution_gate import OwnerFairExecutionGate
    from lib.swarm.liveness import ProgressBeacon, thread_progress_sink

    gate = OwnerFairExecutionGate(capacity=1, waiter_capacity=4)
    beacon = ProgressBeacon(stall_timeout=30.0)
    gate.acquire('owner-a')  # hold the only permit

    acquired = threading.Event()
    acquire_error = []

    def queued_agent():
        try:
            with thread_progress_sink(beacon, 'agent-b'):
                gate.acquire('owner-b', abort_check=lambda: False)
            acquired.set()
        except Exception as exc:  # pragma: no cover - diagnostic path
            acquire_error.append(exc)

    t = threading.Thread(target=queued_agent, daemon=True)
    t.start()
    try:
        # Parked behind owner-a for well over the heartbeat interval.
        time.sleep(1.2)
        assert not acquired.is_set(), 'agent-b admitted while gate was full'
        silence = beacon.seconds_since_activity('agent-b')
        assert silence < 1.0, (
            f'queued agent went silent for {silence:.2f}s at the gate')
        assert beacon.is_making_progress('agent-b')
        assert beacon.is_making_progress()  # swarm-level verdict
    finally:
        gate.release('owner-a')
        t.join(timeout=5)
    assert acquired.is_set(), 'agent-b never admitted after release'
    assert not acquire_error
    gate.release('owner-b')


# ── 2. AbortedError from admission settles CANCELLED, no retry ──

def test_aborted_error_maps_to_cancelled_without_retry():
    from lib.llm_errors import AbortedError
    from lib.swarm.protocol import SubAgentStatus
    from lib.swarm.scheduler import StreamingScheduler

    class _AbortingLimiter:
        def run_agent(self, agent, *, abort_check=None):
            raise AbortedError('swarm execution cancelled before admission')

    retries = []
    sched = StreamingScheduler(
        agent_factory=lambda s: _FakeAgent(s, lambda a: None),
        rate_limiter=_AbortingLimiter(),
        max_parallel=2,
        default_retries=2,
        on_retry=lambda spec, attempt, err: retries.append((spec.id, attempt)),
    )
    try:
        sched.add_specs([_spec('a1', max_retries=2)])
        settled = sched.run_until_idle(timeout=30)
    finally:
        sched.shutdown(wait=True)
    assert len(settled) == 1
    _, result = settled[0]
    assert result.status == SubAgentStatus.CANCELLED.value, (
        f'abort settled as {result.status!r}, not CANCELLED')
    assert 'AbortedError' in (result.error_message or '')
    assert retries == [], f'cancellation was retried: {retries}'


# ── 3. rate-limiter admission wait keeps the beacon fresh ──────

def test_rate_limiter_admission_wait_keeps_beacon_fresh():
    from lib.swarm.execution_gate import OwnerFairExecutionGate
    from lib.swarm.liveness import ProgressBeacon
    from lib.swarm.protocol import SubAgentResult, SubAgentStatus
    from lib.swarm.rate_limiter import RateLimiter
    from lib.swarm.scheduler import StreamingScheduler

    beacon = ProgressBeacon(stall_timeout=30.0)
    limiter = RateLimiter(
        max_concurrent=1,
        owner_key='test-owner',
        execution_gate=OwnerFairExecutionGate(capacity=2, waiter_capacity=8),
    )
    limiter.acquire()  # main thread holds the only slot

    def quick_run(agent):
        return SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                              final_answer='done')

    sched = StreamingScheduler(
        agent_factory=lambda s: _FakeAgent(s, quick_run),
        rate_limiter=limiter,
        max_parallel=2,
        progress_beacon=beacon,
    )
    runner = threading.Thread(
        target=lambda: sched.run_until_idle(timeout=30), daemon=True)
    try:
        sched.add_specs([_spec('queued-1')])
        runner.start()
        # The pool thread is parked in the limiter's semaphore loop; without
        # the admission heartbeat the beacon would show ~1.4 s of silence.
        time.sleep(1.4)
        silence = beacon.seconds_since_activity('queued-1')
        assert silence < 1.0, (
            f'agent queued at the rate limiter went silent for '
            f'{silence:.2f}s')
    finally:
        limiter.release()
        runner.join(timeout=10)
        sched.shutdown(wait=True)
    assert not runner.is_alive(), 'queued agent never admitted after release'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
