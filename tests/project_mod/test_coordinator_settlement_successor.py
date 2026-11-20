"""A queued successor on a Worker's lane must not consume its lead's settlement event."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, controls, delegate, end)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch import recovery
from lib import message_queue, turn_lifecycle

pytestmark = pytest.mark.unit


@pytest.fixture
def settlement(run, monkeypatch):
    r = run
    r.drains, r.order = [], []
    r.successor = 'human'
    monkeypatch.setattr('lib.tasks_pkg.manager.task_admission_closed', lambda: False)
    monkeypatch.setattr(message_queue, 'get_queue_depth', lambda *a, **kw: int(r.successor == 'human'))

    def successor(kind):
        def dispatch(conversation_id, *, user_id):
            assert user_id == 7
            r.order.append(kind)
            if kind != r.successor:
                return None
            r.attempts['unrelated-successor'] = {
                'attemptId': 'unrelated-successor', 'conversationId': conversation_id,
                'turnId': 'unrelated-turn', 'status': 'running'}
            return 'unrelated-task'
        return dispatch

    monkeypatch.setattr(message_queue, 'dispatch_next_queued', successor('human'))
    monkeypatch.setattr(message_queue, 'dispatch_completed_background_batch', successor('background'))
    resolve = recovery.recover_conversation_dispatch

    def recover(conversation_id, *, user_id, **kwargs):
        r.order.append('recover')
        return resolve(conversation_id, user_id=user_id, **kwargs,
            conversations=r.c.conversations,
            repository_factory=lambda owner, project: (r.c.repo if project == '/project'
                                                        else r.c.repository(owner, project)),
            service_factory=lambda repo: r.service, was_aborted_after=lambda *a: False,
            clock=r.c.repo.clock)

    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', recover)
    r.inputs = []
    create_turn = r.service.commands.create_turn

    def capture(*args, **kwargs):
        r.inputs.append(args[2]['inputTurn']['content'])
        return create_turn(*args, **kwargs)

    monkeypatch.setattr(r.service.commands, 'create_turn', capture)
    monkeypatch.setattr('lib.conversations.project_messages.wake_idle_lead', lambda *a, **kw: None)

    class CapturedThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): r.drains.append(self.target)

    def drain(worker):
        # end() has committed the fixture's exact attempt settlement; capture
        # only the asynchronous drain, so overlap uses real joined test threads.
        with monkeypatch.context() as patch:
            patch.setattr(threading, 'Thread', CapturedThread)
            turn_lifecycle._drain_queue_after_settlement(
                {'_userId': 7, '_attemptId': worker['execution']['attemptId']},
                worker['request']['recipientId'], 'completed')
        return r.drains[-1]

    r.drain = drain
    return r


@pytest.mark.parametrize('successor', ['human', 'background', None])
def test_worker_result_wakes_lead_even_when_its_lane_starts_unrelated_work(settlement, successor):
    r = settlement
    worker = delegate(r)
    sibling = delegate(r, 'independent-outcomes')
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.successor = successor
    before = deepcopy(r.c.repo.read(sibling['id']))
    drain = r.drain(worker)
    drain()
    assert len(controls(r)) == 1, 'the settled result needs its lead now, not after unrelated work ends'
    assert r.order == (['human', 'recover'] if successor == 'human' else ['background', 'recover'])
    assert r.c.repo.read(sibling['id']) == before
    assert r.calls.count('coordinator') == 1 and len(r.created) == 2
    if successor:
        assert r.attempts['unrelated-successor']['status'] == 'running'
    drain()  # Duplicate settlement delivery cannot mint another wake or Worker.
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    assert len(r.created) == 2


def test_overlapping_settlement_drains_share_one_wake_and_birth(settlement):
    r = settlement
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    drains = [r.drain(worker), r.drain(worker)]
    gate = threading.Barrier(2)

    def dispatch(drain):
        gate.wait(3)
        drain()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(dispatch, drains))
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    assert len(r.created) == 1


def test_immediately_settled_successor_does_not_duplicate_the_coordinator_wake(settlement, monkeypatch):
    r = settlement
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    dispatch = message_queue.dispatch_next_queued

    def immediate(conversation_id, *, user_id):
        task = dispatch(conversation_id, user_id=user_id)
        r.attempts['unrelated-successor']['status'] = 'completed'
        r.successor = None
        successor = deepcopy(worker)
        successor['execution']['attemptId'] = 'unrelated-successor'
        r.drain(successor)()
        return task

    monkeypatch.setattr(message_queue, 'dispatch_next_queued', immediate)
    r.drain(worker)()
    assert r.order == ['human', 'background', 'recover', 'recover']
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    assert len(r.created) == 1


def test_successor_notification_does_not_dispatch_unborn_worker_grants(settlement, monkeypatch):
    from lib.turn_lifecycle import LifecycleConflict
    r = settlement
    worker = delegate(r)
    create_turn = r.service.commands.create_turn

    def busy(*args, **kwargs):
        raise LifecycleConflict('lane_busy', 'Worker lane has earlier input')

    with monkeypatch.context() as patch:
        patch.setattr(r.service.commands, 'create_turn', busy)
        deferred = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'unborn'})
    assert deferred['dispatch'] == 'deferred'
    unborn = r.c.repo.read(deferred['assignment_id'])
    assert unborn['execution'] is None
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.drain(worker)()
    assert len(controls(r)) == 1
    assert r.c.repo.read(unborn['id']) == unborn
    assert r.service.commands.create_turn is create_turn
    assert len(r.calls) == 2  # The original Worker and its Coordinator, not another Worker.


def test_resumed_lead_sees_review_owner_question_and_independent_outcomes(settlement, monkeypatch):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from tests.project_mod.test_coordinator_continuation import activate
    r = settlement
    worker = delegate(r)
    r.args.update(title='Independent outcomes B and C', brief='Verify outcomes B and C independently.')
    sibling = delegate(r, 'two-outcomes')
    before = deepcopy(r.c.repo.read(sibling['id']))
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.client, bindings=r.c.binding,
        notify=lambda: None, clock=r.c.repo.clock, arm=lambda *a: None)
    progress.publish({'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator',
                      'bindingRevision': 1}, {'summary': 'Independent progress', 'tasks': [
        {'title': 'Outcome B', 'status': 'in_progress', 'progress': 'First check finished'},
        {'title': 'Outcome C', 'status': 'pending',
         'blocker': {'kind': 'needs_external_resource', 'resource': 'the signed input from the owner',
                     'check': 'the signed input verifies against the published key',
                     'ownerAction': 'send the signed input to the project inbox'},
         'reason': 'Owner must supply it'}]})
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda *a, **kw: [
        {'question': 'May I publish externally?', 'turnId': 'owner-question'}])
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.drain(worker)()
    content = r.inputs[-1]
    assert worker['id'] + ': result to review' in content
    assert '[in_progress] Outcome B' in content
    assert ('[pending] Outcome C: Waiting for: the signed input from the owner (proved by: the signed input '
            'verifies against the published key); you: send the signed input to the project inbox') in content
    assert 'May I publish externally? [Turn owner-question]' in content
    activate(r)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.c.repo.read(sibling['id']) == before
    assert len(r.created) == 2
    assert r.scheduled == []


def test_successor_settlement_does_not_cross_the_recipients_project(settlement):
    r = settlement
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.c.conversations.rows[worker['request']['recipientId']]['settings']['canonicalProjectPath'] = '/other'
    r.drain(worker)()
    assert r.order == ['human', 'recover']
    assert not controls(r) and len(r.created) == 1


def test_successor_settlement_cannot_reopen_a_finished_request(settlement):
    from tests.project_mod.test_coordinator_continuation import activate, report
    r = settlement
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    control = activate(r)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.call('finish_project_request', **report(r))['reason'] == 'complete'
    end(r, control, delivered=False)
    r.drain(worker)()
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    assert len(r.created) == 1


@pytest.mark.parametrize('withdrawal', ['stop', 'rebind', 'nonhuman_source', 'foreign_owner'])
def test_successor_settlement_never_recreates_ended_authority(settlement, withdrawal):
    r = settlement
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    if withdrawal == 'stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(7, 'coordinator', 1001, client=r.c.client)
    elif withdrawal == 'rebind':
        r.c.binding.value['revision'] = 2
    elif withdrawal == 'nonhuman_source':
        r.turns['human-turn']['actor'] = 'virtual_user'
    else:
        r.c.conversations.rows['coordinator']['user_id'] = 8
    r.drain(worker)()
    assert r.order == ['human', 'recover']
    assert not controls(r) and len(r.created) == 1
