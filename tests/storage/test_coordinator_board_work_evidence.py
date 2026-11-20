"""Positive board evidence through the ordinary segmented SQL repository."""
import pytest

from lib.conversations.coordinator_progress import CoordinatorTaskProgress, validate_progress
from lib.conversations.coordinator_work_evidence import unknown_evidence
from lib.project_dispatch.repository import ProjectDispatchRepository
from lib.project_dispatch.segmented import SegmentedDispatch
from lib.project_dispatch.service import ProjectDispatchService
from tests.storage.test_coordinator_segmented_allowance import (
    segmented_runtime, runtime, base_runtime, context)
from tests.project_mod.test_coordinator_request_budget import bind

__all__ = ('segmented_runtime', 'runtime', 'base_runtime', 'context')
pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api, pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


@pytest.fixture
def evidence(segmented_runtime, monkeypatch):
    r = segmented_runtime
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.repo.client,
        bindings=r.c.binding, notify=lambda: None, arm=lambda: None)
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    progress.publish(scope, {'summary': 'Work', 'tasks': [
        {'title': 'First outcome', 'status': 'in_progress'},
        {'title': 'Second outcome', 'status': 'pending'}]})
    keys = progress._head()['value']['taskKeys']
    # Keep command-side fixture ports, but construct the read's real repository
    # and real service; only external Turn/Attempt reads are synthetic.
    monkeypatch.setattr('lib.project_dispatch.repository.ProjectDispatchRepository',
        lambda owner, project, **kwargs: ProjectDispatchRepository(
            owner, project, conversations=r.c.conversations, **kwargs) if kwargs else r.c.repo)
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', ProjectDispatchService)
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', r.c.repo.attempt_get)
    monkeypatch.setattr(ProjectDispatchRepository, 'get_workspace', lambda *args, **kwargs: None)
    r.args['board_task_keys'] = [keys[0]]
    return r, progress, scope, keys


def _delegate(r, identity):
    outcome = r.call('delegate_project_work', **{**r.args, 'idempotency_key': identity})
    assert outcome.get('dispatch') == 'accepted', outcome
    return r.c.repo.read(outcome['assignment_id'])


def test_latest_request_preserved_with_separate_positive_previous_request(evidence, monkeypatch):
    r, progress, _, keys = evidence
    def forbidden(*args, **kwargs):
        pytest.fail('board evidence enumerated history or legacy storage')
    old = bind(r)
    previous = _delegate(r, 'previous')
    monkeypatch.setattr(SegmentedDispatch, '_legacy', forbidden)
    for name in ('history_page', 'list', 'discover'):
        monkeypatch.setattr(ProjectDispatchRepository, name, forbidden)
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'active'
    # A second accepted owner request starts while the previous Worker runs.
    r.c.turn = {**r.c.turn, 'turnId': 'second-human'}
    r.turns['second-human'] = r.c.turn
    r.turns['second-coordinator'] = {**r.turns['coordinator-turn'],
        'turnId': 'second-coordinator', 'parentTurnId': 'second-human'}
    r.attempts['second-attempt'] = {**r.attempts['coordinator-attempt'],
        'attemptId': 'second-attempt', 'turnId': 'second-coordinator', 'taskId': 'second-task'}
    r.task.update(id='second-task', _turnId='second-coordinator', _attemptId='second-attempt')
    r.task.pop('_coordinatorRequestBudget', None)
    r.task.pop('_executionBudget', None)
    latest = bind(r)
    assert latest['id'] != old['id']
    current = _delegate(r, 'current')
    state = progress.read(principal=r.c.principal)
    additional = state['workEvidence'].pop('additionalRequests')
    assert additional == [{'requestId': old['id'], 'sourceTurnId': old['authorizationSource']['turnId'],
        'rows': [{'assignmentId': previous['id'], 'conversationId': previous['request']['recipientId'],
                  'taskKeys': [keys[0]], 'phase': 'active'}]}]
    assert state['workEvidence'] == {'scope': 'latest_request', 'coverage': 'unknown',
        'requestId': latest['id'], 'sourceTurnId': 'second-human', 'rows': [
            {'assignmentId': current['id'], 'conversationId': current['request']['recipientId'],
             'taskKeys': [keys[0]], 'phase': 'active'}]}
    assert r.attempts[previous['execution']['attemptId']]['status'] == 'running'
    validate_progress(state, 'progressState')


def test_live_result_review_and_stale_revision(evidence):
    r, progress, _, _ = evidence
    bind(r)
    row = _delegate(r, 'review')
    association = row['execution']
    r.attempts[association['attemptId']]['status'] = 'completed'
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'awaiting_review'
    r.c.repo.record_review(row['id'], 'accept', 'Verified', 1, row['grant']['revision'])
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'unknown'  # no delivery receipt
    r.turns[association['turnId']]['projectionRevision'] = 2
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'unknown'


