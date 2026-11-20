"""A Coordinator wake waits for its lane's event; final verdicts and seen results start nothing.

Regression specs for the 11-18 lead-metrics findings M1 and M3
(docs/PROJECT_COORDINATION.md "A deferred wake waits for its lane's Turn",
"A request whose authority ended is retired once", "A result the last Turn
saw starts no Turn"):

- M1: while a lead's Turn ran, every delivery-lane event re-derived each open
  request, built its wake's continuation context and asked for a Turn just to
  hear ``lane_busy`` (knowledge: ~50 refusals a minute for an hour), and
  re-derived the same final "request was stopped" verdict for dead requests.
- M3: a wake queued during a Turn started right after that Turn ended, although
  the Turn had already been told the results it was for, and only polled.
"""
import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, controls, delegate, end, ready)
from tests.project_mod.test_coordinator_result_wake import board as board, iso
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch import continuation
from lib.turn_lifecycle import LifecycleConflict

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def lane_hints():
    """The lane hints are process-local; every spec starts and ends without any."""
    continuation._LANE_HOLDS.clear()
    continuation._LANE_OBSERVATIONS.clear()
    yield
    continuation._LANE_HOLDS.clear()
    continuation._LANE_OBSERVATIONS.clear()


def busy_lane(r, attempt_id='busy-attempt'):
    """Another Coordinator Turn runs; the real lane refusal names it (``LifecycleConflict.turn``)."""
    r.attempts[attempt_id] = {'attemptId': attempt_id, 'conversationId': 'coordinator',
                              'turnId': 'busy-turn', 'taskId': 'busy-task', 'status': 'running',
                              'createdAt': 1_000_500}
    births = []
    commands = r.service.commands
    create = type(commands).create_turn

    def create_turn(conv, owner, request, **kwargs):
        births.append(conv)
        try:
            return create(commands, conv, owner, request, **kwargs)
        except LifecycleConflict as conflict:
            if conflict.code != 'lane_busy':
                raise
            raise LifecycleConflict('lane_busy', conflict.message,
                                    {'turnId': 'busy-turn', 'currentAttemptId': attempt_id}) from conflict
    commands.create_turn = create_turn
    return births


