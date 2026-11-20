"""Incomplete finish exposes observations, without execution or settlement."""
from copy import deepcopy
import json

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run, runtime, base_runtime, context, ready, activate, delegate, end, report, controls,
)
from lib.project_dispatch import continuation
__all__ = ['run']

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def lane_hints(monkeypatch):
    monkeypatch.setattr(continuation, '_LANE_HOLDS', {})
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})


def queued_stage(r, monkeypatch, key='stage-b'):
    dispatch = r.service.dispatch_authorized
    monkeypatch.setattr(r.service, 'dispatch_authorized', lambda binding: {'dispatch': 'deferred'})
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': key})
    monkeypatch.setattr(r.service, 'dispatch_authorized', dispatch)
    assert result['dispatch'] == 'deferred', result
    return r.c.repo.read(result['assignment_id'])


def diagnostic(result):
    assert result['execution_started'] is False
    assert 'Unfinished/unreviewed' in result['error']
    assert 'not permission' in result['error']
    return json.loads(result['error'].split(' Registered work diagnostic: ')[1])


def test_finish_ready_observation_is_read_only_repeatable_and_request_scoped(run, monkeypatch):
    r = run
    ready(r)
    activate(r)
    stage = queued_stage(r, monkeypatch)
    args = report(r)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.tools.result_value import tool_result_failure_verdict
    for _ in range(2):
        result = execute_coordinator_tool(r.task, 'finish_project_request', args)
        assert tool_result_failure_verdict(result) == 'error'
        shown = diagnostic(json.loads(result))
        assert shown == {'ready': [stage['id']], 'blocked': [], 'unknown': [],
                         'unregistered_intentions': 'unknown', 'request_disposition': 'open'}
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert not r.scheduled and not r.reports
    assert continuation.pre_park_witness(r.c.repo, before, 'other-request')['ready'] == []


@pytest.mark.parametrize('evidence', ['blocked', 'unavailable', 'ambiguous'])
def test_finish_distinguishes_known_blocked_from_unavailable(run, monkeypatch, evidence):
    r = run
    ready(r)
    activate(r)
    stage = queued_stage(r, monkeypatch)
    if evidence == 'blocked':
        monkeypatch.setattr(r.c.repo, 'dependency_states', lambda *a, **kw: [
            {'status': 'waiting_review', 'reason': 'review_required'}])
    else:
        from lib.project_dispatch.repository import ProjectDispatchError
        error = OSError('private unavailable detail') if evidence == 'unavailable' else ProjectDispatchError('private ambiguous detail')
        original = r.c.repo.validate_own

        def validate(binding, **kwargs):
            if binding['assignmentId'] == stage['id']:
                raise error
            return original(binding, **kwargs)

        monkeypatch.setattr(r.c.repo, 'validate_own', validate)
    before = deepcopy(r.c.repo._load()[0])
    result = r.call('finish_project_request', **report(r))
    shown = diagnostic(result)
    assert shown['ready'] == []
    assert shown['blocked'] == ([stage['id']] if evidence == 'blocked' else [])
    assert shown['unknown'] == ([] if evidence == 'blocked' else [stage['id']])
    assert 'private' not in result['error']
    assert r.c.repo._load()[0] == before


def test_board_only_linkage_stays_unknown(run):
    r = run
    ready(r)
    activate(r)
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    progress = CoordinatorTaskProgress(7, '/project', client=r.c.client, bindings=r.c.binding,
        notify=lambda: None, clock=lambda: r.c.now[0], arm=lambda *a: None)
    progress.publish(
        {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1},
        {'summary': 'Unregistered intention', 'tasks': [
            {'title': 'Board-only stage', 'status': 'pending',
             'blocker': {'kind': 'next_action', 'action': 'Draft the stage plan'}}]})
    shown = diagnostic(r.call('finish_project_request', **report(r)))
    assert shown['ready'] == shown['blocked'] == shown['unknown'] == []
    assert shown['unregistered_intentions'] == 'unknown'


def test_observation_does_not_consume_independent_result_wake(run, monkeypatch):
    r = run
    first = delegate(r)
    stage = queued_stage(r, monkeypatch)
    end(r, first)
    shown = diagnostic(r.call('finish_project_request', **report(r)))
    assert shown['ready'] == [stage['id']]
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    assert len(controls(r)) == 1
    assert r.c.repo.read(stage['id'])['execution'] is None


def test_needs_user_does_not_read_diagnostic_or_change_existing_settlement(run, monkeypatch):
    r = run
    ready(r)
    activate(r)
    queued_stage(r, monkeypatch)
    monkeypatch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('needs_user diagnostic'))
    result = r.call('finish_project_request', **report(r, 'blocked'))
    assert result['reason'] == 'needs_user', result


