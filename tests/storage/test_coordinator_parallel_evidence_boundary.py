"""Positive-only parallel receipts through ordinary SQLite request/admission owners."""
from copy import deepcopy

import pytest

from lib.conversations.coordinator_work_evidence import _phase, unknown_evidence
from lib.project_dispatch.repository import ProjectDispatchRepository
from lib.project_dispatch.segmented import SegmentedDispatch, key
from lib.project_dispatch.service import ProjectDispatchService
from tests.storage.test_coordinator_board_work_evidence import (
    evidence, segmented_runtime, runtime, base_runtime, context, _delegate)
from tests.project_mod.test_coordinator_request_budget import bind

__all__ = ('evidence', 'segmented_runtime', 'runtime', 'base_runtime', 'context')
pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api, pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


@pytest.fixture
def parallel(evidence):
    r, progress, scope, keys = evidence
    first = bind(r)
    older = _delegate(r, 'older-request')
    r.older_task = dict(r.task)
    r.c.turn = {**r.c.turn, 'turnId': 'second-human'}
    r.turns['second-human'] = r.c.turn
    r.turns['second-coordinator'] = {**r.turns['coordinator-turn'],
        'turnId': 'second-coordinator', 'parentTurnId': 'second-human'}
    r.attempts['second-attempt'] = {**r.attempts['coordinator-attempt'],
        'attemptId': 'second-attempt', 'turnId': 'second-coordinator'}
    r.task.update(_turnId='second-coordinator', _attemptId='second-attempt')
    r.task.pop('_coordinatorRequestBudget', None)
    r.task.pop('_executionBudget', None)
    second = bind(r)
    latest = _delegate(r, 'latest-request')
    assert first['id'] != second['id']
    assert first['accounts'][0]['taskId'] == second['accounts'][0]['taskId']
    assert first['accounts'][0]['attemptId'] != second['accounts'][0]['attemptId']
    assert older['request']['boardTaskKeys'] == latest['request']['boardTaskKeys'] == keys[:1]
    for row in (older, latest):
        assert r.attempts[row['execution']['attemptId']]['status'] == 'running'
        assert not r.c.repo.request_rows(row['request']['delegation']['attemptId'])['settled']
    return r, progress, scope, keys, first, second, older, latest


def _additional(state):
    return state['workEvidence'].get('additionalRequests', [])


def _finish(r):
    from lib.project_dispatch.continuation import finish_request
    r.turns['decision-report'] = {'turnId': 'decision-report', 'conversationId': 'coordinator',
        'actor': 'assistant', 'kind': 'coordinator_report.decision',
        'createdAt': int(r.c.now[0] * 1000) + 1,
        'projection': {'origin': {'initiator': 'brain'}, 'content': 'Owner decision needed'}}
    finish_request(r.c.repo, r.older_task,
        {'report_turn_id': 'decision-report', 'outcome': 'needs_user'})


def test_bounded_order_page_discovers_second_open_request_without_supplied_ids(parallel, monkeypatch):
    from lib.storage.generated_project_dispatch_contract import SPEC
    r, progress, _, keys, first, second, older, latest = parallel
    reads, hydrated = [], []
    store, view = SegmentedDispatch.query, ProjectDispatchService._view
    def query(instance, operation, **payload):
        reads.append((operation, deepcopy(payload)))
        return store(instance, operation, **payload)
    def observe(service, row, **kwargs):
        hydrated.append(row['id'])
        return view(service, row, **kwargs)
    def forbidden(*args, **kwargs):
        pytest.fail('passive evidence used history/cohort/dependencies, legacy, mutation or effect')
    monkeypatch.setattr(SegmentedDispatch, 'query', query)
    monkeypatch.setattr(ProjectDispatchService, '_view', observe)
    for name in ('history_page', 'list', 'discover', 'mutate', '_load', 'request_rows'):
        monkeypatch.setattr(ProjectDispatchRepository, name, forbidden)
    monkeypatch.setattr(SegmentedDispatch, '_legacy', forbidden)
    monkeypatch.setattr(r.c.repo.client(), 'command', forbidden)
    result = progress.read(principal=r.c.principal)['workEvidence']
    assert result['requestId'] == second['id'] and result['sourceTurnId'] == 'second-human'
    assert result['scope'] == 'latest_request' and result['coverage'] == 'unknown'
    assert [row['assignmentId'] for row in result['rows']] == [latest['id']]
    assert hydrated == [latest['id'], older['id'], older['id']]
    assert result['additionalRequests'] == [{'requestId': first['id'],
        'sourceTurnId': first['authorizationSource']['turnId'], 'rows': [{
            'assignmentId': older['id'], 'conversationId': older['request']['recipientId'],
            'taskKeys': keys[:1], 'phase': 'active'}]}]
    lists = [payload for operation, payload in reads if operation == 'list']
    assert len(lists) == 2
    assert lists[0]['prefix'] == 'assignments:budget:' + key(second['id']) + ':'
    assert lists[1] == {'collection': 'indexes', 'prefix': 'assignments:order:',
        'after_key': '', 'reverse': True, 'limit': SPEC['limits']['pageRows']}


