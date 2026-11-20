"""The retained-usage verifier produces anonymous counts, not request evidence dumps."""
import json

import pytest

from scripts.cache_usage_window import reconcile

pytestmark = pytest.mark.unit

_START = '2026-11-19 00:00:00'
_END = '2026-11-19 07:00:00'


def _stats(trace, *, stamp=_START, model='gpt-6.1-sol', ordinal=1, read=3, write='unknown'):
    return (f'{stamp} [INFO] lib.tasks_pkg.cache_tracking._roi [worker]: '
            f'[rid:private-task] [CacheStats] private-task conv=private-conv R{ordinal} '
            f'model={model} input=12 cache_w={write} cache_r={read} hit=25% '
            f'key=private-key retries=0 trace={trace}\n')


def _record(trace, *, stamp=_START, call=3, model='gpt-6.1-sol', read=3):
    return stamp + ' [INFO] lib.tasks_pkg.cache_tracking._detect [worker]: [CacheRoundRecord] ' + json.dumps({
        'trace_id': trace, 'conv': 'private-conv', 'model': model, 'cache_read': read,
        'call': call, 'baseline': 'live', 'wire_available': True, 'body_identical': True,
        'namespace_verified': True, 'request_identity': {'provider_prefix_id': '8' * 64},
    }) + '\n'


def test_joins_trace_not_ordinal_or_timestamp_and_does_not_publish_identities():
    trace = 'private-trace'
    stats = _stats(trace)
    report = reconcile([stats, _record(trace, stamp='2026-11-19 00:00:01'), stats,
                        _stats('excluded-at-end', stamp=_END),
                        _stats('spoofed').replace('_roi', 'other')], start=_START, end=_END)
    assert report['all'] == {'rows': 1, 'input': 12, 'cache_read': 3, 'input_minus_read': 9,
                             'cache_write_reported': 0, 'cache_write_unknown_rows': 1,
                             'zero_read_rows': 0}
    assert report['join']['round_ordinal_mismatch'] == 1
    assert report['ingest']['stats']['duplicate_projected_rows'] == 1
    group = report['by_model']['gpt-6.1-sol']
    assert group['first_task_round'] == group['verified_unchanged'] == report['all']
    result = json.dumps(report)
    assert 'private-' not in result and '8' * 64 not in result and 'excluded-at-end' not in result


def test_quarantines_bad_or_unmatched_traces_and_groups_unknown_model():
    model = 'private-model'
    report = reconcile([
        _stats('ambiguous'), _stats('ambiguous', read=4), _record('ambiguous'),
        _stats('mismatch'), _record('mismatch', read=4),
        _stats('stats-only'), _record('record-only'),
        _stats('-'), _record(None), _record('malformed').split('{', 1)[0] + 'nope',
        _stats('good', model=model), _record('good', model=model),
        _stats('again', ordinal=2), _record('again', call=2),
    ], start=_START, end=_END)
    assert report['join'] == {'stats_only': 1, 'records_only': 1, 'ambiguous': 1,
                             'identity_or_usage_mismatch': 1, 'read_not_comparable': 0,
                             'round_ordinal_mismatch': 1}
    assert report['ingest']['records']['malformed'] == 1
    assert all(counters['missing_trace'] == 1 for counters in report['ingest'].values())
    assert report['sealed_prefix_id_coverage'] == {
        'rows_with_id': 2, 'distinct_ids': 1, 'additional_occurrences': 1}
    assert set(report['by_model']) == {'other', 'gpt-6.1-sol'}


def test_read_coverage_separates_explicit_zero_from_missing_and_legacy_zero():
    def record(trace, value, status=None, *, model='gpt-6.1-sol', input_tokens=12):
        header, payload = _record(trace, model=model).split('[CacheRoundRecord] ', 1)
        row = json.loads(payload)
        if value is ...:
            del row['cache_read']
        else:
            row['cache_read'] = value
        if status is not None:
            row['input_tokens'] = input_tokens
            row['responses_usage_status'] = {'input_tokens': 'observed', 'cache_read': status}
        return header + '[CacheRoundRecord] ' + json.dumps(row)

    positive = record('positive', 3, 'observed')
    report = reconcile([
        positive, positive, _stats('positive'),
        record('explicit-zero', 0, 'observed'), _stats('explicit-zero', read=0),
        record('legacy-zero', 0), _stats('legacy-zero', read=0),
        record('absent', ...), _stats('absent', read=0),
        record('null', None, 'missing'),
        record('contradiction', 0, 'missing'), _stats('contradiction', read=0),
        record('invalid', True, 'observed'),
        record('no-input', 0, 'observed', input_tokens=None), _stats('no-input', read=0),
        record('wrong-input', 3, 'observed', input_tokens=11), _stats('wrong-input'),
        record('private-route', 3, model='private-provider-like-model'),
        record('conflict', 3), record('conflict', None, 'missing'),
    ], start=_START, end=_END)

    coverage = report['detector_read_coverage']
    assert coverage['all'] == {'rows': 10, 'positive_rows': 3, 'explicit_zero_rows': 1,
                               'legacy_zero_rows': 1, 'unknown_rows': 5}
    assert coverage['by_model']['other']['positive_rows'] == 1
    assert report['join']['read_not_comparable'] == 3
    assert report['join']['identity_or_usage_mismatch'] == 1
    assert report['join']['records_only'] == 4  # includes the conflicting trace
    assert report['all']['rows'] == 3
    assert report['all']['zero_read_rows'] == 2  # numeric join only, not proven misses
    assert report['evidence_scope'] == {
        'dispatch_coverage': 'unknown', 'provider_attribution': 'unavailable',
        'configured_estimate': 'unavailable', 'upstream_actual': 'unavailable',
    }
    result = json.dumps(report)
    assert all(value not in result for value in ('private-', 'misses', 'savings'))
