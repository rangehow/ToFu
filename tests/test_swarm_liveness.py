"""tests/test_swarm_liveness.py — guards for the swarm's THREE independent clocks.

Measured incident (conv ms8c68l0ppwfcw, 2026-07-31) — three separate deadlines,
none of which asked whether work was being produced:

  1. ``master._start_driver`` called ``iter_completions()`` with no argument and
     inherited ``scheduler.iter_completions(timeout=600.0)``. At exactly 600.0s
     the driver abandoned two RUNNING agents, set ``_terminated``, shut the pool
     down and persisted ``settled:true``. ``await_agents`` then told the model
     the ids "will NEVER complete" — ``orphans`` delivered 21 minutes later
     (``elapsed=2023.1s``, 1,091,830 tokens). The abandoned agents rendered as
     "无结果" because ``_build_agent_snapshot`` coerces resultless agents to
     ``unknown`` once ``terminated`` is set. 56 historical occurrences.

  2. ``SubTaskSpec.timeout_seconds=1800`` is evaluated only in ``before_round``,
     so an agent blocked INSIDE a tool never reaches the check. ``coder-tests``
     sat in a ``pytest`` child for over an hour and never tripped it.

  3. ``_session_timestamps[key]`` is written once, in ``_set_session`` at spawn,
     and never refreshed; ``_cleanup_stale_sessions`` calls ``session.abort()``
     at TTL. Its ``_key_is_live`` shield only looks for a non-terminal CHAT
     task, which is precisely what a fire-and-forget swarm does not have.
     105 historical occurrences.

The fix is one shared liveness fact (``lib/swarm/liveness.ProgressBeacon``) that
all three consult, so none of them can independently decide a busy agent is
finished. These tests are BEHAVIOURAL — they drive the real scheduler/master
with fake agents rather than asserting on source text, because the defect being
guarded is "a deadline fires while work is in flight", which only a running
loop can demonstrate.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.swarm.liveness import DEFAULT_STALL_TIMEOUT_SEC, ProgressBeacon  # noqa: E402
from lib.swarm.protocol import SubAgentResult, SubAgentStatus, SubTaskSpec  # noqa: E402
from lib.swarm.scheduler import StreamingScheduler  # noqa: E402

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════

class _FakeAgent:
    """Agent that stays busy until released, touching the beacon as it works.

    Models the real shape of the incident: an agent that is PRODUCING (so it
    must never be killed) but takes longer than any fixed budget.
    """

    def __init__(self, spec, beacon=None, gate=None, touch_interval=0.01):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self._beacon = beacon
        self._gate = gate or threading.Event()
        self._touch_interval = touch_interval
        self.started = threading.Event()

    def run(self):
        self.started.set()
        while not self._gate.is_set():
            if self._beacon is not None:
                self._beacon.touch(self.spec.id, 'tool_output')
            time.sleep(self._touch_interval)
        return SubAgentResult(
            status=SubAgentStatus.COMPLETED.value,
            final_answer='done for real',
            elapsed_seconds=1.0,
        )


class _SilentAgent:
    """Agent that never touches the beacon — the genuinely wedged shape."""

    def __init__(self, spec, gate=None):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self._gate = gate or threading.Event()
        self.started = threading.Event()

    def run(self):
        self.started.set()
        self._gate.wait(timeout=30)
        return SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                              final_answer='late')


def _spec(sid, role='coder'):
    return SubTaskSpec(id=sid, role=role, objective=f'objective for {sid}')


def _await_started(agents: dict, key: str, timeout: float = 20.0):
    """Wait for the pool thread to CONSTRUCT and start agent *key*.

    ``add_specs`` only submits to a ThreadPoolExecutor, so under load the
    worker may not have called the factory yet and ``agents[key]`` legitimately
    does not exist. Asserting on it directly raised ``KeyError`` in a full,
    CPU-saturated suite run while passing in isolation. Poll for the entry
    first, then wait on its event — a harness-timing fix that does not weaken
    what the test asserts.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = agents.get(key)
        if a is not None and a.started.wait(timeout=0.05):
            return a
        time.sleep(0.01)
    raise AssertionError(f'agent {key!r} never started within {timeout}s')


