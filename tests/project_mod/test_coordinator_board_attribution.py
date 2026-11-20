"""Dispatch coverage is immutable descriptive evidence, never authority or title inference."""
from copy import deepcopy

import pytest

from tests.project_mod.test_coordinator_workers import runtime, base_runtime, context
from tests.project_mod.test_coordinator_task_history import Store
from lib.conversations.coordinator_progress import CoordinatorTaskProgress, POLICY
from lib.conversations.coordinator_births import BIRTHS_KEY

__all__ = ('runtime', 'base_runtime', 'context')
pytestmark = pytest.mark.unit


@pytest.fixture
def linked(runtime):
    r = runtime
    r.c.repo.client = Store()
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.repo.client,
        bindings=r.c.binding, notify=lambda: None, arm=lambda *args: None)
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    progress.publish(scope, {'summary': 'Two outcomes', 'tasks': [
        {'title': 'Outcome A', 'status': 'in_progress'},
        {'title': 'Outcome B', 'status': 'pending',
         'blocker': {'kind': 'next_action', 'action': 'Check the second outcome'}}]})
    r.progress, r.board_scope = progress, scope
    r.keys = progress.read_for_coordinator(scope)['taskKeys']
    return r


def test_two_outcomes_many_assignments_retry_rename_and_passive_projection(linked):
    r = linked
    args = {**r.args, 'board_task_keys': r.keys}
    first = r.call('delegate_project_work', **args)
    assert first['dispatch'] == 'accepted', first
    row = r.c.repo.read(first['assignment_id'])
    assert row['request']['boardTaskKeys'] == r.keys
    assert row['grant']['bindingRevision'] == 1
    assert row['grant']['owner'] == 7
    assert r.c.repo.validate(r.c.repo.admission(row['id'])) == row
    second = r.call('delegate_project_work', **{**args, 'idempotency_key': 'second'})
    assert r.c.repo.read(second['assignment_id'])['request']['boardTaskKeys'] == r.keys
    # A concurrent rename changes display text, not the captured key evidence.
    head = r.c.repo.client.rows[(POLICY['namespace'], r.progress.key)]
    head['value']['board']['tasks'][0]['title'] = 'Renamed A'
    assert r.progress.read_for_coordinator(r.board_scope, title='Renamed A')['taskKeys'] == r.keys[:1]
    r.task.pop(BIRTHS_KEY, None)
    replay = r.call('delegate_project_work', **args)
    assert replay == first and len(r.calls) == 2
    assert r.c.repo.read(row['id'])['request']['boardTaskKeys'] == r.keys
    from lib.project_dispatch.service import HISTORY_REQUEST_FIELDS, COORDINATOR_REQUEST_FIELDS
    assert 'boardTaskKeys' in HISTORY_REQUEST_FIELDS
    assert 'boardTaskKeys' in COORDINATOR_REQUEST_FIELDS
    view = r.service.view(row['id'], principal=r.c.principal)
    assert view['request']['boardTaskKeys'] == r.keys
    changed = r.call('delegate_project_work', **{**args, 'board_task_keys': r.keys[:1]})
    assert 'error' in changed and len(r.calls) == 2


def test_invalid_key_refuses_before_conversation_and_does_not_reveal_candidates(linked):
    r = linked
    from lib.conversations.coordinator_task_history import NAMESPACE
    identity, candidate = next((key, deepcopy(value)) for key, value in r.c.repo.client.rows.items()
                               if key[0] == NAMESPACE)
    candidate['value']['key'] = 'f' * 32
    r.c.repo.client.rows[(NAMESPACE, identity[1][:-32] + 'f' * 32)] = candidate
    result = r.call('delegate_project_work', **{**r.args, 'board_task_keys': ['f' * 32]})
    assert 'error' in result and not r.created and not r.calls
    assert 'Outcome A' not in str(result) and r.keys[0] not in str(result)
    assert r.c.repo.list() == []


def test_explicit_empty_and_legacy_unknown_are_distinct(linked):
    r = linked
    unknown = r.call('delegate_project_work', **r.args)
    assert 'boardTaskKeys' not in r.c.repo.read(unknown['assignment_id'])['request']
    empty = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'empty', 'board_task_keys': []})
    assert r.c.repo.read(empty['assignment_id'])['request']['boardTaskKeys'] == []


def test_new_binding_does_not_adopt_old_keys(linked):
    r = linked
    r.c.binding.value['revision'] = 2
    result = r.call('delegate_project_work', **{**r.args, 'board_task_keys': r.keys})
    assert 'error' in result and not r.calls and not r.created