@pytest.mark.parametrize('change', ['missing_attempt', 'stale_source', 'held', 'interrupted', 'revived_board'])
def test_older_receipts_follow_exact_source_hold_and_current_board(parallel, change):
    from lib.turn_verdict import LEAD_HOLD
    r, progress, scope, keys, first, _, older, latest = parallel
    before = progress._head()
    if change == 'missing_attempt':
        r.attempts.pop(older['execution']['attemptId'])
    elif change == 'stale_source':
        r.turns[first['authorizationSource']['turnId']]['projectionRevision'] += 1
    elif change in ('held', 'interrupted'):
        r.attempts[older['execution']['attemptId']]['status'] = 'interrupted'
        if change == 'held':
            r.turns[older['execution']['turnId']]['settlement'] = {'cause': LEAD_HOLD}
    else:
        progress.publish(scope, {'summary': 'Departed', 'tasks': []})
        assert _additional(progress.read(principal=r.c.principal)) == []
        progress.publish(scope, {'summary': 'Revived', 'tasks': [
            {'title': 'First outcome', 'status': 'in_progress'},
            {'title': 'Second outcome', 'status': 'pending'}]})
        assert progress._head()['value']['taskKeys'] == keys
        assert progress._head()['version'] != before['version']
    after = progress.read(principal=r.c.principal)
    assert [row['assignmentId'] for row in after['workEvidence']['rows']] == [latest['id']]
    assert after['workEvidence']['coverage'] == 'unknown'
    if change in ('stale_source', 'interrupted'):
        assert _additional(after) == []
    else:
        assert _additional(after)[0]['rows'][0]['phase'] == {
            'missing_attempt': 'unknown', 'held': 'held', 'revived_board': 'active'}[change]
    if change == 'interrupted':
        assert _phase(r.service._view(older, settlement_only=True, diagnostic_receipts=True)) == 'ended'


def test_settled_needs_user_request_with_running_worker_is_not_eligible(parallel):
    r, progress, _, _, _, _, older, _ = parallel
    assert _additional(progress.read(principal=r.c.principal))
    _finish(r)
    assert r.c.repo.request_rows(older['request']['delegation']['attemptId'])['settled']
    assert r.attempts[older['execution']['attemptId']]['status'] == 'running'
    assert _phase(r.service._view(older, settlement_only=True, diagnostic_receipts=True)) == 'active'
    assert _additional(progress.read(principal=r.c.principal)) == []


@pytest.mark.parametrize('change', ['settlement', 'request', 'assignment', 'source', 'board', 'binding', 'attempt'])
def test_post_hydration_fences_reject_stale_additional_receipts(parallel, monkeypatch, change):
    r, progress, scope, _, first, _, older, _ = parallel
    original = ProjectDispatchService._view
    fired = False
    def view(service, row, **kwargs):
        nonlocal fired
        result = original(service, row, **kwargs)
        if row['id'] != older['id'] or fired:
            return result
        fired = True
        if change == 'attempt':
            r.attempts[older['execution']['attemptId']]['status'] = 'interrupted'
        elif change == 'settlement':
            _finish(r)
        elif change == 'source':
            r.turns[first['authorizationSource']['turnId']]['projectionRevision'] += 1
        elif change == 'board':
            progress.publish(scope, {'summary': 'Changed while hydrating', 'tasks': []})
        elif change == 'binding':
            r.c.binding.value['revision'] += 1
        else:
            from lib.project_dispatch.segmented import digest
            field, ident = ('requestBudgets', first['id']) if change == 'request' else ('assignments', older['id'])
            document = SegmentedDispatch(r.c.repo).get_many([(field, ident)])[field, ident]
            value = deepcopy(document['value'])
            if change == 'request':
                value['record']['accounts'][0]['toolCalls'] += 1
            else:
                value['record']['grant']['revision'] += 1
                value['record']['grant']['revoked'] = True
            r.c.repo.client(write=True).command('project.dispatch.commit', {
                'user_id': r.c.repo.owner, 'project_path': r.c.repo.project,
                'changes': [{'collection': 'requests' if change == 'request' else 'assignments',
                    'id': key(ident), 'expected_version': document['version'], 'value': value}]},
                'fixture-concurrent-change-' + digest(value))
        return result
    monkeypatch.setattr(ProjectDispatchService, '_view', view)
    result = progress.read(principal=r.c.principal)
    assert _additional(result) == []
    if change in ('board', 'binding'):
        assert result['workEvidence'] == unknown_evidence()
    else:
        assert result['workEvidence']['requestId'] is not None


