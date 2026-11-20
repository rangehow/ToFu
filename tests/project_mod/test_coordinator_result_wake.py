"""Result-driven Coordinator wakes are never deferred by a board commitment.

Regression specs for the 11-09 13:41–16:55 incident: with an open commitment
due hours later on the task board, every Worker settlement still had to resume
the Coordinator immediately. The buggy gate (continuation._proposal) returned
{'wait_until': next_due} whenever `next_due` was set, regardless of new
evidence, so results sat unreviewed until the commitment's due time.

Docs: docs/PROJECT_COORDINATION.md "Waiting is not stalling" — only a retry
with NO new Worker evidence may wait for a promised time. A settled result is
new evidence and never waits.
"""
import logging

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, controls, delegate, end, ready)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.support.semantic_authorities import semantic_authority as semantic_authority
from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.project_dispatch import continuation

pytestmark = pytest.mark.unit


@pytest.fixture
def board(run):
    r = run
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.client, bindings=r.c.binding,
                                       notify=lambda: None, clock=lambda: r.c.now[0],
                                       arm=lambda *a: None)
    r.publish = lambda tasks: progress.publish(
        {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1},
        {'summary': 'What the owner follows', 'tasks': tasks})
    return r


def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec='seconds')


def test_a_worker_settlement_wakes_now_despite_a_future_commitment(board):
    """The incident shape: a future commitment + a new Worker result."""
    r = board
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    # A promise due an hour from now, unrelated to this Worker result.
    due = r.c.now[0] + 3600
    r.publish([{'title': 'Report the quota-reset result', 'status': 'pending', 'due': iso(due),
                'blocker': {'kind': 'next_action', 'action': 'Report the quota-reset result'}}])
    # The result is new evidence: the wake is dispatched NOW, not at `due`.
    assert r.wake() == ['accepted']
    assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'results'
    # The commitment itself is not consumed by a results wake.
    assert not controls(r)[-1]['request']['coordinatorWake'].get('commitments')


def test_a_settlement_during_a_running_turn_is_delivered_after_it(board):
    """A result that lands while the previous wake is still running is not lost."""
    r = board
    first = delegate(r)
    end(r, first)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    due = r.c.now[0] + 3600
    r.publish([{'title': 'Report the quota-reset result', 'status': 'pending', 'due': iso(due),
                'blocker': {'kind': 'next_action', 'action': 'Report the quota-reset result'}}])
    assert r.wake() == ['accepted']
    # A second Worker, already delegated, settles while the continuation turn
    # is mid-flight (activate so the delegation source is the current attempt).
    from tests.project_mod.test_coordinator_continuation import activate
    activate(r)
    second = delegate(r, 'second')
    end(r, second)
    # The running wake owns the lane; nothing starts now.
    assert r.wake() == []
    # Once that turn settles, the new result is delivered immediately.
    end(r, controls(r)[-1], delivered=False)
    assert r.wake() == ['accepted']
    assert len(controls(r)) == 2
    assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'results'


def test_a_commitment_due_still_fires_at_its_due_time(board):
    """Commitments keep their own wake; the fix only stops them delaying results."""
    r = board
    ready(r)
    end(r, controls(r)[-1], delivered=False)  # settle the first continuation
    due = r.c.now[0] + 10
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress', 'due': iso(due)}])
    r.c.now[0] += 20  # past the due time, no new Worker evidence
    assert r.wake() == ['accepted']
    assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'commitment_due'


def test_a_stall_retry_with_no_new_evidence_still_waits(board):
    """The documented 'waiting is not stalling' case is preserved."""
    r = board
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    due = r.c.now[0] + 3600
    r.publish([{'title': 'Report the quota-reset result', 'status': 'pending', 'due': iso(due),
                'blocker': {'kind': 'next_action', 'action': 'Report the quota-reset result'}}])
    end(r, controls(r)[-1], delivered=False)  # no new Worker evidence
    assert r.wake() == [], 'a retry with no new evidence waits for the promised time'
    assert r.scheduled[-1] == due


def test_a_continuation_start_failure_records_a_fault(board, monkeypatch):
    """A wake that cannot start is a WARNING + one fault, never a DEBUG line."""
    r = board
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    faults = []
    monkeypatch.setattr('lib.router.watchdog.signals.report_coordinator_wake_failure',
                        lambda owner, project, error, **kwargs: faults.append(
                            (owner, project, error, kwargs)))

    class Boom(RuntimeError):
        pass

    monkeypatch.setattr(r.service, 'dispatch_authorized',
                        lambda binding: (_ for _ in ()).throw(Boom('executor unavailable')))
    import logging as _logging
    records = []

    class Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture(level=_logging.WARNING)
    _logging.getLogger(continuation.__name__).addHandler(handler)
    try:
        assert r.wake() == ['deferred']
    finally:
        _logging.getLogger(continuation.__name__).removeHandler(handler)
    assert faults, 'a continuation that could not start must record a fault'
    owner, project, error, kwargs = faults[0]
    assert isinstance(error, Boom)
    assert kwargs.get('wake'), 'the fault names which wake could not start'
    assert any(rec.levelno == _logging.WARNING for rec in records), \
        'the failure is logged at WARNING, not DEBUG'


pytest_plugins = ('tests.support.postgres_cluster',)


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_the_wake_fault_is_durable_in_a_real_storage_authority(semantic_authority, tmp_path, monkeypatch):
    """The producer records a real watchdogSignal, idempotent across passes."""
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import router_watchdog
    from lib.router.watchdog.signals import FaultSignals, report_coordinator_wake_failure
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    global_router(owner, create=True)
    router_watchdog(owner, create=True)
    project = tmp_path / 'alpha'
    project.mkdir()
    coordinator = ProjectCoordinationRepository(owner).create(
        str(project), folder_id='folder', expected_revision=0, command_id='alpha')
    woken = []

    def dispatch(conversation_id, message, tools_config, **kwargs):
        woken.append((conversation_id, kwargs))
        from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
        return ScheduledTurnDispatch('started', task_id=f'wake-{len(woken)}')

    first = report_coordinator_wake_failure(
        owner, str(project), RuntimeError('executor unavailable'),
        conversation_id=coordinator['conversationId'], source_attempt_id='attempt-1',
        wake='coordinator-wake:abc', dispatch=dispatch)
    assert first is not None and first['state'] == 'open'
    assert first['error']['kind'] == 'coordinator_wake_failed'
    assert first['subject']['conversationId'] == coordinator['conversationId']
    assert first['subject']['role'] == 'project_router'
    # Replayed pass over the same wake: same signal, no second wake.
    again = report_coordinator_wake_failure(
        owner, str(project), RuntimeError('executor unavailable'),
        conversation_id=coordinator['conversationId'], source_attempt_id='attempt-1',
        wake='coordinator-wake:abc', dispatch=dispatch)
    assert again['signalId'] == first['signalId']
    assert len(woken) == 1
    open_ids = [s['signalId'] for s in FaultSignals(owner).open_page()['signals']]
    assert first['signalId'] in open_ids