def test_rework_inherits_immutable_coverage_without_accepting_invalid_override(linked):
    r = linked
    first = r.call('delegate_project_work', **{**r.args, 'board_task_keys': r.keys})
    row = r.c.repo.read(first['assignment_id'])
    association = row['execution']
    r.turns[association['turnId']]['projectionRevision'] = 2
    r.attempts[association['attemptId']]['status'] = 'completed'
    review = r.call('review_project_work', assignment_id=row['id'], result_revision=2,
                    decision='rework', note='Repair both outcomes')
    assert 'error' not in review, review
    row = r.c.repo.read(row['id'])
    pin = {'assignmentId': row['id'], 'grantRevision': row['grant']['revision'], 'resultRevision': 2}
    r.progress.publish(r.board_scope, {'summary': 'Retired original outcomes', 'tasks': []})
    args = {**r.args, 'idempotency_key': 'repair', 'rework_of': pin}
    repair = r.call('delegate_project_work', **args)
    assert 'error' not in repair, repair
    assert r.c.repo.read(repair['assignment_id'])['request']['boardTaskKeys'] == r.keys
    invalid = r.call('delegate_project_work', **{**args, 'idempotency_key': 'bad-repair',
                                               'board_task_keys': ['f' * 32]})
    assert 'error' in invalid and len(r.calls) == 2
    assert deepcopy(row['request']['boardTaskKeys']) == r.keys


def test_cancelled_and_departed_coverage_is_historical_and_retry_does_not_relink(linked):
    r = linked
    args = {**r.args, 'board_task_keys': r.keys}
    first = r.call('delegate_project_work', **args)
    r.progress.publish(r.board_scope, {'summary': 'Retired outcomes', 'tasks': [
        {'title': 'Outcome A', 'status': 'cancelled', 'reason': 'No longer needed'}]})
    r.task.pop(BIRTHS_KEY, None)
    assert r.call('delegate_project_work', **args) == first
    another = r.call('delegate_project_work', **{**args, 'idempotency_key': 'historical-work'})
    assert 'error' in another and len(r.calls) == 1
    assert r.c.repo.read(first['assignment_id'])['request']['boardTaskKeys'] == r.keys
    assert r.progress.read()['board']['tasks'][0]['status'] == 'cancelled'


def test_successor_keeps_predecessor_coverage_without_title_resolution(monkeypatch):
    from types import SimpleNamespace
    from lib.conversations.session_control import _successor
    from lib.conversations import coordinator_workers
    captured = []

    def delegate(task, scope, arguments, **kwargs):
        captured.append((arguments, kwargs))
        return {'assignment_id': 'next', 'dispatch': 'accepted', 'status': 'running', 'task_id': 'task-next'}

    monkeypatch.setattr(coordinator_workers, 'delegate', delegate)
    row = {'id': 'previous', 'request': {'brief': 'Original work', 'boardTaskKeys': ['a' * 32, 'b' * 32]},
           'execution': {'turnId': 'old-turn', 'attemptId': 'old-attempt'}, 'review': None}
    service = SimpleNamespace(attempt_get=lambda *args, **kw: {}, workspace_get=lambda *args: None)
    result = _successor({}, {'user_id': 7},
        {'attempt_id': 'old-attempt', 'expected_revision': 3, 'instruction': 'Continue the work'},
        row, {'turnId': 'old-turn', 'conversationId': 'worker', 'projectionRevision': 3}, service)
    assert result['continues_assignment'] == row['id']
    assert 'board_task_keys' not in captured[0][0]
    assert captured[0][1]['coverage_from'] == row['id']


@pytest.mark.parametrize('coverage', [None, 'current'])
def test_current_binding_rework_does_not_inherit_foreign_epoch_coverage(linked, coverage):
    r = linked
    first = r.call('delegate_project_work', **{**r.args, 'board_task_keys': r.keys})
    old = r.c.repo.read(first['assignment_id'])
    association = old['execution']
    r.turns[association['turnId']]['projectionRevision'] = 2
    r.attempts[association['attemptId']]['status'] = 'completed'
    assert 'error' not in r.call('review_project_work', assignment_id=old['id'], result_revision=2,
                                 decision='rework', note='Repair old result')
    old = r.c.repo.read(old['id'])
    r.c.binding.value['revision'] = 2
    r.board_scope['bindingRevision'] = 2
    r.progress.publish(r.board_scope, {'summary': 'New namespace', 'tasks': [
        {'title': 'Outcome A', 'status': 'in_progress'}]})
    current_keys = r.progress.read_for_coordinator(r.board_scope)['taskKeys']
    assert current_keys == r.keys[:1]
    r.task.pop('_coordinatorDispatchScope', None)
    args = {**r.args, 'idempotency_key': 'new-epoch-rework', 'rework_of': {
        'assignmentId': old['id'], 'grantRevision': old['grant']['revision'], 'resultRevision': 2}}
    if coverage == 'current':
        args['board_task_keys'] = current_keys
    repaired = r.call('delegate_project_work', **args)
    assert 'error' not in repaired, repaired
    new = r.c.repo.read(repaired['assignment_id'])
    assert old['grant']['bindingRevision'] == 1 and new['grant']['bindingRevision'] == 2
    if coverage == 'current':
        assert new['request']['boardTaskKeys'] == current_keys
    else:
        assert 'boardTaskKeys' not in new['request']
    assert old['request']['boardTaskKeys'] == r.keys