# ═══════════════════════════════════════════════════════
#  1. The beacon itself
# ═══════════════════════════════════════════════════════

def test_beacon_reports_progress_while_touched():
    b = ProgressBeacon(stall_timeout=0.3)
    b.touch('a1', 'round_start')
    assert b.is_making_progress('a1')
    assert b.is_making_progress()


def test_beacon_detects_a_silent_agent():
    b = ProgressBeacon(stall_timeout=0.05)
    b.touch('a1')
    time.sleep(0.12)
    assert not b.is_making_progress('a1'), \
        'an agent that stopped emitting must be reported as stalled'
    stalled = b.stalled_agents()
    assert [s[0] for s in stalled] == ['a1']


def test_one_busy_agent_keeps_the_swarm_alive():
    """Whole-swarm liveness = MOST RECENT activity across agents.

    The 600s driver budget killed everyone at once; a swarm must stay alive
    while ANY agent is still producing.
    """
    b = ProgressBeacon(stall_timeout=0.2)
    b.touch('quiet')
    time.sleep(0.25)
    b.touch('busy')
    assert b.is_making_progress(), 'swarm-wide liveness must follow the busiest agent'
    assert not b.is_making_progress('quiet')
    assert b.is_making_progress('busy')


def test_untracked_agent_is_not_reported_stalled():
    """Unknown id → assume alive. The default must never destroy work."""
    b = ProgressBeacon(stall_timeout=0.01)
    assert b.is_making_progress('never-started')
    assert b.seconds_since_activity('never-started') == 0.0


def test_settled_agent_is_forgotten_and_cannot_hold_the_swarm_open():
    b = ProgressBeacon(stall_timeout=0.05)
    b.touch('a1')
    b.forget('a1')
    assert b.tracked_agents() == []
    time.sleep(0.1)
    assert b.is_making_progress(), 'no tracked agents → nothing is stalling'


def test_default_stall_timeout_is_not_a_runtime_budget():
    """The constant bounds SILENCE, not total runtime.

    Pinned because the whole regression was budgets masquerading as health
    checks: any value near a plausible total runtime (600/1800) would let this
    module re-become the thing it replaced.
    """
    assert DEFAULT_STALL_TIMEOUT_SEC >= 600, \
        'too tight — a slow-but-working agent would be reaped'
    assert DEFAULT_STALL_TIMEOUT_SEC != 1800, \
        'must not silently re-adopt the old wall-clock budget'


# ═══════════════════════════════════════════════════════
#  2. Driver: no fixed whole-swarm budget  (defect #1)
# ═══════════════════════════════════════════════════════

def test_scheduler_iter_completions_defaults_to_no_fixed_budget():
    """``iter_completions()`` must not carry a finite default deadline.

    ``master._start_driver`` calls it with NO arguments, so whatever default
    lives here IS the whole-swarm budget. It was 600.0 — the exact value in
    ``Driver done — elapsed=600.0s``.
    """
    import inspect
    sig = inspect.signature(StreamingScheduler.iter_completions)
    default = sig.parameters['timeout'].default
    assert default is None or default == float('inf') or default <= 0, (
        f'iter_completions still defaults to a finite {default}s whole-swarm '
        'budget — a second-wave agent would inherit a deadline fixed before '
        'it was even spawned')