def test_a_deferred_wake_waits_for_the_running_turn_and_starts_once_when_it_ends(run):
    r = run
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    births = busy_lane(r)
    assert r.wake() == ['deferred']
    assert births == ['coordinator'] and controls(r)[0]['execution'] is None
    # Delivery-lane events, timed wakes and pending-wake passes while the Turn runs.
    for _ in range(5):
        assert r.wake() == ['deferred']
    assert births == ['coordinator'], 'no birth is attempted again while the refusing Turn runs'
    # The Turn ends; its settlement drain runs the pass: the wake starts exactly once.
    r.attempts['busy-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    assert births == ['coordinator', 'coordinator'] and len(controls(r)) == 1
    assert controls(r)[0]['execution'] is not None
    assert r.wake() == [] and len(births) == 2


def test_every_request_of_a_held_lane_waits_after_one_refusal(run):
    r = run
    first = delegate(r)
    end(r, first)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    births = busy_lane(r)
    assert r.wake() == ['deferred']
    held = continuation._LANE_HOLDS[(7, 'coordinator')]
    assert held == 'busy-attempt'
    # A pending-wake pass names its source explicitly; the hold applies to it too.
    source = first['request']['delegation']['attemptId']
    assert continuation.recover_coordinator(r.c.repo, service=r.service, sources=[source],
                                            schedule=r.scheduled.append) == ['deferred']
    assert births == ['coordinator']


def test_an_unreadable_holder_falls_back_to_the_ordinary_birth(run, monkeypatch):
    r = run
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    births = busy_lane(r)
    assert r.wake() == ['deferred']
    read = r.c.repo.attempt_get

    def flaky(attempt_id, **kwargs):
        if attempt_id == 'busy-attempt':
            raise RuntimeError('storage unavailable')
        return read(attempt_id, **kwargs)
    monkeypatch.setattr(r.c.repo, 'attempt_get', flaky)
    assert r.wake() == ['deferred'] and births == ['coordinator', 'coordinator']


def test_a_final_authority_verdict_retires_the_request_after_one_evaluation(run, monkeypatch):
    r = run
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    from lib.project_dispatch.repository import record_stop
    record_stop(7, 'coordinator', 1001, client=r.c.client)
    from lib.project_dispatch import delegation
    evaluations = []
    validate = delegation.validate_source
    monkeypatch.setattr(delegation, 'validate_source',
                        lambda *a, **kw: (evaluations.append(1), validate(*a, **kw))[1])
    assert r.wake() == ['retired'] and not controls(r)
    value, _ = r.c.repo._load()
    receipt = value['coordinatorSettlements'][0]
    assert receipt['reason'] == 'authority_ended' and 'reportTurnId' not in receipt
    assert receipt['sourceAttemptId'] == worker['request']['delegation']['attemptId']
    # One evaluation under the pass and one under its CAS; none after.
    seen = len(evaluations)
    for _ in range(3):
        assert r.wake() == []
    assert continuation.recover_coordinator(
        r.c.repo, service=r.service, sources=[receipt['sourceAttemptId']],
        schedule=r.scheduled.append) == []
    assert len(evaluations) == seen, 'a retired request is never re-derived'
    _, settled = r.c.repo.discover()
    assert receipt['sourceAttemptId'] in settled


def test_a_live_request_is_never_retired_by_a_raced_verdict(run, monkeypatch):
    """The CAS re-derives the verdict: authority restored meanwhile keeps the request open."""
    r = run
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    from lib.project_dispatch import delegation
    validate = delegation.validate_source
    calls = []

    def first_refuses(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError('Original Coordinator request was stopped')
        return validate(*args, **kwargs)
    monkeypatch.setattr(delegation, 'validate_source', first_refuses)
    assert r.wake() == ['deferred']
    value, _ = r.c.repo._load()
    assert not value.get('coordinatorSettlements')
    assert r.wake() == ['accepted']


def poll_reports(r, *assignments):
    """The Coordinator attempt's own poll: what it was told, as its Turn end records it."""
    outcome = r.call('poll', ids=[row['id'] for row in assignments], wait_s=0)
    assert 'error' not in outcome, outcome
    return dict(r.task['_coordinatorWorkerReports']['reported'])


def test_a_result_the_ended_turn_saw_starts_no_turn_while_others_run(run):
    r = run
    seen = delegate(r)
    running = delegate(r, 'second')
    end(r, seen)
    reported = poll_reports(r, seen)
    assert seen['id'] in reported
    # The Turn ends (an idle wait on the running Worker) after it was told the result.
    r.attempts['coordinator-attempt']['status'] = 'completed'
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', reported)
    assert r.wake() == [] and not controls(r), 'no empty re-wake for a result already reported'
    # A genuinely new event after the Turn end still wakes the lead.
    end(r, running)
    assert r.wake() == ['accepted'] and len(controls(r)) == 1


def test_a_wake_queued_during_the_turn_starts_only_for_news(run):
    """The traced M3 case: authorized while the lane was busy, born after the Turn saw its cause."""
    r = run
    seen = delegate(r)
    running = delegate(r, 'second')
    end(r, seen)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    births = busy_lane(r)
    assert r.wake() == ['deferred'] and controls(r)[0]['execution'] is None
    # The busy Turn's polls reported the result, then it completed.
    reported = poll_reports(r, seen)
    r.attempts['busy-attempt']['status'] = 'completed'
    continuation.observe_turn_end(7, 'coordinator', 'busy-attempt', reported)
    assert r.wake() == [] and births == ['coordinator']
    assert controls(r)[0]['execution'] is None
    # The queued wake is kept, not revoked: the next settlement starts it.
    end(r, running)
    assert r.wake() == ['accepted'] and len(controls(r)) == 1
    assert controls(r)[0]['execution'] is not None


def test_a_seen_result_still_wakes_when_nothing_else_will(run):
    """With no pending Worker, no later settlement would wake the request: it wakes now."""
    r = run
    seen = delegate(r)
    end(r, seen)
    reported = poll_reports(r, seen)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', reported)
    assert r.wake() == ['accepted']


def test_a_long_deferred_wake_starts_with_its_brief_rebuilt_from_current_state(board, monkeypatch):
    """Wake 7a04ce76 (tofu lead, 2026-11-18): authorized 00:01, refused ``lane_busy``
    until 08:34, it started with a brief whose wording a 04:36 change had removed.

    The instruction is the code's current one at birth, and a due promise
    completed while the wake waited is no longer announced as due.
    """
    r = board
    ready(r)
    end(r, controls(r)[-1], delivered=False)
    due = r.c.now[0] + 10
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress', 'due': iso(due)},
               {'title': 'Ship the queue fix', 'status': 'pending', 'due': iso(due),
                'blocker': {'kind': 'next_action', 'action': 'Merge the queue fix'}}])
    r.c.now[0] += 20
    current = continuation.continuation_instruction
    monkeypatch.setattr(continuation, 'continuation_instruction',
                        lambda profile: current(profile) + 'Wording a later change removes. ')
    births = busy_lane(r)
    inputs = []
    create = r.service.commands.create_turn

    def capture(conv, owner, request, **kwargs):
        inputs.append(request['inputTurn']['content'])
        return create(conv, owner, request, **kwargs)
    r.service.commands.create_turn = capture
    assert r.wake() == ['deferred']
    deferred = controls(r)[-1]
    assert deferred['execution'] is None
    assert deferred['request']['coordinatorWake']['reason'] == 'commitment_due'
    assert 'Wording a later change removes.' in deferred['request']['brief']

    # Hours later the code's wording changed and one promise was kept.
    monkeypatch.setattr(continuation, 'continuation_instruction', current)
    r.c.now[0] += 8 * 3600
    r.publish([{'title': 'Report the restart outcome', 'status': 'completed', 'due': iso(due)},
               {'title': 'Ship the queue fix', 'status': 'pending', 'due': iso(due),
                'blocker': {'kind': 'next_action', 'action': 'Merge the queue fix'}}])
    r.attempts['busy-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted'] and births == ['coordinator', 'coordinator']
    started = inputs[-1]
    assert 'Wording a later change removes.' not in started
    assert current(deferred['request']['profile']) in started
    assert '"Ship the queue fix" \u2014 due' in started
    assert '"Report the restart outcome"' not in started
    # The authorization record keeps what was authorized.
    assert controls(r)[-1]['request']['brief'] == deferred['request']['brief']


