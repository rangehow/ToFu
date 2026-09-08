"""Scientific support is dependent evidence, not successful execution."""
import copy

import pytest

from lib.research.action import apply_action_result
from lib.research.program import normalize_program_fields, readiness
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def _result(outcome='supported'):
    workspace = empty_workspace('Matched experiment')
    workspace['hypothesis'] = 'Intervention improves held-out accuracy'
    return apply_action_result(
        action='experiment', workspace=workspace, task_id='t1', bindings=[],
        payload={'run': {'id': 'r1', 'status': 'passed'}, 'claims': [
            {'id': 'c1', 'text': 'Improves accuracy', 'status': 'supported',
             'evidence_refs': ['r1']}]},
        receipts=[{'tool': 'run_command', 'status': 'done',
                   'artifact_ref': 'evidence:1', 'result_digest': 'abc',
                   'result_excerpt': '{"scientific_outcome":"' + outcome + '"}'}])


def test_support_requires_observed_outcome_not_execution_pass():
    result = _result('inconclusive')
    assert result['runs'][0]['execution_status'] == 'completed'
    assert result['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert result['claims'][0]['status'] == 'contested'
    assert not readiness(result)['ready']


@pytest.mark.parametrize('change', ['hypothesis', 'protocol', 'evidence', 'claim'])
def test_dependency_changes_contest_previously_supported_claim(change):
    result = _result()
    assert result['claims'][0]['status'] == 'supported'
    assert normalize_program_fields(result)['claims'][0]['validity'] == 'current'
    changed = copy.deepcopy(result)
    if change == 'hypothesis':
        changed['hypothesis'] = 'Different hypothesis'
    elif change == 'protocol':
        changed['protocol']['baseline'] = 'Different comparator'
    elif change == 'claim':
        changed['claims'][0]['text'] = 'Different assertion'
    else:
        changed['runs'][0]['tool_receipts'][0]['result_digest'] = 'edited'
    normalized = normalize_program_fields(changed)
    assert normalized['claims'][0]['status'] == 'contested'
    assert normalized['claims'][0]['evidence_refs'] == ['r1']
    assert not readiness(changed)['ready']


def test_legacy_pass_is_preserved_but_never_promoted_to_scientific_support():
    normalized = normalize_program_fields({'runs': [
        {'id': 'old', 'status': 'passed', 'artifact_ref': 'old:data'}],
        'claims': [{'id': 'old-claim', 'text': 'Legacy assertion',
                    'status': 'supported', 'evidence_refs': ['old']}]})
    assert normalized['runs'][0]['status'] == 'passed'
    assert normalized['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert normalized['runs'][0]['validity'] == 'unverified'
    assert normalized['claims'][0]['status'] == 'contested'


def test_refutation_preserved_and_model_cannot_mutate_frozen_protocol():
    result = _result('refuted')
    assert result['runs'][0]['scientific_outcome'] == 'refuted'
    assert result['claims'][0]['status'] == 'contested'
    updated = apply_action_result(action='experiment', workspace=result,
        payload={'protocol': {'baseline': 'invented'}, 'run': {
            'scientific_outcome': 'supported'}}, receipts=[], task_id='t2', bindings=[])
    assert updated['protocol'] == result['protocol']
    assert updated['runs'][-1]['scientific_outcome'] == 'inconclusive'