@pytest.mark.parametrize('explicit', [False, True])
def test_legacy_epochless_rework_preserves_unknown_or_explicit_empty(linked, explicit):
    r = linked
    first = r.call('delegate_project_work', **r.args)
    old = r.c.repo.read(first['assignment_id'])
    association = old['execution']
    r.turns[association['turnId']]['projectionRevision'] = 2
    r.attempts[association['attemptId']]['status'] = 'completed'
    assert 'error' not in r.call('review_project_work', assignment_id=old['id'], result_revision=2,
                                 decision='rework', note='Repair legacy work')
    from lib.project_dispatch.repository import NAMESPACE
    stored = r.c.repo.client.rows[(NAMESPACE, r.c.repo.key)]['value']['assignments'][0]
    del stored['grant']['bindingRevision']
    args = {**r.args, 'idempotency_key': 'legacy-repair', 'rework_of': {
        'assignmentId': old['id'], 'grantRevision': old['grant']['revision'], 'resultRevision': 2}}
    if explicit:
        args['board_task_keys'] = []
    repaired = r.call('delegate_project_work', **args)
    assert 'error' not in repaired, repaired
    request = r.c.repo.read(repaired['assignment_id'])['request']
    if explicit:
        assert request['boardTaskKeys'] == []
    else:
        assert 'boardTaskKeys' not in request


def test_public_authorization_rejects_fresh_unchecked_coverage_but_replays_stored_evidence(context):
    from lib.project_dispatch.repository import ProjectDispatchError, NAMESPACE
    c = context
    request = {**c.request, 'boardTaskKeys': ['f' * 32]}
    with pytest.raises(ProjectDispatchError, match='only by scoped Coordinator delegation'):
        c.repo.authorize(request, c.principal, 'public')
    assert c.repo.list() == []
    original = c.repo.authorize(c.request, c.principal, 'public')
    stored = c.client.rows[(NAMESPACE, c.repo.key)]['value']['assignments'][0]
    stored['request']['boardTaskKeys'] = request['boardTaskKeys']
    replay = c.repo.authorize(request, c.principal, 'public')
    assert replay['id'] == original['id'] and replay['request'] == request


def test_public_http_input_matches_published_contract_and_excludes_internal_keys():
    import yaml
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from lib.project_dispatch.contract import schema
    document = yaml.safe_load(Path('contracts/api_v1.yaml').read_text())
    operation = document['paths']['/api/v4/project/coordination/assignments']['post']
    published = operation['requestBody']['content']['application/json']['schema']
    runtime = schema('publicRequest')
    assert runtime == published
    assert 'boardTaskKeys' not in runtime['properties']
    assert 'boardTaskKeys' in schema('request')['properties']
    assert any('boardTaskKeys' in error.message for error in
               Draft202012Validator(runtime).iter_errors({'boardTaskKeys': ['f' * 32]}))


def test_exact_and_history_views_preserve_frozen_binding_namespace(context):
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_dispatch.repository import NAMESPACE, _validate_schema
    c = context
    first = c.repo.authorize(c.request, c.principal, 'first')
    c.binding.value['revision'] = 2
    second = c.repo.authorize(c.request, c.principal, 'second')
    records = c.client.rows[(NAMESPACE, c.repo.key)]['value']['assignments']
    for row in records:
        row['request']['boardTaskKeys'] = ['a' * 32]
    records[0]['review'] = {'decision': 'accept', 'note': 'Reviewed prior result',
                            'resultRevision': 1}
    service = ProjectDispatchService(c.repo)
    for identity, revision in ((first['id'], 1), (second['id'], 2)):
        exact = service.view(identity, principal=c.principal)
        assert exact['bindingRevision'] == revision
        _validate_schema(exact, 'view')
    page = service.history_page(principal=c.principal)
    assert {row['assignmentId']: row['bindingRevision'] for row in page['assignments']} == {
        first['id']: 1, second['id']: 2}
    assert service.view(first['id'], principal=c.principal)['review']['decision'] == 'accept'
    del records[0]['grant']['bindingRevision']
    assert 'bindingRevision' not in service.view(first['id'], principal=c.principal)
    legacy_page = service.history_page(principal=c.principal)
    legacy = next(row for row in legacy_page['assignments'] if row['assignmentId'] == first['id'])
    assert 'bindingRevision' not in legacy