def test_a_newer_settlement_than_the_reported_one_wakes(run):
    r = run
    seen = delegate(r)
    delegate(r, 'second')
    end(r, seen)
    reported = poll_reports(r, seen)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    # Reported at another attempt than the current one: that result is news.
    stale = {seen['id']: (reported[seen['id']][0], reported[seen['id']][1], 'older-attempt',
                          reported[seen['id']][3])}
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', stale)
    assert r.wake() == ['accepted']


def test_the_settlement_drain_records_a_completed_coordinator_attempts_reports(monkeypatch):
    from lib import turn_lifecycle
    from threading import RLock
    recorded = []
    monkeypatch.setattr(continuation, 'observe_turn_end', lambda *args: recorded.append(args))
    monkeypatch.setattr('lib.tasks_pkg.manager.task_admission_closed', lambda: False)
    monkeypatch.setattr('lib.tasks_pkg.manager.task_user_id', lambda task: 7)

    def stop_here(*args, **kwargs):
        raise RuntimeError('drain not under test')
    monkeypatch.setattr('lib.message_queue.get_queue_depth', stop_here)
    reports = {'reported': {'a1': ('needs_review', 't1', 'att1', None)}, 'pending': set(),
               'seeded': True, 'lock': RLock()}
    task = {'id': 'task', '_attemptId': 'attempt', '_coordinatorWorkerReports': reports}
    turn_lifecycle._drain_queue_after_settlement(task, 'coordinator', 'completed')
    assert recorded == [(7, 'coordinator', 'attempt', {'a1': ('needs_review', 't1', 'att1', None)})]
    recorded.clear()
    turn_lifecycle._drain_queue_after_settlement(task, 'coordinator', 'failed')
    assert recorded == [], 'only a completed attempt finished judging what it saw'