@pytest.mark.parametrize('phase', ['waiting_review', 'waiting_delivery', 'ready', 'unknown'])
def test_exact_registered_dependency_evidence_in_finish_response(run, phase):
    r = run
    first = delegate(r)
    end(r, first)
    checkpoint = 'a' * 40
    workspace = r.workspaces[first['execution']['taskId']]
    workspace['checkpoint_sha'] = checkpoint
    workspace['origin']['delivery']['checkpointSha'] = checkpoint
    request = deepcopy(first['request'])
    request['dependencies'] = [{'assignmentId': first['id'], 'grantRevision': 1,
                                'resultRevision': 1, 'checkpointSha': checkpoint}]
    stage = r.c.repo.authorize_from_coordinator(request, task=r.task, command_id='registered-dependent')
    from lib.project_dispatch.delegation import review_worker
    if phase != 'waiting_review':
        review_worker(r.c.repo, r.task, {'assignment_id': first['id'], 'decision': 'accept',
            'note': 'Verified', 'result_revision': 1,
            **({'checkpoint_sha': checkpoint} if phase in ('ready', 'unknown') else {})})
    if phase == 'unknown':
        r.workspaces.pop(first['execution']['taskId'])
    assert r.c.repo.dependency_states(stage)[0]['status'] == phase
    args = report(r)
    before = deepcopy(r.c.repo._load()[0])
    if phase == 'unknown':
        # The earlier canonical-delivery refusal keeps its original precedence.
        result = r.call('finish_project_request', **args)
        assert result['error'] == 'Worker canonical delivery is not confirmed'
        shown = continuation.pre_park_witness(r.c.repo, before, 'coordinator-attempt')
    else:
        shown = diagnostic(r.call('finish_project_request', **args))
    bucket = 'blocked' if phase.startswith('waiting') else phase
    assert shown[bucket] == [stage['id']]
    assert r.c.repo._load()[0] == before


def test_allowed_complete_and_settlement_replay_never_read_diagnostic(run, monkeypatch):
    r = run
    first = ready(r)
    activate(r)
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                      decision='accept', note='Verified')
    assert reviewed['decision'] == 'accept'
    monkeypatch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('allowed diagnostic'))
    args = report(r)
    done = r.call('finish_project_request', **args)
    assert done['reason'] == 'complete'
    before = deepcopy(r.c.repo._load()[0])
    assert r.call('finish_project_request', **args) == done
    refused = r.call('finish_project_request', **report(r, 'blocked'))
    assert refused['error'] == 'Coordinator settlement receipt is immutable'
    assert r.c.repo._load()[0] == before


def test_snapshot_excludes_other_request_ids(run, monkeypatch):
    r = run
    stage = queued_stage(r, monkeypatch)
    value = deepcopy(r.c.repo._load()[0])
    foreign = deepcopy(next(row for row in value['assignments'] if row['id'] == stage['id']))
    foreign['id'] = 'other-request-assignment'
    foreign['request']['delegation']['attemptId'] = 'other-request'
    value['assignments'].append(foreign)
    shown = continuation.pre_park_witness(r.c.repo, value, 'coordinator-attempt')
    assert shown['ready'] == [stage['id']]
    assert foreign['id'] not in json.dumps(shown)


@pytest.mark.parametrize('withdrawal', ['revoked', 'stopped'])
def test_withdrawn_prerequisite_and_unavailable_sibling_keep_distinct_finish_outcomes(run, monkeypatch, withdrawal):
    r = run
    revoked = delegate(r, 'prerequisite-revoked')
    unavailable = delegate(r, 'prerequisite-unavailable')
    children = []
    for parent in (revoked, unavailable):
        end(r, parent)
        checkpoint = 'b' * 40
        workspace = r.workspaces[parent['execution']['taskId']]
        workspace['checkpoint_sha'] = checkpoint
        workspace['origin']['delivery']['checkpointSha'] = checkpoint
        request = deepcopy(parent['request'])
        request['dependencies'] = [{'assignmentId': parent['id'], 'grantRevision': 1,
                                    'resultRevision': 1, 'checkpointSha': checkpoint}]
        children.append(r.c.repo.authorize_from_coordinator(
            request, task=r.task, command_id='dependent-' + parent['id']))
    if withdrawal == 'revoked':
        r.c.repo.revoke(revoked['id'], expected_revision=1)
    else:
        from lib.project_dispatch.repository import record_stop
        record_stop(7, revoked['request']['recipientId'], 1001, client=r.c.client)
    r.workspaces.pop(unavailable['execution']['taskId'])
    ready_sibling = queued_stage(r, monkeypatch, 'ready-sibling')
    assert r.c.repo.dependency_states(children[0])[0]['reason'] == withdrawal
    assert r.c.repo.dependency_states(children[1])[0]['status'] == 'unknown'
    args = report(r)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    shown = diagnostic(r.call('finish_project_request', **args))
    assert shown['blocked'] == [children[0]['id']]
    assert shown['unknown'] == [children[1]['id']]
    assert shown['ready'] == [ready_sibling['id']]
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert not r.scheduled and not r.reports


