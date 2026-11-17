"""A waiting Coordinator reads only Workers that can have changed and yields to input between reads.

A lead polls its Workers with wait_s. A Worker whose task runs in this process
is read again only once its end fires here (or it no longer runs here); any
other is read at each durable recheck. Input ends a pass between two Worker
reads, however many Workers the lead polls. Ported from the old repo's
SCALE-COORD-1 line (38c9c6b3a4) onto the current wait
(docs/PROJECT_COORDINATION.md, "Waiting for Workers").
"""
import time

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_coordinator_workers import (
    _poll_in_thread, _subscriptions, _worker_message, coordinator_inbox, runtime)
from tests.project_mod.test_project_dispatch_repository import context

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('base_runtime', 'context', 'coordinator_inbox', 'runtime')

pytestmark = pytest.mark.unit


def _delegate(r, count):
    return [r.call('delegate_project_work', **{**r.args, 'idempotency_key': f'worker-{index}'})
            for index in range(count)]


def _running_here(monkeypatch, workers):
    """Each Worker's task runs in this process until the test removes it from the returned set."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    live, real = {worker['task_id'] for worker in workers}, chat_task_runtime.get_owned

    def get_owned(task_id, *, user_id):
        if task_id in live:
            return {'id': task_id, '_userId': user_id, 'status': 'running'}
        return real(task_id, user_id=user_id)
    monkeypatch.setattr(chat_task_runtime, 'get_owned', get_owned)
    return live


def _assignment_reads(r, monkeypatch):
    """Every assignment view a poll reads, in order; ``delay[0]`` seconds each."""
    service = type(r.service)
    reads, delay, real = [], [0.0], service.view_for_coordinator

    def view(self, assignment_id, *, scope, settlement_only=False):
        reads.append(assignment_id)
        if delay[0]:
            time.sleep(delay[0])
        return real(self, assignment_id, scope=scope, settlement_only=settlement_only)
    monkeypatch.setattr(service, 'view_for_coordinator', view)
    return reads, delay


def _end(r, worker, live, status='completed'):
    from lib.agent_core import admission
    row = r.c.repo.read(worker['assignment_id'])
    r.attempts[row['execution']['attemptId']]['status'] = status
    live.discard(worker['task_id'])
    admission.fire_terminal_callbacks(worker['task_id'])


def test_a_waiting_lead_rereads_only_the_workers_whose_end_fired(runtime, monkeypatch):
    from lib.tasks_pkg import wait_interruption
    r = runtime
    monkeypatch.setattr(wait_interruption, 'DURABLE_RECHECK_SECONDS', 0.05)
    workers = _delegate(r, 6)
    live = _running_here(monkeypatch, workers)
    subscribed = _subscriptions(monkeypatch)
    reads, _ = _assignment_reads(r, monkeypatch)
    ids = [worker['assignment_id'] for worker in workers]
    thread, result = _poll_in_thread(r, ids)
    assert subscribed(*[worker['task_id'] for worker in workers])
    time.sleep(0.3)
    before = len(reads)
    time.sleep(0.6)  # about a dozen durable rechecks while every Worker still runs here
    assert len(reads) == before, f'{len(reads) - before} assignment reads while no Worker changed'
    _end(r, workers[0], live)
    thread.join(5)
    assert not thread.is_alive() and result, 'the wait missed the fired end'
    value, _ = result[0]
    assert value['wait'] == 'progress', value
    # The ended Worker alone is read: its settlement, then its result for the excerpt.
    assert set(reads[before:]) == {ids[0]}, reads[before:]
    assert [row['assignment_id'] for row in value['workers']] == [ids[0]]
    assert value['pending']['running'] == ids[1:]


def test_a_later_poll_reads_no_worker_still_running_here(runtime, monkeypatch):
    r = runtime
    workers = _delegate(r, 6)
    live = _running_here(monkeypatch, workers)
    ids = [worker['assignment_id'] for worker in workers]
    assert r.call('poll', ids=ids, wait_s=0.05)['wait'] == 'timeout'
    reads, _ = _assignment_reads(r, monkeypatch)
    assert r.call('poll', ids=ids, wait_s=0.05)['wait'] == 'timeout'
    assert reads == [], reads
    # A Worker that no longer runs here is read again, and its settlement is news.
    r.attempts[r.c.repo.read(ids[2])['execution']['attemptId']]['status'] = 'completed'
    live.discard(workers[2]['task_id'])
    value = r.call('poll', ids=ids, wait_s=0.05)
    assert set(reads) == {ids[2]}, reads
    assert [row['assignment_id'] for row in value['workers']] == [ids[2]]


def test_input_ends_a_wait_between_two_worker_reads_however_many_it_polls(
        runtime, monkeypatch, coordinator_inbox):
    from lib.tasks_pkg import wait_interruption
    r = runtime
    monkeypatch.setattr(wait_interruption, 'DURABLE_RECHECK_SECONDS', 0.2)
    workers = _delegate(r, 20)
    reads, delay = _assignment_reads(r, monkeypatch)
    delay[0] = 0.25  # one full pass over every Worker takes 5 s
    ids = [worker['assignment_id'] for worker in workers]
    thread, result = _poll_in_thread(r, ids)
    while not reads:
        time.sleep(0.01)
    question = _worker_message(workers[0], 'mid-pass')
    sent = time.monotonic()
    thread.join(10)
    assert not thread.is_alive() and result
    value, _ = result[0]
    assert value['wait'] == 'pending_input', value
    assert time.monotonic() - sent < 1.5, 'the wait finished a full pass before it saw the input'
    assert [item['id'] for item in value['waiting']] == [question['id']]
    # Every Worker not known settled, read or not, is still named pending.
    assert len(reads) < len(ids) and value['pending']['running'] == ids
    assert not any(identity in value['instruction'] for identity in ids)


def test_a_settlement_outside_this_process_wakes_the_wait_within_one_recheck(runtime, monkeypatch):
    from lib.tasks_pkg import wait_interruption
    r = runtime
    monkeypatch.setattr(wait_interruption, 'DURABLE_RECHECK_SECONDS', 0.2)
    workers = _delegate(r, 20)
    live = _running_here(monkeypatch, workers[1:])
    reads, delay = _assignment_reads(r, monkeypatch)
    ids = [worker['assignment_id'] for worker in workers]
    assert r.call('poll', ids=ids, wait_s=0.1)['wait'] == 'timeout'
    delay[0] = 0.25  # re-reading the 19 Workers running here would take ~5 s per recheck
    reads.clear()
    thread, result = _poll_in_thread(r, ids)
    time.sleep(0.5)
    elsewhere = r.c.repo.read(ids[0])
    r.attempts[elsewhere['execution']['attemptId']]['status'] = 'completed'
    ended = time.monotonic()
    thread.join(10)
    assert not thread.is_alive() and result
    value, _ = result[0]
    assert value['wait'] == 'progress', value
    assert time.monotonic() - ended < 1.5, 'the settlement waited for a pass over every Worker'
    # Only the Worker not running here is read, at each recheck and for its excerpt.
    assert set(reads) == {ids[0]}, reads
    assert live == {worker['task_id'] for worker in workers[1:]}