def test_omitted_keys_and_retired_binding_are_not_ownership_evidence(evidence):
    r, progress, scope, _ = evidence
    bind(r)
    _delegate(r, 'linked')
    progress.publish(scope, {'summary': 'Replaced', 'tasks': [{'title': 'Other outcome', 'status': 'pending'}]})
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'] == []
    r.c.binding.value['revision'] = 2
    assert progress.read(principal=r.c.principal)['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('phase', [None, 'copying', 'copied'])
def test_not_ready_never_loads_legacy_or_enumerates(evidence, monkeypatch, phase):
    r, progress, _, _ = evidence
    def head(store):
        return None if phase is None else {'value': {'state': {'phase': phase}}}
    monkeypatch.setattr(SegmentedDispatch, 'head', head)
    def forbidden(*args, **kwargs):
        pytest.fail('non-ready evidence attempted a cohort or legacy load')
    monkeypatch.setattr(ProjectDispatchRepository, '_load', forbidden)
    assert progress.read(principal=r.c.principal)['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('change', ['binding', 'board'])
def test_post_read_fences_concurrent_changes(evidence, monkeypatch, change):
    r, progress, scope, _ = evidence
    bind(r)
    _delegate(r, 'racing')
    original = ProjectDispatchService._view
    def view(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        if change == 'binding':
            r.c.binding.value['revision'] += 1
        else:
            progress.publish(scope, {'summary': 'Changed during read', 'tasks': []})
        return result
    monkeypatch.setattr(ProjectDispatchService, '_view', view)
    assert progress.read(principal=r.c.principal)['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('missing', ['turn', 'attempt'])
def test_missing_bound_execution_receipt_is_unknown_not_queued(evidence, missing):
    r, progress, _, _ = evidence
    bind(r)
    row = _delegate(r, 'missing')
    if missing == 'turn':
        r.turns.pop(row['execution']['turnId'])
    else:
        r.attempts.pop(row['execution']['attemptId'])
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'unknown'


@pytest.mark.parametrize('access', ['absent', 'conversations', 'foreign_owner', 'foreign_tenant', 'system'])
def test_evidence_preserves_dispatch_access(evidence, monkeypatch, access):
    from dataclasses import replace
    r, progress, _, _ = evidence
    bind(r)
    _delegate(r, 'private')
    principal = r.c.principal
    if access == 'absent':
        principal = None
    elif access == 'conversations':
        principal = replace(principal, scopes=frozenset({'conversations'}))
    elif access == 'foreign_owner':
        principal = replace(principal, owner_user_id=8)
    elif access == 'foreign_tenant':
        principal = replace(principal, tenant_id='other')
    else:
        principal = replace(principal, kind='system')
    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized principal hydrated a Worker receipt')
    monkeypatch.setattr(ProjectDispatchService, '_view', forbidden)
    result = progress.read(principal=principal)
    assert result['board'] is not None
    assert result['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('status, expected', [('working', 'awaiting_delivery'),
    ('pending', 'awaiting_delivery'), ('blocked', 'held'), ('failed', 'held'),
    ('preserved', 'held'), ('delivered', 'delivered')])
@pytest.mark.parametrize('receipt_checkpoint', ['exact', 'missing', 'empty', 'conflict'])
@pytest.mark.parametrize('workspace_checkpoint', ['pinned', 'newer'])
def test_exact_workspace_receipts_drive_delivery_phase(evidence, monkeypatch, status, expected,
                                                      receipt_checkpoint, workspace_checkpoint):
    from lib.project_dispatch.admission import KEY
    r, progress, _, _ = evidence
    bind(r)
    row = _delegate(r, 'delivery')
    association = row['execution']
    attempt = r.attempts[association['attemptId']]
    attempt['status'] = 'completed'
    r.c.repo.record_review(row['id'], 'accept', 'Verified', 1, row['grant']['revision'])
    checkpoint = 'c' * 40
    def publication(value):
        stored = next(item for item in value['assignments'] if item['id'] == row['id'])
        stored['publication'] = {'resultRevision': 1, 'grantRevision': row['grant']['revision'],
            'checkpointSha': checkpoint, 'subject': 'coordinator:' + row['request']['delegation']['taskId'],
            'requestedAt': r.c.repo.clock()}
    r.c.repo._change(publication, selected=r.c.repo._point_selection(row['id']))
    workspace = {'user_id': 7, 'project_root': '/project',
        'task_id': association.get('taskId') or attempt['taskId'],
        'origin': {KEY: r.c.repo.admission(row['id']),
                   'delivery': {'state': status, 'checkpointSha': checkpoint}}}
    workspace['checkpoint_sha'] = checkpoint if workspace_checkpoint == 'pinned' else 'e' * 40
    if receipt_checkpoint == 'missing':
        workspace['origin']['delivery'].pop('checkpointSha')
    elif receipt_checkpoint == 'empty':
        workspace['origin']['delivery']['checkpointSha'] = ''
    elif receipt_checkpoint == 'conflict':
        workspace['origin']['delivery']['checkpointSha'] = 'd' * 40
    if status == 'blocked':
        workspace['origin']['delivery']['reason'] = 'project_publication_authority_missing'
    if status == 'delivered':
        workspace['origin']['delivery'].update(targetSha='a' * 40)
    monkeypatch.setattr(ProjectDispatchRepository, 'get_workspace', lambda *a, **kw: workspace)
    def forbidden(*args, **kwargs):
        pytest.fail('passive board read performed live publication validation, a mutation or a command')
    monkeypatch.setattr('lib.project_dispatch.publication._publication_verdict', forbidden)
    monkeypatch.setattr(ProjectDispatchRepository, 'mutate', forbidden)
    monkeypatch.setattr(r.c.repo.client(), 'command', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    view = ProjectDispatchService(ProjectDispatchRepository(7, '/project', client=r.c.repo.client,
        conversations=r.c.conversations, binding_repository=r.c.binding))._view(r.c.repo.read(row['id']),
            settlement_only=True, diagnostic_receipts=True)
    assert view['delivery'] is not None, view
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == (
        expected if receipt_checkpoint == 'exact' else 'unknown')
    r.turns[association['turnId']]['projectionRevision'] = 2
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'unknown'
    r.turns[association['turnId']]['projectionRevision'] = 1
    workspace['origin'][KEY]['assignmentId'] = 'another-assignment'
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'][0]['phase'] == 'unknown'


@pytest.mark.parametrize('count', [1, 65, 193])
def test_receipt_hydration_is_one_admission_ordered_storage_batch(evidence, monkeypatch, count):
    from lib.storage.generated_project_dispatch_contract import SPEC
    r, progress, _, _ = evidence
    bind(r)
    from copy import deepcopy
    from lib.storage import port
    template = _delegate(r, 'batch-template')
    rows = [template]
    for start in range(1, count, 16):
        additions = []
        for index in range(start, min(start + 16, count)):
            row = deepcopy(template)
            row['id'] = f'bounded-page-{index}'
            additions.append(row)
        r.c.repo._change(lambda value: value['assignments'].extend(additions),
            selected=r.c.repo._selection(budgets=[template['request']['delegation']['policy']['requestBudgetId']],
                cohort=False, dependencies=False, include_policy=False))
        rows.extend(additions)
    reads, batches = [], []
    client = r.c.repo.client()
    original_query, original_many = client.query, port.query_many
    def query(operation, payload, **kwargs):
        reads.append((operation, payload))
        return original_query(operation, payload, **kwargs)
    def query_many(client, requests, **kwargs):
        if len(requests) > 1:
            reads.extend(requests)
        batches.append(list(requests))
        return original_many(client, requests, **kwargs)
    monkeypatch.setattr(client, 'query', query)
    monkeypatch.setattr(port, 'query_many', query_many)
    def forbidden(*args, **kwargs):
        pytest.fail('board attempted legacy enumeration or mutation')
    monkeypatch.setattr(SegmentedDispatch, '_legacy', forbidden)
    monkeypatch.setattr(ProjectDispatchRepository, '_load', forbidden)
    monkeypatch.setattr(ProjectDispatchRepository, 'mutate', forbidden)
    observed = []
    original = ProjectDispatchService._view
    def view(service, row, **kwargs):
        assert kwargs == {'settlement_only': True, 'diagnostic_receipts': True}
        observed.append(row['id'])
        return original(service, row, **kwargs)
    monkeypatch.setattr(ProjectDispatchService, '_view', view)
    result = progress.read(principal=r.c.principal)['workEvidence']
    eligible = rows[:SPEC['limits']['pageRows']]
    assert observed == [row['id'] for row in eligible]
    lists = [payload for operation, payload in reads if operation == 'project.dispatch.list']
    assert len(lists) == 2
    assert lists[1] == {'user_id': 7, 'project_path': '/project', 'collection': 'indexes',
        'prefix': 'assignments:order:', 'after_key': '', 'reverse': True, 'limit': SPEC['limits']['pageRows']}
    assert lists[0]['collection'] == 'indexes'
    assert lists[0]['prefix'].startswith('assignments:budget:')
    assert lists[0]['after_key'] == '' and lists[0]['limit'] == SPEC['limits']['pageRows']
    budget_gets = [payload for operation, payload in reads
        if operation == 'project.dispatch.get' and payload['collection'] == 'requests']
    assert len(budget_gets) == 1
    head_gets = [payload for operation, payload in reads
        if operation == 'project.dispatch.get' and payload['collection'] == 'head']
    assert len(head_gets) == 2
    assignment_gets = [payload for operation, payload in reads
        if operation == 'project.dispatch.get' and payload['collection'] == 'assignments']
    from lib.project_dispatch.segmented import key
    newest = list(reversed(rows[-SPEC['limits']['pageRows']:]))
    assert [payload['id'] for payload in assignment_gets] == [key(row['id']) for row in eligible + newest]
    assignment_batches = [batch for batch in batches if any(
        payload.get('collection') == 'assignments' for _, payload in batch)]
    assert len(assignment_batches) == 2
    assert len(assignment_batches[0]) == len(eligible)
    assert len(assignment_batches[1]) == len(newest)
    assert [row['assignmentId'] for row in result['rows']] == observed
    assert result['coverage'] == 'unknown'


@pytest.mark.parametrize('legacy_field', ['boardTaskKeys', 'bindingRevision'])
def test_unlinked_or_epochless_historical_rows_never_project_positive_links(evidence, legacy_field):
    r, progress, _, _ = evidence
    bind(r)
    if legacy_field == 'boardTaskKeys':
        r.args.pop('board_task_keys')
    row = _delegate(r, 'historical')
    if legacy_field == 'boardTaskKeys':
        assert progress.read(principal=r.c.principal)['workEvidence']['rows'] == []
        return
    def historical(value):
        stored = next(item for item in value['assignments'] if item['id'] == row['id'])
        stored['grant'].pop('bindingRevision')
    r.c.repo._change(historical, selected=r.c.repo._point_selection(row['id']))
    assert progress.read(principal=r.c.principal)['workEvidence']['rows'] == []


def test_request_accepted_during_hydration_retires_latest_request_evidence(evidence, monkeypatch):
    r, progress, _, _ = evidence
    bind(r)
    _delegate(r, 'first-request')
    original = ProjectDispatchService._view
    def view(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        r.c.turn = {**r.c.turn, 'turnId': 'concurrent-human'}
        r.turns['concurrent-human'] = r.c.turn
        r.turns['concurrent-coordinator'] = {**r.turns['coordinator-turn'],
            'turnId': 'concurrent-coordinator', 'parentTurnId': 'concurrent-human'}
        r.attempts['concurrent-attempt'] = {**r.attempts['coordinator-attempt'],
            'attemptId': 'concurrent-attempt', 'turnId': 'concurrent-coordinator', 'taskId': 'concurrent-task'}
        r.task.update(id='concurrent-task', _turnId='concurrent-coordinator', _attemptId='concurrent-attempt')
        r.task.pop('_coordinatorRequestBudget', None)
        r.task.pop('_executionBudget', None)
        bind(r)
        return result
    monkeypatch.setattr(ProjectDispatchService, '_view', view)
    assert progress.read(principal=r.c.principal)['workEvidence'] == unknown_evidence()


@pytest.mark.parametrize('missing', ['publication', 'result', 'grant', 'checkpoint'])
def test_delivered_projection_requires_exact_current_publication_pins(missing, semantic_authority):
    from lib.conversations.coordinator_work_evidence import _phase
    view = {'execution': {'status': 'completed', 'resultRevision': 2},
        'review': {'decision': 'accept', 'resultRevision': 2}, 'revoked': False,
        'grantRevision': 3, 'canonicalDelivery': True,
        'delivery': {'status': 'delivered', 'checkpointSha': 'current'},
        'publication': {'resultRevision': 2, 'grantRevision': 3, 'checkpointSha': 'current'}}
    assert _phase(view) == 'delivered'
    if missing == 'publication':
        view['publication'] = None
    else:
        field = {'result': 'resultRevision', 'grant': 'grantRevision', 'checkpoint': 'checkpointSha'}[missing]
        view['publication'][field] = 'old'
    assert _phase(view) == 'unknown'


@pytest.mark.parametrize('corruption', ['sequence', 'request', 'schema'])
def test_bounded_page_rejects_invalid_indexed_documents(evidence, monkeypatch, corruption):
    from copy import deepcopy
    r, progress, _, _ = evidence
    bind(r)
    row = _delegate(r, 'invalid-indexed-document')
    original = SegmentedDispatch.get_many
    def get_many(store, documents):
        answers = original(store, documents)
        for (field, ident), answer in answers.items():
            if field != 'assignments' or ident != row['id']:
                continue
            answer = deepcopy(answer)
            answers[field, ident] = answer
            if corruption == 'sequence':
                answer['value']['sequence'] += 1
            elif corruption == 'request':
                answer['value']['record']['request']['delegation']['policy']['requestBudgetId'] = 'another-request'
            else:
                answer['value']['record']['execution'] = {'turnId': 42}
        return answers
    monkeypatch.setattr(SegmentedDispatch, 'get_many', get_many)
    assert progress.read(principal=r.c.principal)['workEvidence'] == unknown_evidence()
