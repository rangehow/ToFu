"""Delegation returns at once; its Worker's birth completes on a thread of its own.

Owner decision 2026-11-11: delegating is as immediate as a human sending a
message. These specifications run births and tool-count flushes on real
threads, held by a gate before any effect, through the actual Coordinator tool.
"""
import threading
from types import SimpleNamespace

import pytest

from lib.conversations import coordinator_births, coordinator_workers
from lib.project_dispatch import request_budget
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.project_mod.test_coordinator_request_budget import bind, usage

__all__ = ('base_runtime', 'context')

pytestmark = pytest.mark.unit

# The fixtures run births inline; these specifications use the real threads.
_THREADED_BIRTH = coordinator_births.spawn
_THREADED_FLUSH = request_budget.spawn


@pytest.fixture
def held(runtime, monkeypatch):
    """Births run on their own threads and wait at root preparation until released."""
    r = runtime
    monkeypatch.setattr(coordinator_births, 'spawn', _THREADED_BIRTH)
    r.release, r.entered = threading.Event(), threading.Event()

    def prepare(project, **_):
        r.entered.set()
        assert r.release.wait(10)
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', prepare)
    r.settle = lambda: coordinator_births.wait(r.task, None, deadline=None, interrupted=lambda: False)

    def abort_attempt(attempt_id, owner):
        r.attempts[attempt_id]['status'] = 'interrupted'
        return SimpleNamespace(value={})
    r.service.commands.abort_attempt = abort_attempt
    return r


def test_delegation_returns_its_worker_identity_before_the_worker_exists(held):
    r = held
    started = r.call('delegate_project_work', **r.args)
    assert started['status'] == started['dispatch'] == 'starting', started
    assert r.entered.wait(10)
    assert not r.created and not r.calls and not r.c.repo.list()
    r.release.set()
    r.settle()
    row = r.c.repo.read(started['assignment_id'])
    assert row['request']['recipientId'] == started['conversation_id']
    assert r.calls == [started['conversation_id']] and len(r.created) == 1


def test_parallel_delegations_are_born_concurrently(held, monkeypatch):
    """Four delegations in one round never wait on each other's slow root preparation.

    Every birth is held inside root preparation; all four must be inside it at
    once, which a serialized birth (or a call blocking on its birth) cannot do.
    """
    r = held
    lock, entered, all_inside = threading.Lock(), [], threading.Event()

    def prepare(project, **_):
        with lock:
            entered.append(project)
            if len(entered) == 4:
                all_inside.set()
        assert r.release.wait(10)
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', prepare)
    results, callers = {}, []
    for index in range(4):
        arguments = {**r.args, 'title': f'Work {index}', 'idempotency_key': f'parallel-{index}'}
        callers.append(threading.Thread(target=lambda index=index, arguments=arguments: results.update(
            {index: r.call('delegate_project_work', **arguments)})))
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(10)
    # Every call returned while each birth is still held in preparation.
    assert sorted(results) == [0, 1, 2, 3]
    assert {result['status'] for result in results.values()} == {'starting'}, results
    assert all_inside.wait(10), entered
    assert not r.created and not r.calls
    r.release.set()
    r.settle()
    assert len(r.created) == len(r.calls) == 4
    assert len({result['assignment_id'] for result in results.values()}) == 4


def test_a_replay_joins_the_birth_in_flight_and_a_finished_one_runs_again(held):
    r = held
    first = r.call('delegate_project_work', **r.args)
    assert r.entered.wait(10)
    assert r.call('delegate_project_work', **r.args) == first
    assert 'error' in r.call('delegate_project_work', **{**r.args, 'brief': 'Different work'})
    r.release.set()
    r.settle()
    assert len(r.created) == len(r.calls) == 1
    # A finished birth runs again and reuses its accepted Worker.
    assert r.call('delegate_project_work', **r.args)['assignment_id'] == first['assignment_id']
    r.settle()
    assert len(r.created) == len(r.calls) == 1


def test_await_reports_a_starting_worker_then_the_born_one(held):
    r = held
    started = r.call('delegate_project_work', **r.args)
    assert r.entered.wait(10)
    waiting = r.call('await_project_workers', assignment_ids=[started['assignment_id']], timeout_seconds=0)
    assert waiting['wait'] == 'timeout' and waiting['workers'][0]['status'] == 'starting', waiting
    r.release.set()
    r.settle()
    born = r.call('await_project_workers', assignment_ids=[started['assignment_id']], timeout_seconds=0)
    assert born['wait'] == 'timeout' and born['pending']['running'] == [started['assignment_id']], born


