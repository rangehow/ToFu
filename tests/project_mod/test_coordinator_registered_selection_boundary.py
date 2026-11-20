"""Executable input qualification for passive registered-work recommendations."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.project_dispatch import continuation
from lib.project_dispatch.publication import delivery_view
from tests.project_mod.test_coordinator_continuation import (
    run, runtime, base_runtime, context, delegate, end, report, _bound_request, _decision_report,
)
from tests.project_mod.test_project_dispatch_dependencies import (
    dependency_context, completed, dependent, reference,
)
__all__ = ['run', 'dependency_context']

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def injected_storage_only(monkeypatch):
    from lib.storage import service as storage_service
    monkeypatch.setattr(storage_service, '_runtime', SimpleNamespace(
        client=lambda **kwargs: pytest.fail('Synthetic readiness must use only injected storage ports')))


def _registered_child(r, parent, command):
    checkpoint = 'a' * 40
    request = deepcopy(parent['request'])
    recipient = 'candidate-' + command
    r.c.conversations.rows[recipient] = {'user_id': 7, 'settings': {'projectPath': '/project'}}
    request['recipientId'] = recipient
    request['dependencies'] = [{'assignmentId': parent['id'], 'grantRevision': 1,
                                'resultRevision': 1, 'checkpointSha': checkpoint}]
    return r.c.repo.authorize_from_coordinator(request, task=r.task, command_id=command)


def _pin_result(r, parent):
    end(r, parent)
    workspace = r.workspaces[parent['execution']['taskId']]
    workspace['checkpoint_sha'] = 'a' * 40
    workspace['origin']['delivery']['checkpointSha'] = 'a' * 40


def _accept_result(r, parent):
    from lib.project_dispatch.delegation import review_worker
    review_worker(r.c.repo, r.task, {'assignment_id': parent['id'], 'decision': 'accept',
        'note': 'Exact synthetic result', 'result_revision': 1, 'checkpoint_sha': 'a' * 40})


@pytest.mark.parametrize('third_state', ['waiting_review', 'unknown', 'stale_result', 'stale_checkpoint'])
def test_current_readiness_and_settlement_projection_discriminate_only_with_full_evidence(run, third_state):
    r = run
    ready_parent, waiting_parent = delegate(r, 'ready-parent'), delegate(r, 'waiting-parent')
    for parent in (ready_parent, waiting_parent):
        _pin_result(r, parent)
    first = _registered_child(r, ready_parent, 'ready-a')
    second = _registered_child(r, ready_parent, 'ready-b')
    third = _registered_child(r, waiting_parent, 'waiting-c')
    _accept_result(r, ready_parent)
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator',
             'binding_revision': 1}

    def projection():
        return r.service.view_for_coordinator(third['id'], scope=scope, settlement_only=True)

    waiting_view = projection()
    if third_state != 'waiting_review':
        _accept_result(r, waiting_parent)
        assert r.c.repo.dependency_states(third)[0]['status'] == 'ready'
        assert projection() == waiting_view
    if third_state == 'unknown':
        r.workspaces.pop(waiting_parent['execution']['taskId'])
    elif third_state == 'stale_result':
        r.turns[waiting_parent['execution']['turnId']]['projectionRevision'] = 2
    elif third_state == 'stale_checkpoint':
        r.workspaces[waiting_parent['execution']['taskId']]['checkpoint_sha'] = 'b' * 40

    selected = r.c.repo._selection(sources=['coordinator-attempt'])
    assert selected['include_policy'] and selected['dependencies']
    value, _ = r.c.repo._load(selected)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    for _ in range(2):
        shown = continuation.pre_park_witness(r.c.repo, value, 'coordinator-attempt')
        assert shown['ready'] == [first['id'], second['id']]
        bucket = 'blocked' if third_state == 'waiting_review' else 'unknown'
        assert shown[bucket] == [third['id']]
        assert shown['unknown' if bucket == 'blocked' else 'blocked'] == []
        assert shown['request_disposition'] == 'open'
        assert shown['unregistered_intentions'] == 'unknown'
        assert continuation.pre_park_witness(r.c.repo, value, 'other-request')['ready'] == []
        assert projection() == waiting_view
    assert waiting_view['execution'] is None and 'dependencyStates' not in waiting_view
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert all(r.c.repo.read(row['id'])['execution'] is None for row in (first, second, third))
    assert not r.scheduled and not r.reports


@pytest.mark.parametrize('disposition', ['owner_decision', 'settled'])
def test_full_request_disposition_overrides_ready_prerequisites(run, monkeypatch, disposition):
    r = run
    _bound_request(r)
    parent = delegate(r)
    _pin_result(r, parent)
    child = _registered_child(r, parent, 'ready-before-disposition')
    _accept_result(r, parent)
    assert r.c.repo.dependency_states(child)[0]['status'] == 'ready'
    if disposition == 'owner_decision':
        monkeypatch.setattr(continuation, 'schedule_wake', lambda *args, **kwargs: None)
        result = r.call('request_user_decision', report_turn_id=_decision_report(r),
                        recommendation='Keep the protected path waiting')
        assert 'error' not in result
    else:
        assert r.call('finish_project_request', **report(r, 'blocked'))['reason'] == 'needs_user'
    selected = r.c.repo._selection(sources=['coordinator-attempt'])
    value, _ = r.c.repo._load(selected)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    shown = continuation.pre_park_witness(r.c.repo, value, 'coordinator-attempt')
    assert shown['request_disposition'] == disposition
    assert shown['ready'] == shown['blocked'] == shown['unknown'] == []
    assert shown['unregistered_intentions'] == 'unknown'
    assert r.c.repo.read(child['id'])['execution'] is None
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert not r.scheduled and not r.reports


class MissingNonEffectfulReadinessProfile(AssertionError):
    pass


@pytest.mark.xfail(strict=True, raises=MissingNonEffectfulReadinessProfile,
                   reason='Dependency delivery projection has no guaranteed non-effectful read profile')
def test_held_receipt_readiness_must_not_enter_durable_intent_settlement(dependency_context, monkeypatch):
    c = dependency_context
    prior = completed(c, files=True, reviewed=True)
    child = dependent(c, [reference(prior)])
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    prior = c.repo.read(prior['id'])
    receipt = c.workspaces['task-prerequisite']['origin']['delivery']
    receipt['reason'] = 'project_publication_authority_missing'
    snapshot, _ = c.repo._load(c.repo._selection(assignments=[child['id']]))

    # A later canonical row may differ from the dependency snapshot's row.
    c.repo.mutate(prior['id'], lambda row: row['modelAttempts'].append({
        'root_task_id': 'task-prerequisite', 'attempt_ordinal': 1, 'attempt_task_id': 'physical',
        'reserved_tokens': 10, 'phase': 'dispatch_intent', 'reported_tokens': 0,
        'usage_complete': False, 'account_nonce': '0123456789abcdef0123456789abcdef',
        'account_observed_tokens': 0}))
    c.repo.model_lease_open = lambda *args: False
    before, puts = deepcopy(c.repo._load()[0]), c.client.puts
    mutations = []

    def forbid_mutation(assignment_id, *args, **kwargs):
        mutations.append(assignment_id)
        raise MissingNonEffectfulReadinessProfile('Readiness entered a durable mutation port')

    with monkeypatch.context() as patch:
        patch.setattr(c.repo, 'mutate', forbid_mutation)
        diagnostic = delivery_view(prior, c.attempts['attempt-prerequisite'],
                                   c.repo.get_workspace, diagnostic=True)
        assert diagnostic['status'] == 'blocked' and not mutations
        states = c.repo.dependency_states(child, value=snapshot)
    assert states[0]['status'] != 'ready'
    assert c.repo._load()[0] == before and c.client.puts == puts
    assert not c.births
    if mutations:
        assert mutations == [prior['id']]
        raise MissingNonEffectfulReadinessProfile(
            'Diagnostic receipt projection stays passive, but current dependency readiness '
            'revalidates the held publication and enters durable intent settlement. '
            'The tripwire prevented the write; no passive runtime consumer is admitted.')


@pytest.mark.xfail(strict=True, raises=MissingNonEffectfulReadinessProfile,
                   reason='Denied-finish diagnostic can settle a prerequisite model intent')
def test_denied_finish_diagnostic_must_not_write_prerequisite_ledger(run, monkeypatch):
    r = run
    parent = delegate(r)
    _pin_result(r, parent)
    child = _registered_child(r, parent, 'denied-finish-child')
    _accept_result(r, parent)
    args, calls = report(r), list(r.calls)
    witness = continuation.pre_park_witness
    observations = []
    r.c.repo.model_lease_open = lambda *args: False

    def observe(repository, value, source_attempt):
        assert source_attempt == 'coordinator-attempt'
        captured = next(row for row in value['assignments'] if row['id'] == parent['id'])
        assert captured['modelAttempts'] == []
        # The finish caller has already read the parent's delivered result.
        r.workspaces[parent['execution']['taskId']]['origin']['delivery'].update(
            state='blocked', reason='project_publication_authority_missing')
        repository.mutate(parent['id'], lambda row: row['modelAttempts'].append({
            'root_task_id': parent['execution']['taskId'], 'attempt_ordinal': 1,
            'attempt_task_id': 'physical', 'reserved_tokens': 10, 'phase': 'dispatch_intent',
            'reported_tokens': 0, 'usage_complete': False,
            'account_nonce': '0123456789abcdef0123456789abcdef', 'account_observed_tokens': 0}))
        before, puts = deepcopy(repository._load()[0]), r.c.client.puts
        shown = witness(repository, value, source_attempt)
        observations.append((before, deepcopy(repository._load()[0]), r.c.client.puts - puts))
        return shown

    monkeypatch.setattr(continuation, 'pre_park_witness', observe)
    result = r.call('finish_project_request', **args)
    assert result['execution_started'] is False and 'Unfinished/unreviewed' in result['error']
    shown = json.loads(result['error'].split(' Registered work diagnostic: ')[1])
    assert shown['ready'] == []
    assert child['id'] in shown['blocked'] + shown['unknown']
    assert len(observations) == 1
    before, after, writes = observations[0]
    assert r.c.repo._load()[0] == after
    assert after.get('coordinatorSettlements', []) == []
    assert r.c.repo.read(child['id'])['execution'] is None and r.calls == calls
    assert not r.scheduled and not r.reports
    if writes:
        expected = deepcopy(before)
        changed = next(row for row in expected['assignments'] if row['id'] == parent['id'])
        changed['modelAttempts'][0]['phase'] = 'settled'
        assert writes == 1 and after == expected
        raise MissingNonEffectfulReadinessProfile(
            'Denied finish creates no completion receipt or child execution, but its original '
            'diagnostic evaluation writes one prerequisite ledger intent from dispatch_intent '
            'to settled in injected storage; no nonmutating runtime profile is installed.')
    assert after == before