def test_driver_does_not_abandon_a_working_agent():
    """END-TO-END reproduction of the reported bug.

    A working agent outlives a short liveness window; the driver must keep
    draining rather than declaring the swarm finished. Pre-fix (600s constant,
    no beacon) the loop returns while the agent is mid-flight.
    """
    beacon = ProgressBeacon(stall_timeout=5.0)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _FakeAgent(spec, beacon=beacon, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('slow')])
        _await_started(agents, 'slow')

        drained = []
        done = threading.Event()

        def _drive():
            for item in sched.iter_completions(poll_interval=0.05):
                drained.append(item)
            done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()

        # Far longer than any per-poll tick: a healthy agent must not be
        # abandoned just because time passed.
        time.sleep(1.0)
        assert not done.is_set(), \
            'driver exited while the agent was still producing — this is the ' \
            '600s abandonment bug'
        assert sched.running_count == 1

        gate.set()
        assert done.wait(timeout=10), 'driver must exit once the agent settles'
        assert len(drained) == 1
        assert drained[0][1].final_answer == 'done for real'
    finally:
        gate.set()
        sched.shutdown()


def test_driver_still_stops_on_a_genuinely_stalled_swarm():
    """The complement: silence must still terminate, or we trade one bug for a hang."""
    beacon = ProgressBeacon(stall_timeout=0.4)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _SilentAgent(spec, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('wedged')])
        _await_started(agents, 'wedged')

        done = threading.Event()

        def _drive():
            for _ in sched.iter_completions(poll_interval=0.05):
                pass
            done.set()

        threading.Thread(target=_drive, daemon=True).start()
        assert done.wait(timeout=10), \
            'a swarm with zero activity past the stall window must terminate'
    finally:
        gate.set()
        sched.shutdown()


# ═══════════════════════════════════════════════════════
#  3. Termination honesty  (defect #1b — the "never complete" lie)
# ═══════════════════════════════════════════════════════

def test_scheduler_shutdown_waits_for_running_agents():
    """``shutdown()`` must not strand in-flight agents.

    Post-shutdown ``_launch_ready_locked`` raises ``RuntimeError: cannot
    schedule new futures after shutdown`` (verified), so any dependent spec
    unblocked by a late completion dies inside ``_run_one``.
    """
    beacon = ProgressBeacon(stall_timeout=5.0)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _FakeAgent(spec, beacon=beacon, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('busy')])
        _await_started(agents, 'busy')
        assert sched.running_count == 1

        gate.set()
        sched.shutdown()
        assert sched.running_count == 0, \
            'shutdown() returned while an agent was still running — its ' \
            'result can never be recorded'
    finally:
        gate.set()


# ═══════════════════════════════════════════════════════
#  4. Session TTL must follow activity, not age  (defect #3)
# ═══════════════════════════════════════════════════════

def test_session_ttl_is_refreshed_by_agent_activity():
    """``_session_timestamps`` must track ACTIVITY, not the spawn instant.

    Written once at spawn, the entry guaranteed ``session.abort()`` at TTL no
    matter how busy the swarm was (105 historical kills). ``_key_is_live`` did
    not save it: it looks for a non-terminal chat task, which a
    fire-and-forget swarm — the exact case — does not have.
    """
    import lib.swarm.integration._state as st

    key = 'test-conv-liveness'
    beacon = ProgressBeacon(stall_timeout=60.0)

    class _FakeSession:
        is_terminated = False
        progress_beacon = beacon

        def abort(self):
            raise AssertionError('a busy swarm must never be TTL-aborted')

    with st._sessions_lock:
        st._active_sessions[key] = _FakeSession()
        st._session_timestamps[key] = time.time() - (st.SESSION_TTL_SECONDS + 600)

    try:
        beacon.touch('a1', 'round_start')
        with st._sessions_lock:
            import lib.swarm.integration as _pkg
            _pkg._last_cleanup = 0.0
            st._last_cleanup = 0.0
            st._cleanup_stale_sessions()
        assert key in st._active_sessions, \
            'a swarm whose agents are actively producing was reaped by age'
    finally:
        with st._sessions_lock:
            st._active_sessions.pop(key, None)
            st._session_timestamps.pop(key, None)


