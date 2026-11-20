"""RFC-only vectors and reconstructed 58a65c70 negative fixtures; no runtime event writer."""
from copy import deepcopy
import hashlib
import json

import pytest

from lib.project_dispatch import continuation
from tests.project_mod.test_coordinator_continuation import (
    run as run, runtime as runtime, base_runtime as base_runtime, context as context,
    activate, controls, delegate, end,
)
from tests.project_mod.test_coordinator_wake_lane_events import poll_reports

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_readiness_probe(monkeypatch):
    monkeypatch.setattr('lib.conversations.coordinator_continuation._questions', lambda *a: [])
    monkeypatch.setattr('lib.integration_control.release_publication_hold', lambda *a, **kw: None)
    continuation._LANE_HOLDS.clear()
    continuation._LANE_OBSERVATIONS.clear()
    yield
    continuation._LANE_HOLDS.clear()
    continuation._LANE_OBSERVATIONS.clear()


def _accepted_file_result(r):
    first = delegate(r)
    end(r, first)
    sha = 'a' * 40
    workspace = r.workspaces[first['execution']['taskId']]
    workspace['checkpoint_sha'] = sha
    workspace['origin']['delivery']['checkpointSha'] = sha
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                     decision='accept', note='Synthetic exact prerequisite', checkpoint_sha=sha)
    assert reviewed['decision'] == 'accept'
    return first, sha


def _registered_stage(r, first, sha):
    request = deepcopy(first['request'])
    request['dependencies'] = [{'assignmentId': first['id'], 'grantRevision': 1,
                                'resultRevision': 1, 'checkpointSha': sha}]
    stage = r.c.repo.authorize_from_coordinator(request, task=r.task, command_id='synthetic-stage')
    assert r.c.repo.validate(r.c.repo.admission(stage['id']))['id'] == stage['id']
    assert r.c.repo.dependency_states(stage)[0]['status'] == 'ready'
    assert stage['execution'] is None
    return stage


def test_retained_negative_normal_park_has_no_readiness_wake(run):
    r = run
    first, sha = _accepted_file_result(r)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    control = activate(r)
    stage = _registered_stage(r, first, sha)
    frontier = control['request']['coordinatorWake']['frontier']
    end(r, control, delivered=False)
    births = list(r.created)
    for _ in range(3):
        assert r.wake() == []
    assert controls(r) == [r.c.repo.read(control['id'])]
    assert controls(r)[0]['request']['coordinatorWake']['frontier'] == frontier
    assert r.c.repo.read(stage['id'])['execution'] is None
    assert r.created == births


def test_retained_negative_observed_dormant_results_cannot_name_readiness(run):
    r = run
    first, sha = _accepted_file_result(r)
    stage = _registered_stage(r, first, sha)
    reported = poll_reports(r, first)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.queued_human = True
    assert r.wake() == ['deferred']
    dormant = controls(r)[0]
    assert dormant['execution'] is None
    assert r.wake() == ['deferred'] and len(controls(r)) == 1
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', reported)
    r.queued_human = False
    births = list(r.created)
    for _ in range(3):
        assert r.wake() == []
    assert r.c.repo.read(dormant['id'])['execution'] is None
    assert r.c.repo.read(stage['id'])['execution'] is None
    assert r.created == births and len(controls(r)) == 1


def _candidate():
    return {'version': 'registered-work-readiness/v1', 'owner': 7, 'tenant': 'owner:7',
            'project': '/project', 'coordinator': 'coordinator', 'bindingRevision': 1,
            'request': {'sourceAttemptId': 'original', 'requestBudgetId': 'request-1'},
            'assignmentId': 'stage-1', 'grantRevision': 1,
            'dependencies': [{'assignmentId': 'prerequisite', 'grantRevision': 1,
                              'resultRevision': 1, 'checkpointSha': 'a' * 40}]}