def test_a_refused_birth_reaches_await_and_the_worker_page_and_a_replay_retries_it(held, monkeypatch):
    from lib.project_dispatch.admission import WorkerWorkspaceUnavailable
    r = held
    refusals = [WorkerWorkspaceUnavailable('The folder holds other projects.', code='ambiguous_project_root',
                                           details={'question': 'Share them?'})]

    def prepare(project, **_):
        assert r.release.wait(10)
        if refusals:
            raise refusals.pop()
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', prepare)
    started = r.call('delegate_project_work', **r.args)
    assert started['status'] == 'starting', started
    r.release.set()
    r.settle()
    awaited = r.call('await_project_workers', assignment_ids=[started['assignment_id']], timeout_seconds=0)
    row = awaited['workers'][0]
    assert awaited['wait'] == 'settled' and row['status'] == 'failed_to_start', awaited
    assert row['error']['code'] == 'ambiguous_project_root' and row['error']['details'] == {'question': 'Share them?'}
    assert 'owner' in row['error']['recovery'] and row['error']['execution_started'] is False
    page = r.call('get_project_workers')
    assert [item['assignment_id'] for item in page['starting_workers']] == [started['assignment_id']]
    assert not r.created and not r.calls and not r.c.repo.list()
    assert r.call('delegate_project_work', **r.args)['assignment_id'] == started['assignment_id']
    r.settle()
    assert r.calls == [started['conversation_id']]
    assert 'starting_workers' not in r.call('get_project_workers')


def test_await_after_a_restart_reports_a_delegation_that_never_reached_its_grant(held, monkeypatch):
    """A restart ends births with their task; storage then shows only born Workers."""
    r = held
    refuse, gate = [], threading.Event()

    def prepare(project, **_):
        assert gate.wait(10)
        if refuse:
            raise RuntimeError('interrupted by a restart')
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', prepare)
    born = r.call('delegate_project_work', **r.args)
    gate.set()
    r.settle()
    refuse.append(True)
    gate.clear()
    lost = r.call('delegate_project_work', **{**r.args, 'title': 'Lost', 'idempotency_key': 'lost-key'})
    assert lost['status'] == 'starting', lost
    gate.set()
    r.settle()
    r.task.pop(coordinator_births.BIRTHS_KEY)
    awaited = r.call('await_project_workers', assignment_ids=[born['assignment_id'], lost['assignment_id']],
                     timeout_seconds=0)
    rows = {row['assignment_id']: row for row in awaited['workers']}
    assert rows[lost['assignment_id']]['status'] == 'failed_to_start', awaited
    assert 'same arguments' in rows[lost['assignment_id']]['error']['recovery']
    assert awaited['pending']['running'] == [born['assignment_id']], awaited


@pytest.mark.parametrize('meanwhile', ['completed', 'stopped'])
def test_a_birth_outlives_its_completed_attempt_but_never_a_stop(held, meanwhile):
    r = held
    started = r.call('delegate_project_work', **r.args)
    assert r.entered.wait(10)
    if meanwhile == 'completed':
        r.attempts['coordinator-attempt']['status'] = 'completed'
    else:
        r.task['aborted'] = True
    r.release.set()
    r.settle()
    if meanwhile == 'completed':
        assert r.calls == [started['conversation_id']]
        return
    assert not r.calls and not r.c.repo.list()
    failure = r.task[coordinator_births.BIRTHS_KEY][started['assignment_id']].failure
    assert 'current executing Coordinator request' in failure['error']


def test_cancel_and_finish_wait_for_births_in_flight(held):
    r = held
    started = r.call('delegate_project_work', **r.args)
    assert r.entered.wait(10)
    outcome = {}
    cancelling = threading.Thread(target=lambda: outcome.update(
        r.call('cancel_project_work', assignment_id=started['assignment_id'])))
    cancelling.start()
    cancelling.join(0.5)
    assert cancelling.is_alive() and not outcome
    r.release.set()
    cancelling.join(10)
    assert outcome['revoked'] and r.c.repo.read(started['assignment_id'])['grant']['revoked'], outcome


def test_a_tool_reservation_never_waits_for_the_count_it_records(runtime, monkeypatch):
    r = runtime
    row = bind(r)
    monkeypatch.setattr(request_budget, 'spawn', _THREADED_FLUSH)
    gate, recorded, commits = threading.Event(), threading.Event(), []
    change = r.c.repo._change

    def held_change(update, **options):
        assert gate.wait(10)
        result = change(update, **options)
        commits.append(update)
        if usage(r, row)['toolCalls'] == 3:
            recorded.set()
        return result
    monkeypatch.setattr(r.c.repo, '_change', held_change)
    budget = r.task['_executionBudget']
    assert [budget.reserve_tool(r.task) for _ in range(3)] == [None, None, None]
    assert not commits
    gate.set()
    assert recorded.wait(10)
    # The first flush wrote one reservation; the two that arrived meanwhile share the next.
    assert len(commits) == 2