def test_session_ttl_still_reaps_a_dead_session():
    """Complement: a genuinely quiet expired session must still be cleaned up."""
    import lib.swarm.integration._state as st

    key = 'test-conv-dead'
    beacon = ProgressBeacon(stall_timeout=0.01)
    aborted = threading.Event()

    class _FakeSession:
        is_terminated = False
        progress_beacon = beacon

        def abort(self):
            aborted.set()

    with st._sessions_lock:
        st._active_sessions[key] = _FakeSession()
        st._session_timestamps[key] = time.time() - (st.SESSION_TTL_SECONDS + 600)

    try:
        time.sleep(0.05)
        with st._sessions_lock:
            import lib.swarm.integration as _pkg
            _pkg._last_cleanup = 0.0
            st._last_cleanup = 0.0
            st._cleanup_stale_sessions()
        assert key not in st._active_sessions, \
            'an expired session with no activity must still be reaped'
        assert aborted.is_set()
    finally:
        with st._sessions_lock:
            st._active_sessions.pop(key, None)
            st._session_timestamps.pop(key, None)


# ═══════════════════════════════════════════════════════
#  5. Tool-level heartbeat  (defect #2 — the invisible hang)
# ═══════════════════════════════════════════════════════

def test_tool_output_counts_as_progress():
    """A long tool that keeps printing must keep its agent alive.

    This is the ``coder-tests`` shape: the agent blocks inside ONE
    ``run_command`` for over an hour. Round starts / tokens / tool returns all
    stop at the moment of entry, so without a tool-level signal the agent looks
    silent and any stall check would reap a healthy build.
    """
    from lib.swarm.liveness import notify_tool_progress, thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.25)
    beacon.touch('a1', 'tool_start')
    with thread_progress_sink(beacon, 'a1'):
        for _ in range(6):
            time.sleep(0.06)
            notify_tool_progress('subprocess_output')
            assert beacon.is_making_progress('a1'), \
                'subprocess output must count as progress'
    assert beacon.is_making_progress('a1')


def test_silent_tool_does_not_fake_progress():
    """Complement: entering a tool must not itself confer immortality."""
    from lib.swarm.liveness import thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.05)
    beacon.touch('a1', 'tool_start')
    with thread_progress_sink(beacon, 'a1'):
        time.sleep(0.15)          # tool produced NOTHING
        assert not beacon.is_making_progress('a1'), \
            'a tool that emits nothing at all must still be detectable'


def test_notify_outside_a_swarm_is_a_noop():
    """The main chat path must be unaffected — no sink bound, no error."""
    from lib.swarm.liveness import notify_tool_progress
    notify_tool_progress('subprocess_output')   # must not raise


def test_run_command_emits_the_heartbeat_on_real_output():
    """END-TO-END: the real ``_safe_on_chunk`` funnel must beat the beacon.

    Asserts the WIRING, not a constant: both run loops push every chunk
    through this helper, so if the heartbeat is removed from it a long command
    goes dark again. Note it must fire even with ``on_chunk=None`` — output
    proves life whether or not anyone subscribed to it.
    """
    from lib.project_mod.run_command import _safe_on_chunk
    from lib.swarm.liveness import thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.2)
    beacon.touch('a1', 'tool_start')
    time.sleep(0.25)
    assert not beacon.is_making_progress('a1')

    with thread_progress_sink(beacon, 'a1'):
        _safe_on_chunk(None, 'stdout', 'tests/test_foo.py .... [ 42%]\n')
    assert beacon.is_making_progress('a1'), \
        '_safe_on_chunk no longer heartbeats — a long run_command would ' \
        'look wedged to the stall check'

    with thread_progress_sink(beacon, 'a1'):
        _safe_on_chunk(None, 'stdout', '')      # empty chunk = no evidence
    assert beacon.seconds_since_activity('a1') < 0.2