def test_same_assignment_work_task_with_distinct_attempt_uses_current_receipt(parallel):
    r, progress, _, _, _, _, older, _ = parallel
    before = r.c.repo.assignment_version(older['id'])
    previous = older['execution']
    r.attempts[previous['attemptId']]['status'] = 'interrupted'
    successor = 'older-resumed-attempt'
    r.attempts[successor] = {**r.attempts[previous['attemptId']],
        'attemptId': successor, 'status': 'running'}
    r.c.repo.continue_execution(older['id'], previous, previous['turnId'], successor)
    current = r.c.repo.read_point(older['id'])
    assert current['execution']['taskId'] == previous['taskId']
    assert current['execution']['attemptId'] != previous['attemptId']
    assert r.c.repo.assignment_version(older['id']) != before
    assert _additional(progress.read(principal=r.c.principal))[0]['rows'][0]['phase'] == 'active'
    r.attempts[successor]['status'] = 'interrupted'
    assert _additional(progress.read(principal=r.c.principal)) == []


@pytest.mark.parametrize('scope_change', ['tenant', 'binding', 'project'])
def test_unrelated_scope_exposes_no_receipts(parallel, scope_change):
    from dataclasses import replace
    from types import SimpleNamespace
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    r, progress, _, _, _, _, _, _ = parallel
    principal = r.c.principal
    if scope_change == 'tenant':
        principal = replace(principal, tenant_id='unrelated')
    elif scope_change == 'binding':
        r.c.binding.value['revision'] += 1
    else:
        binding = SimpleNamespace(read=lambda project: {**r.c.binding.value, 'projectPath': project})
        progress = CoordinatorTaskProgress(7, '/unrelated', client=r.c.repo.client,
            bindings=binding, notify=lambda: None, arm=lambda: None)
    assert progress.read(principal=principal)['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('unrelated', [1, 65])
def test_unrelated_newest_rows_never_trigger_page_continuation_or_absence_claim(parallel, monkeypatch, unrelated):
    from lib.storage.generated_project_dispatch_contract import SPEC
    r, progress, _, _, _, _, older, latest = parallel
    for start in range(0, unrelated, 16):
        additions = []
        for index in range(start, min(start + 16, unrelated)):
            row = deepcopy(latest)
            row['id'] = f'unrelated-{index}'
            row['request']['boardTaskKeys'] = []
            additions.append(row)
        r.c.repo._change(lambda value: value['assignments'].extend(additions),
            selected=r.c.repo._selection(budgets=[latest['request']['delegation']['policy']['requestBudgetId']],
                cohort=False, dependencies=False, include_policy=False))
    calls = []
    original = SegmentedDispatch.query
    def query(store, operation, **payload):
        if operation == 'list':
            calls.append(payload)
        return original(store, operation, **payload)
    monkeypatch.setattr(SegmentedDispatch, 'query', query)
    result = progress.read(principal=r.c.principal)
    assert len(calls) == 2
    assert all(call['after_key'] == '' and call['limit'] == SPEC['limits']['pageRows'] for call in calls)
    assert result['workEvidence']['coverage'] == 'unknown'
    if unrelated == 1:
        assert _additional(result)[0]['rows'][0]['assignmentId'] == older['id']
    else:
        assert _additional(result) == []
        assert r.attempts[older['execution']['attemptId']]['status'] == 'running'


@pytest.mark.parametrize('damage', ['missing', 'sequence', 'schema'])
def test_damaged_additional_document_keeps_latest_positive_and_unknown_coverage(parallel, monkeypatch, damage):
    r, progress, _, _, _, _, older, latest = parallel
    original = SegmentedDispatch.get_many
    def get_many(store, points):
        answers = original(store, points)
        point = ('assignments', older['id'])
        if point in answers:
            document = deepcopy(answers[point])
            if damage == 'missing':
                document = None
            elif damage == 'sequence':
                document['value']['sequence'] += 1
            else:
                document['value']['record']['execution'] = {'attemptId': 5}
            answers[point] = document
        return answers
    monkeypatch.setattr(SegmentedDispatch, 'get_many', get_many)
    result = progress.read(principal=r.c.principal)
    assert _additional(result) == []
    assert result['workEvidence']['coverage'] == 'unknown'
    assert result['workEvidence']['rows'][0]['assignmentId'] == latest['id']