@pytest.mark.parametrize('reason,bucket', [
    ('revoked', 'blocked'), ('stopped', 'blocked'),
    ('scope_changed', 'unknown'), ('result_changed', 'unknown'),
    ('unsupported_reason', 'unknown'),
])
def test_invalid_dependency_reasons_are_not_blanket_blocked(run, monkeypatch, reason, bucket):
    r = run
    ready(r)
    activate(r)
    stage = queued_stage(r, monkeypatch)
    original = r.c.repo.dependency_states

    def dependency_states(row, **kwargs):
        if row['id'] == stage['id']:
            return [{'status': 'invalid', 'reason': reason}]
        return original(row, **kwargs)

    monkeypatch.setattr(r.c.repo, 'dependency_states', dependency_states)
    before = deepcopy(r.c.repo._load()[0])
    shown = diagnostic(r.call('finish_project_request', **report(r)))
    assert shown[bucket] == [stage['id']]
    assert shown['unknown' if bucket == 'blocked' else 'blocked'] == []
    assert shown['ready'] == [] and r.c.repo._load()[0] == before


@pytest.mark.parametrize('ancestor', ['revoked', 'stopped', 'unavailable', 'ambiguous'])
def test_rejected_intermediate_cannot_mask_transitive_withdrawal(run, ancestor):
    r = run
    first = delegate(r, 'ancestor')
    end(r, first)
    checkpoint = 'c' * 40
    from lib.project_dispatch.delegation import review_worker

    def pin_workspace(row):
        workspace = r.workspaces[row['execution']['taskId']]
        workspace['checkpoint_sha'] = checkpoint
        workspace['origin']['delivery']['checkpointSha'] = checkpoint

    def child(parent, key):
        request = deepcopy(parent['request'])
        recipient = 'recipient-' + key
        r.c.conversations.rows[recipient] = {'user_id': 7, 'settings': {'projectPath': '/project'}}
        request['recipientId'] = recipient
        request['dependencies'] = [{'assignmentId': parent['id'], 'grantRevision': 1,
                                    'resultRevision': 1, 'checkpointSha': checkpoint}]
        return r.c.repo.authorize_from_coordinator(request, task=r.task, command_id=key)

    pin_workspace(first)
    review_worker(r.c.repo, r.task, {'assignment_id': first['id'], 'decision': 'accept',
        'note': 'Verified', 'result_revision': 1, 'checkpoint_sha': checkpoint})
    middle = child(first, 'middle')
    assert r.service.dispatch_authorized(r.c.repo.admission(middle['id']))['dispatch'] == 'accepted'
    middle = r.c.repo.read(middle['id'])
    end(r, middle)
    pin_workspace(middle)
    last = child(middle, 'last')
    review_worker(r.c.repo, r.task, {'assignment_id': middle['id'], 'decision': 'reject',
        'note': 'Rejected', 'result_revision': 1})
    if ancestor == 'revoked':
        r.c.repo.revoke(first['id'], expected_revision=1)
    elif ancestor == 'stopped':
        from lib.project_dispatch.repository import record_stop
        record_stop(7, first['request']['recipientId'], 1001, client=r.c.client)
    elif ancestor == 'unavailable':
        r.workspaces.pop(first['execution']['taskId'])
    else:
        r.turns[first['execution']['turnId']]['projectionRevision'] = 2
    assert r.c.repo.dependency_states(last)[0]['reason'] == 'review_rejected'
    args = report(r)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    if ancestor in ('revoked', 'stopped'):
        shown = diagnostic(r.call('finish_project_request', **args))
    else:
        # The earlier parent's exact-result/delivery refusal retains precedence.
        shown = continuation.pre_park_witness(r.c.repo, before, 'coordinator-attempt')
    bucket = 'blocked' if ancestor in ('revoked', 'stopped') else 'unknown'
    assert shown[bucket] == [last['id']]
    assert shown['unknown' if bucket == 'blocked' else 'blocked'] == []
    assert shown['ready'] == []
    assert r.c.repo._load()[0] == before and r.calls == calls
