"""Paired comparison is descriptive evidence, not a generated quality score."""
from copy import deepcopy

import pytest

from lib.research.quality_comparison import METRICS, compare_quality

pytestmark = pytest.mark.unit


def pair():
    arm = dict(task_digest='task', corpus_digest='corpus', model='model',
               budget_digest='budget', actual_cost=4, cost_ceiling=5,
               adjudication_kind='independent_evidence_audit', evidence_refs=['audit:1'],
               metrics={key: 0.5 for key in METRICS})
    return dict(pair_id='p1', control=arm, treatment=deepcopy(arm))


def test_matched_differences_do_not_claim_significance():
    row = pair()
    row['treatment']['metrics']['counterevidence_recall'] = 0.75
    result = compare_quality([row])
    assert result['status'] == 'descriptive_only'
    assert result['mean_paired_difference']['counterevidence_recall'] == 0.25
    assert not result['excluded_pairs']


@pytest.mark.parametrize('field,value', [
    ('corpus_digest', 'other'), ('cost_ceiling', 6), ('actual_cost', 9),
    ('actual_cost', float('nan')), ('actual_cost', True),
    ('adjudication_kind', 'model_self_score'), ('evidence_refs', []),
    ('metrics', {}),
])
def test_unmatched_or_unverified_arm_is_excluded(field, value):
    row = pair()
    row['treatment'][field] = value
    result = compare_quality([row])
    assert result['status'] == 'insufficient_evidence'
    assert not result['mean_paired_difference']
    assert result['excluded_pairs']


def test_missing_duplicate_and_bounded_inputs():
    assert compare_quality([])['status'] == 'insufficient_evidence'
    result = compare_quality([pair(), pair(), None])
    assert len(result['valid_pairs']) == 1
    assert len(result['excluded_pairs']) == 2
    with pytest.raises(ValueError):
        compare_quality([pair()] * 101)
