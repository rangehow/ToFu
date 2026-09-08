"""Read-only paired research-quality comparison; never launches paid work.

Inputs are externally adjudicated, paired run receipts. This module validates
comparability and reports raw paired differences, not acceptance probabilities
or statistical significance. Production model-review scores are not outcomes.
"""
from __future__ import annotations

import math
from statistics import mean

METRICS = ('supported_claim_precision', 'counterevidence_recall',
           'useful_hypothesis_rate', 'reproduction_success_rate')
MAX_PAIRS = 100


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def compare_quality(pairs: list[dict]) -> dict:
    """Validate complete matched pairs; invalid pairs stay visible, never imputed.

    Each arm declares identical frozen corpus, task, model and budget identity,
    actual spend and independently adjudicated metrics with evidence references.
    This validates receipts only; the caller must audit referenced evidence.
    """
    if not isinstance(pairs, list) or len(pairs) > MAX_PAIRS:
        raise ValueError('quality comparison requires at most 100 pairs')
    valid, excluded, seen = [], [], set()
    for index, pair in enumerate(pairs):
        reasons = []
        if not isinstance(pair, dict):
            excluded.append({'index': index, 'reasons': ['malformed_pair']})
            continue
        identity = pair.get('pair_id')
        if not isinstance(identity, str) or not identity or identity in seen:
            reasons.append('missing_or_duplicate_pair_id')
        else:
            seen.add(identity)
        control, treatment = pair.get('control'), pair.get('treatment')
        if not isinstance(control, dict) or not isinstance(treatment, dict):
            excluded.append({'index': index, 'reasons': reasons + ['missing_arm']})
            continue
        for key in ('task_digest', 'corpus_digest', 'model', 'budget_digest'):
            if not isinstance(control.get(key), str) or not control[key] or control[key] != treatment.get(key):
                reasons.append('unmatched_' + key)
        for name, arm in (('control', control), ('treatment', treatment)):
            if arm.get('adjudication_kind') != 'independent_evidence_audit':
                reasons.append(name + '_not_independently_audited')
            refs = arm.get('evidence_refs')
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
                reasons.append(name + '_missing_evidence')
            spend, ceiling = arm.get('actual_cost'), arm.get('cost_ceiling')
            if not _number(spend) or not _number(ceiling) or not 0 <= spend <= ceiling:
                reasons.append(name + '_invalid_budget')
            metrics = arm.get('metrics')
            if not isinstance(metrics, dict) or any(not _number(metrics.get(key)) or not 0 <= metrics[key] <= 1 for key in METRICS):
                reasons.append(name + '_missing_or_invalid_metrics')
        if control.get('cost_ceiling') != treatment.get('cost_ceiling'):
            reasons.append('unmatched_cost_ceiling')
        if reasons:
            excluded.append({'index': index, 'pair_id': identity, 'reasons': reasons})
        else:
            valid.append({'pair_id': identity, 'differences': {
                key: treatment['metrics'][key] - control['metrics'][key] for key in METRICS},
                'cost_difference': treatment['actual_cost'] - control['actual_cost']})
    return {'status': 'descriptive_only' if valid else 'insufficient_evidence',
            'valid_pairs': valid, 'excluded_pairs': excluded,
            'mean_paired_difference': {key: mean(row['differences'][key] for row in valid)
                                       for key in METRICS} if valid else {},
            'limitations': ['Evidence references require external audit.',
                            'No significance or conference acceptance claim is inferred.',
                            'Identical budget ceilings do not imply identical actual spend.']}