def _token(candidate):
    # Test-local identity algebra, not a proposed production codec or validator.
    value = deepcopy(candidate)
    value['dependencies'].sort(key=lambda pin: pin['assignmentId'])
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _admit_model(candidate, *, receipts, aligned=True, request_open=True, authority=True,
                 unbound=True, ready=True, lane_idle=True):
    """Pure atomic-transition assumption: receipts and wake are one in-memory value."""
    identity = _token(candidate)
    if identity in receipts:
        return receipts, receipts[identity]
    if not all((aligned, request_open, authority, unbound, ready, lane_idle)):
        return receipts, None
    wake = 'coordinator-readiness:' + identity
    return {**receipts, identity: wake}, wake


def test_rfc_identity_and_uncertain_ack_are_stable_across_continuation_attempts():
    candidate = _candidate()
    receipts, wake = _admit_model(candidate, receipts={})
    for continuation_attempt in ('continuation-1', 'continuation-2', 'after-restart'):
        replay, same = _admit_model(candidate, receipts=deepcopy(receipts), lane_idle=False)
        assert same == wake and replay == receipts
        assert continuation_attempt not in candidate['request']
    assert len(receipts) == 1
    assert _admit_model(candidate, receipts=receipts, request_open=False)[0] == receipts


@pytest.mark.parametrize('fence', ['aligned', 'request_open', 'authority', 'unbound', 'ready', 'lane_idle'])
def test_rfc_unmet_fence_never_consumes_readiness(fence):
    receipts, wake = _admit_model(_candidate(), receipts={}, **{fence: False})
    assert receipts == {} and wake is None


@pytest.mark.parametrize('field,value', [('owner', 8), ('tenant', 'other'), ('project', '/other'),
                                        ('bindingRevision', 2), ('assignmentId', 'stage-2'),
                                        ('grantRevision', 2), ('version', 'unsupported/v2')])
def test_rfc_identity_separates_authority_and_registration_versions(field, value):
    original = _candidate()
    changed = {**original, field: value}
    assert _token(original) != _token(changed)


def test_rfc_dependency_order_is_not_news_but_exact_pin_change_is():
    original = _candidate()
    original['dependencies'].append({'assignmentId': 'other', 'grantRevision': 1, 'resultRevision': 2})
    reordered = deepcopy(original)
    reordered['dependencies'].reverse()
    assert _token(original) == _token(reordered)
    changed = deepcopy(original)
    changed['dependencies'][0]['resultRevision'] = 2
    assert _token(original) != _token(changed)


def test_rfc_parallel_outcomes_do_not_rearm_consumed_subset_or_hide_independent_work():
    first = _candidate()
    second = {**first, 'assignmentId': 'stage-2'}
    receipts, one = _admit_model(first, receipts={})
    receipts, two = _admit_model(second, receipts=receipts)
    assert one != two and len(receipts) == 2
    replay, same = _admit_model(second, receipts=receipts)
    assert replay == receipts and same == two
    assert first['assignmentId'] not in second['dependencies']


@pytest.mark.parametrize('field', ['sourceAttemptId', 'requestBudgetId'])
def test_rfc_original_request_identity_is_not_shared_with_another_request(field):
    original = _candidate()
    changed = deepcopy(original)
    changed['request'][field] = 'different-request'
    assert _token(original) != _token(changed)


def test_rfc_request_grant_actual_pin_alignment_cannot_be_replaced_by_display_readiness():
    request_pin = _candidate()['dependencies'][0]
    for changed in ('grantRevision', 'resultRevision', 'checkpointSha'):
        grant_pin = deepcopy(request_pin)
        actual_pin = deepcopy(request_pin)
        actual_pin[changed] = 'changed' if changed == 'checkpointSha' else 2
        aligned = request_pin == grant_pin == actual_pin
        assert not aligned
        assert _admit_model(_candidate(), receipts={}, aligned=aligned, ready=True) == ({}, None)
    assert _admit_model(_candidate(), receipts={}, aligned=True)[1] is not None
