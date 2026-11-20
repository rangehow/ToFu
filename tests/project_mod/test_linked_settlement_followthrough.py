"""Hermetic dispatch linkage, delivery readiness and queued-successor settlement composition."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.conversations.coordinator_progress import CoordinatorTaskProgress
from lib.project_dispatch import admission, recovery
from lib.project_mod import delivery
from tests.project_mod.test_coordinator_continuation import (
    run, controls, end, activate,
)
from tests.project_mod.test_coordinator_workers import runtime, base_runtime, context
from tests.project_mod.test_coordinator_settlement_successor import settlement
from tests.project_mod.test_coordinator_task_history import Store

__all__ = ('run', 'runtime', 'base_runtime', 'context', 'settlement')
pytestmark = pytest.mark.unit


@pytest.fixture
def linked_settlement(settlement, monkeypatch):
    r = settlement
    r.c.client = r.c.repo.client = Store()
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.client,
        bindings=r.c.binding, notify=lambda: None, arm=lambda *a: None, clock=r.c.repo.clock)
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    progress.publish(scope, {'summary': 'Ordinary dummy outcomes', 'tasks': [
        {'title': 'Prepare dummy input', 'status': 'in_progress'},
        {'title': 'Consume dummy input', 'status': 'pending',
         'blocker': {'kind': 'blocked_by_dependency', 'assignmentId': 'assignment-dummy', 'revision': 1},
         'reason': 'Await exact delivered input'},
        {'title': 'Consume dummy input (legacy)', 'status': 'pending',
         'blocker': {'kind': 'next_action', 'action': 'Consume the dummy input'}}]})
    r.progress = progress
    r.board_keys = progress.read_for_coordinator(scope)['taskKeys']
    monkeypatch.setattr(recovery, 'ProjectDispatchRepository', lambda owner, project: r.c.repo)
    monkeypatch.setattr(recovery, 'ProjectDispatchService', lambda repo: r.service)
    monkeypatch.setattr('lib.conversation_sync.pending_abort.was_pending_abort_after', lambda *a: False)
    return r


@pytest.mark.parametrize('successor', ['human', 'background'])
def test_linked_delivery_then_queued_successor_wakes_original_request_once(linked_settlement, monkeypatch, successor):
    r = linked_settlement
    board_before = deepcopy(r.progress.read())
    prior_args = {**r.args, 'title': 'Prepare dummy input', 'idempotency_key': 'prepare',
                  'board_task_keys': r.board_keys[:1]}
    first = r.call('delegate_project_work', **prior_args)
    assert first['dispatch'] == 'accepted', first
    prior = r.c.repo.read(first['assignment_id'])
    assert prior['request']['boardTaskKeys'] == r.board_keys[:1]
    end(r, prior, delivered=False)
    work_id = prior['execution']['taskId']
    checkpoint = 'dummy-exact-checkpoint'
    workspace = {'task_id': work_id, 'user_id': 7, 'project_root': '/project',
        'state': 'merged', 'workspace_path': '/source', 'baseline_sha': 'base',
        'checkpoint_sha': checkpoint, 'candidate_sha': 'candidate',
        'origin': {admission.KEY: r.c.repo.admission(prior['id']), 'delivery': {
            'taskOutcome': 'completed', 'state': 'pending', 'phase': 'adoption',
            'checkpointSha': checkpoint, 'targetRef': 'refs/heads/main',
            'conversationId': prior['request']['recipientId']}}}
    r.workspaces[work_id] = workspace
    pin = {'assignmentId': prior['id'], 'grantRevision': prior['grant']['revision'],
           'resultRevision': 1, 'checkpointSha': checkpoint}
    args = {**r.args, 'title': 'Consume dummy input', 'idempotency_key': 'consume',
            'dependencies': [pin], 'board_task_keys': r.board_keys[1:2]}
    deferred = r.call('delegate_project_work', **args)
    assert deferred['dispatch'] == 'deferred', deferred
    dependent = r.c.repo.read(deferred['assignment_id'])
    original_grant = deepcopy(dependent['grant'])
    assert dependent['request']['boardTaskKeys'] == r.board_keys[1:2]

    def dependency_status():
        return r.c.repo.dependency_states(r.c.repo.read(dependent['id']))[0]['status']

    assert dependency_status() == 'waiting_review'
    assert dependent['execution'] is None and dependent['modelAttempts'] == []
    assert len(r.calls) == 1
    # A same-title legacy delegation has no coverage evidence to adopt or guess.
    legacy = r.call('delegate_project_work', **{**r.args, 'title': 'Consume dummy input',
                                              'idempotency_key': 'legacy'})
    assert legacy['dispatch'] == 'accepted'
    assert 'boardTaskKeys' not in r.c.repo.read(legacy['assignment_id'])['request']

    r.service.publication_lock = lambda project: nullcontext()
    r.service.publication_wake = lambda: None
    r.service.publication_release = lambda *a, **kw: None
    reviewed = r.call('review_project_work', assignment_id=prior['id'], result_revision=1,
                      decision='accept', note='Checked dummy input')
    assert reviewed['decision'] == 'accept', reviewed
    assert dependency_status() == 'waiting_delivery'
    assert r.c.repo.read(dependent['id'])['execution'] is None
    published = r.call('review_project_work', assignment_id=prior['id'], result_revision=1,
                       decision='accept', note='Checked dummy input', checkpoint_sha=checkpoint)
    assert 'error' not in published, published
    assert r.c.repo.read(prior['id'])['publication']['checkpointSha'] == checkpoint
    assert dependency_status() == 'waiting_delivery'
    assert r.c.repo.read(dependent['id'])['execution'] is None

    # Git and workspace storage are ports; the receiver, receipt writer and
    # post-ACK recovery remain real. No delivered receipt is seeded by the test.
    monkeypatch.setattr(delivery.state, 'get_workspace', lambda *a, **kw: deepcopy(workspace))

    def save(**kwargs):
        assert kwargs['task_id'] == work_id and kwargs['user_id'] == 7
        workspace['origin'].update(deepcopy(kwargs['patch']))
        return deepcopy(workspace['origin'])

    monkeypatch.setattr(delivery.state, 'set_workspace_meta', save)

    class PresentRoot(type(Path())):
        def is_dir(self):
            return str(self) == '/project' or super().is_dir()

    monkeypatch.setattr(delivery, 'Path', PresentRoot)
    control = SimpleNamespace(_repo_root=lambda p: Path('/project'),
        _repo_lock=lambda *a, **kw: nullcontext(), _now=r.c.repo.clock, _push=lambda *a, **kw: None,
        task_publication_checkpoint=lambda row: row['checkpoint_sha'],
        _git=lambda root, args: SimpleNamespace(returncode=0, stdout='refs/heads/main', stderr=''),
        _rev=lambda *a: checkpoint, _porcelain=lambda *a: {'total': 0})
    monkeypatch.setattr(delivery, '_control', lambda: control)
    monkeypatch.setattr('lib.project_mod.workspace_lease.workspace_write_lease', lambda *a, **kw: nullcontext())
    monkeypatch.setattr(delivery, '_publication_hold', lambda *a: None)
    receipt = delivery.deliver_registered_workspace('/project', work_id, user_id=7)
    assert receipt['state'] == 'delivered' and receipt['checkpointSha'] == checkpoint
    assert dependency_status() == 'ready'
    born = r.c.repo.read(dependent['id'])
    assert born['execution'] is not None and born['grant'] == original_grant
    assert born['request'] == dependent['request'] and born['modelAttempts'] == []
    assert not controls(r), 'prerequisite readiness is not a Coordinator wake while its Turn is live'
    delivery.deliver_registered_workspace('/project', work_id, user_id=7)
    assert r.c.repo.read(dependent['id'])['execution'] == born['execution']

    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.successor = successor
    drain = r.drain(prior)
    drain()
    assert r.order == [successor, 'recover']
    assert r.attempts['unrelated-successor']['status'] == 'running'
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    wake = controls(r)[0]
    assert wake['request']['delegation'] == prior['request']['delegation']
    assert wake['request']['authorizationSource'] == prior['request']['authorizationSource']
    assert wake['grant']['owner'] == 7 and wake['grant']['bindingRevision'] == 1
    assert wake['request']['coordinatorId'] == wake['request']['recipientId'] == 'coordinator'
    assert 'Consume dummy input' in r.inputs[-1]
    drain()
    assert len(controls(r)) == r.calls.count('coordinator') == 1
    activate(r)
    replay = r.call('delegate_project_work', **args)
    assert replay['assignment_id'] == dependent['id'] and replay['dispatch'] == 'accepted'
    assert r.c.repo.read(dependent['id'])['execution'] == born['execution']
    linked = [row for row in r.c.repo.list()
              if r.board_keys[1] in row['request'].get('boardTaskKeys', [])]
    assert [row['id'] for row in linked] == [dependent['id']]
    assert all(r.board_keys[2] not in row['request'].get('boardTaskKeys', []) for row in r.c.repo.list())
    assert len(r.created) == 3 and len(r.calls) == 4 and r.scheduled == []
    assert r.progress.read() == board_before, 'settlement and title similarity cannot change the board'
