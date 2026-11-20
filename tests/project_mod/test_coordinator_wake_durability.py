"""A Worker result wakes its idle Coordinator exactly once, even when a pass cannot finish.

Production 11-10 (04:37 boot to 07:57): 21 of 65 settlement drains failed
before or inside recover_coordinator (13 lost the dispatch snapshot to
concurrent writers, 8 KeyError 'source'); every one dropped its Coordinator
wake, and nothing retried it until another settlement of the same project.
A pass that cannot finish is now a durable pending wake (pending_wakes) that
the project's next dispatch commit, any later recovery pass, the maintenance
tick or startup completes; the wake's own command identity keeps it single.
Docs: docs/PROJECT_COORDINATION.md "Durable Coordinator wakes".
"""
import threading

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, activate, controls, delegate, end)
from tests.project_mod.test_coordinator_result_wake import board as board, iso
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.project_mod.test_project_dispatch_repository import model_attempt
from lib.project_dispatch import pending_wakes, recovery
from lib.project_dispatch.repository import DispatchContention
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

SOURCE = pending_wakes.source_entry('coordinator-attempt')


@pytest.fixture
def faults(monkeypatch):
    """Watchdog producers: every reported fault and every producer resolution."""
    record = {'reported': [], 'resolved': []}

    def report(owner, project, error, **kwargs):
        record['reported'].append(kwargs)
        return {'signalId': f'signal-{len(record["reported"])}'}

    class Signals:
        def __init__(self, owner, client=None):
            pass

        def resolve(self, ids, text):
            record['resolved'].append(ids)
    monkeypatch.setattr('lib.router.watchdog.signals.report_coordinator_wake_failure', report)
    monkeypatch.setattr('lib.router.watchdog.signals.report_recovery_failure', report)
    monkeypatch.setattr('lib.router.watchdog.signals.FaultSignals', Signals)
    return record


@pytest.fixture
def events(run, monkeypatch):
    """The project's next dispatch commit runs its pending wakes, inline for the spec."""
    r = run
    real = pending_wakes.process
    monkeypatch.setattr(pending_wakes, 'process',
                        lambda owner, project, **kw: real(owner, project, repository=r.c.repo, service=r.service))
    monkeypatch.setattr(pending_wakes, '_spawn', lambda target, key: target(key))
    yield
    with pending_wakes._LOCK:
        pending_wakes._PENDING.pop((7, '/project'), None)
        pending_wakes._DUE.pop((7, '/project'), None)
        pending_wakes._RUNNING.discard((7, '/project'))


def pending(r):
    return pending_wakes.PendingWakes(7, '/project', client=r.c.repo.client).entries()


def contend(r, monkeypatch, times):
    """The next ``times`` request snapshots lose to concurrent writers, as in production."""
    real, left = r.c.repo._load, [times]

    def load(selected=None, **options):
        if selected is not None and selected.get('sources') and left[0]:
            left[0] -= 1
            raise DispatchContention('Concurrent/uncertain dispatch snapshot; retry reading')
        return real(selected, **options)
    monkeypatch.setattr(r.c.repo, '_load', load)
    return left


def commit(r, row, label):
    """Any dispatch write of the project: here a Worker's model ledger entry."""
    r.c.repo.mutate(row['id'], lambda value: value['modelAttempts'].append(model_attempt(label)))


def settled_worker(r):
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    return worker


def test_a_wake_whose_snapshot_lost_to_writers_is_delivered_once_by_the_next_commit(run, faults, events,
                                                                                     monkeypatch):
    r = run
    worker = settled_worker(r)
    contend(r, monkeypatch, 2)
    assert r.wake() == ['deferred'] and controls(r) == []
    assert list(pending(r)) == [SOURCE]
    # A second failing pass is the same episode: no second fault.
    assert r.wake() == ['deferred'] and len(faults['reported']) == 1
    assert faults['reported'][0]['source_attempt_id'] == 'coordinator-attempt'
    commit(r, worker, 'next-event')
    wakes = controls(r)
    assert len(wakes) == 1 and wakes[0]['request']['coordinatorWake']['reason'] == 'results'
    assert pending(r) == {} and faults['resolved'] == [['signal-1']]
    # Later commits and passes find nothing pending and never wake twice.
    commit(r, worker, 'later-event')
    assert r.wake() == [] and len(controls(r)) == 1


def test_concurrent_passes_over_one_pending_wake_start_one_continuation(run, faults, monkeypatch):
    r = run
    settled_worker(r)
    contend(r, monkeypatch, 1)
    assert r.wake() == ['deferred'] and list(pending(r)) == [SOURCE]
    barrier = threading.Barrier(2)

    def pass_():
        barrier.wait(timeout=5)
        pending_wakes.process(7, '/project', repository=r.c.repo, service=r.service)
    threads = [threading.Thread(target=pass_) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(controls(r)) == 1 and pending(r) == {}
    assert r.calls.count('coordinator') == 1


def test_a_failed_settlement_drain_is_retained_until_a_later_event_completes_it(run, faults, events,
                                                                               monkeypatch):
    r = run
    worker = settled_worker(r)
    recipient = worker['request']['recipientId']
    real, failing = r.c.repo.discover, [1]

    def discover(*, recipient_id=None):
        if failing[0]:
            failing[0] -= 1
            raise StorageError('database_timeout', 'authority unavailable', retryable=True)
        return real(recipient_id=recipient_id)
    monkeypatch.setattr(r.c.repo, 'discover', discover)
    # The drain's own handler reports this occurrence; the entry keeps the work.
    with pytest.raises(StorageError):
        recovery.recover_recipient(7, '/project', recipient, repository=r.c.repo, service=r.service)
    assert list(pending(r)) == [pending_wakes.recipient_entry(recipient)] and controls(r) == []
    assert faults['reported'] == []
    commit(r, worker, 'next-event')
    assert len(controls(r)) == 1 and pending(r) == {}


def test_a_due_commitment_wake_that_cannot_start_is_retained_and_delivered_once(board, faults, events,
                                                                               monkeypatch):
    r = board
    worker = settled_worker(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)  # the results wake settled
    r.publish([{'title': 'Report the restart outcome', 'status': 'in_progress', 'due': iso(r.c.now[0] + 10)}])
    r.c.now[0] += 20  # the commitment is due; its timer runs this pass
    contend(r, monkeypatch, 1)
    assert r.wake() == ['deferred'] and list(pending(r)) == [SOURCE]
    commit(r, worker, 'next-event')
    wakes = controls(r)
    assert len(wakes) == 2 and wakes[-1]['request']['coordinatorWake']['reason'] == 'commitment_due'
    assert pending(r) == {}
    assert r.wake() == [] and len(controls(r)) == 2


def test_a_failure_after_completion_opens_a_new_episode(run, faults, events, monkeypatch):
    r = run
    worker = settled_worker(r)
    contend(r, monkeypatch, 1)
    assert r.wake() == ['deferred']
    commit(r, worker, 'first-recovery')
    assert len(controls(r)) == 1 and faults['resolved'] == [['signal-1']]
    # The next result's wake fails too: a new episode, a new fault, never
    # absorbed by the resolved one.
    activate(r)
    second = delegate(r, 'second')
    end(r, second)
    end(r, controls(r)[-1], delivered=False)
    contend(r, monkeypatch, 1)
    assert r.wake() == ['deferred']
    assert len(faults['reported']) == 2
    assert faults['reported'][0]['wake'] != faults['reported'][1]['wake']
