"""Synthetic point reads through the existing operation runner; no authorities."""
from copy import deepcopy

import pytest

from scripts import tofudb_charter_assess as charter
from scripts import tofudb_perf_compare as bench
from scripts.tofudb_workloads.result_evidence import compare_results

pytestmark = pytest.mark.unit


class Records:
    def __init__(self, *, wrong=False, timestamp=123):
        self.values = {}
        self.calls = []
        self.wrong = wrong
        self.timestamp = timestamp

    def execute(self, operation, payload, command):
        self.calls.append((operation, deepcopy(payload), command))
        key = payload['key']
        if operation == 'record.put':
            self.values[key] = deepcopy(payload['value'])
            return {'key': key, 'version': 1, 'updated_at_ms': self.timestamp}
        value = deepcopy(self.values[key])
        if self.wrong:
            value['n'] = -1
        return {'value': value, 'version': 1, 'updated_at_ms': self.timestamp}


def run(engine, monkeypatch):
    clock = iter(range(0, 100_000_000, 1000))
    monkeypatch.setattr(bench.time, 'perf_counter_ns', lambda: next(clock))
    entry = next(row for row in bench._workloads(bench._SCALES['tiny'], 1, namespace='fixture')
                 if row[0] == 'record_get_points')
    _, setup, operation, _, runner = entry
    return runner(engine, setup, operation, 4)


def test_correct_timed_population_qualifies_and_records_original_results(monkeypatch):
    left, right = Records(), Records()
    a, b = run(left, monkeypatch), run(right, monkeypatch)
    assert compare_results({'tofudb': a, 'sqlite': b})['qualified']
    assert left.calls == right.calls
    assert len(left.calls) == 20 + 5 + 4
    assert a['samples_ms'] == b['samples_ms'] == [0.001] * 4
    assert [sample['iteration'] for sample in a['result_evidence']['samples']] == [5, 6, 7, 8]
    assert a['result_evidence']['samples'][0]['result']['updated_at_ms'] == 123


@pytest.mark.parametrize('damage', ['wrong', 'missing', 'error', 'population', 'metadata',
                                    'empty', 'unsupported', 'boolean', 'partial',
                                    'reorder', 'duplicate', 'clocks', 'shape'])
def test_fidelity_is_not_recorded_execution(monkeypatch, damage):
    a = run(Records(), monkeypatch)
    b = deepcopy(a)
    samples = b['result_evidence']['samples']
    if damage == 'wrong':
        samples[0]['result']['value']['n'] = -1
    elif damage == 'missing':
        del samples[0]['result']
    elif damage == 'error':
        b['error'] = 'read failed'
    elif damage == 'population':
        samples[0]['request'][1]['key'] = 'k/000019'
    elif damage == 'metadata':
        samples[0]['result']['version'] = 2
    elif damage == 'empty':
        samples[0]['result'] = None
    elif damage == 'unsupported':
        del b['result_evidence']
    elif damage == 'boolean':
        samples[0]['result']['value']['n'] = True
    elif damage == 'partial':
        samples.pop()
    elif damage == 'reorder':
        samples.reverse()
    elif damage == 'duplicate':
        samples[1] = deepcopy(samples[0])
    elif damage == 'clocks':
        b['samples_ms'].pop()
    else:
        b['result_evidence']['response_shape'] = 'bare'
    result = compare_results({'tofudb': a, 'sqlite': b})
    assert result['qualified'] is False
    assert b['iterations'] == 4
    assert b['samples_ms'] == [0.001] * (3 if damage == 'clocks' else 4)
    cell = charter.metric_cell(
        [{'workloads': {'record_get_points': {'tofudb': a, 'sqlite': b}}}],
        {'status': 'PASS', 'reasons': []}, 'record_get_points', {'name': 'p50_ms'}, 'sqlite', 1)
    assert cell['status'] != 'PASS'
    assert not cell.get('observations')


def test_full_response_metadata_differences_are_not_normalized(monkeypatch):
    a, b = run(Records(timestamp=123), monkeypatch), run(Records(timestamp=124), monkeypatch)
    assert not compare_results({'tofudb': a, 'sqlite': b})['qualified']


def test_wrong_equal_results_do_not_qualify(monkeypatch):
    a = run(Records(wrong=True), monkeypatch)
    assert not compare_results({'tofudb': a, 'sqlite': deepcopy(a)})['qualified']


def test_matching_but_different_timed_populations_do_not_qualify(monkeypatch):
    a = run(Records(), monkeypatch)
    b = deepcopy(a)
    b['result_evidence']['warmup'] = 6
    assert not compare_results({'tofudb': a, 'sqlite': b})['qualified']


def test_missing_evidence_and_unknown_operation_cannot_admit_ratio(monkeypatch):
    stats = bench.measure_workload(Records(), None,
                                   lambda i: ('record.put', {'key': str(i), 'value': i}, True),
                                   2, warmup=0)
    assert 'result_evidence' not in stats
    cell = charter.metric_cell(
        [{'workloads': {'write': {'tofudb': stats, 'sqlite': deepcopy(stats)}}}],
        {'status': 'PASS', 'reasons': []}, 'write', {'name': 'p50_ms'}, 'sqlite', 1)
    assert cell['status'] == 'INSUFFICIENT'
    assert 'ratio' not in str(cell)


def test_timing_arm_has_only_the_same_execution_between_clocks(monkeypatch):
    events = []
    ticks = iter(range(20))

    def clock():
        events.append('clock')
        return next(ticks)

    def execute(engine, operation, payload, command):
        events.append('execute')
        return {'value': bench._record_value(0), 'version': 1, 'updated_at_ms': 1}

    import copy

    original_copy = copy.deepcopy

    def capture(value):
        events.append('capture')
        return original_copy(value)

    monkeypatch.setattr(copy, 'deepcopy', capture)
    monkeypatch.setattr(bench.time, 'perf_counter_ns', clock)
    monkeypatch.setattr(bench, '_execute_workload_operation', execute)
    operation = lambda i: ('record.get', {'namespace': 'f', 'key': 'k/000000'}, False)
    for oracle in (None, {'namespace': 'f', 'seed_records': 1, 'seed_receipts': [
            {'key': 'k/000000', 'version': 1, 'updated_at_ms': 1}]}):
        events.clear()
        bench.measure_workload(Records(), None, operation, 2, warmup=0, result_oracle=oracle)
        expected = (['clock', 'execute', 'clock'] if oracle is None else
                    ['capture', 'clock', 'execute', 'clock', 'capture'])
        assert events == expected * 2


def test_evidence_cannot_be_relabelled_as_another_workload(monkeypatch):
    stats = run(Records(), monkeypatch)
    assert not compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)},
                               workload='turn_append_burst')['qualified']


def test_full_assessment_without_results_withholds_operation_ratios():
    from tests.tofudb.test_tofudb_charter_assess import SOURCES, reports

    result = charter.assess(reports(), current_sources=SOURCES)
    checked = 0
    for dimension in result['dimensions']:
        for cell in dimension.get('cells', []):
            if 'workload' in cell:
                checked += 1
                assert cell['status'] == 'INSUFFICIENT'
                assert not cell.get('observations')
    assert checked > 0
    assert result['comprehensive_victory'] is False


def test_seed_receipts_are_snapshots_not_reused_adapter_buffers(monkeypatch):
    class ReusedReceipt(Records):
        def __init__(self):
            super().__init__()
            self.receipt = {}

        def execute(self, operation, payload, command):
            result = super().execute(operation, payload, command)
            if operation == 'record.put':
                self.receipt.clear()
                self.receipt.update(result)
                return self.receipt
            return result

    a, b = run(ReusedReceipt(), monkeypatch), run(Records(), monkeypatch)
    assert compare_results({'tofudb': a, 'sqlite': b})['qualified']
    receipts = a['result_evidence']['seed_receipts']
    assert receipts[0]['key'] == 'k/000000'
    assert receipts[-1]['key'] == 'k/000019'


def test_custom_witnesses_without_oracles_cannot_bypass_result_gate():
    from tests.tofudb.test_tofudb_charter_assess import SOURCES, witnessed_reports

    result = charter.assess(witnessed_reports(), current_sources=SOURCES)
    checked = 0
    for dimension in result['dimensions']:
        for cell in dimension.get('cells', []):
            if 'witness' in cell:
                checked += 1
                assert cell['status'] == 'INSUFFICIENT'
                assert not cell.get('observations')
    assert checked > 0


@pytest.mark.parametrize('case', ['equivalent', 'mismatch', 'missing', 'error', 'unsupported'])
def test_console_ratios_require_pair_specific_result_qualification(monkeypatch, capsys, case):
    native = run(Records(), monkeypatch)
    sqlite = deepcopy(native)
    raw = deepcopy(native)
    for stats, duration in ((native, 1), (sqlite, 2), (raw, 3)):
        stats.update(bench._latency_summary([duration] * 4, retain_samples=False))
        stats['samples_ms'] = [duration] * 4
    workload = 'record_get_points'
    if case == 'mismatch':
        sqlite['result_evidence']['samples'][0]['result']['value']['n'] = -1
    elif case == 'missing':
        del sqlite['result_evidence']
    elif case == 'error':
        sqlite['error'] = 'synthetic execution failure'
    elif case == 'unsupported':
        workload = 'turn_append_burst'
    # A collector summary is not authority for console comparison.
    report = {'engines': ['tofudb', 'sqlite', 'rawsqlite'],
              'workloads': {workload: {'tofudb': native, 'sqlite': sqlite, 'rawsqlite': raw}},
              'operation_result_comparison': {workload: {'qualified': True, 'reasons': []}}}
    bench._print_report(report)
    output = capsys.readouterr().out
    sqlite_section = output.split('tofudb speedup vs sqlite (mean latency ratio):')[1].split(
        'tofudb speedup vs rawsqlite (mean latency ratio):')[0]
    if case == 'equivalent':
        assert '2.00x  FASTER' in sqlite_section
    else:
        assert 'unqualified' in sqlite_section
        reason = ('timed result differs from seeded value oracle' if case == 'mismatch' else
                  'no justified oracle for this workload' if case == 'unsupported' else
                  'execution error or missing supported result evidence')
        assert reason in sqlite_section
        assert 'FASTER' not in sqlite_section
        assert 'slower' not in sqlite_section
        assert '2.00x' not in sqlite_section
    raw_section = output.split('tofudb speedup vs rawsqlite (mean latency ratio):')[1]
    assert 'unqualified' in raw_section
    assert '3.00x' not in raw_section
    assert 'FASTER' not in raw_section


@pytest.mark.parametrize('field', ['version', 'updated_at_ms'])
@pytest.mark.parametrize('value,qualified', [
    (0, False), (-1, False), (2**64, False), (True, False), (False, False),
    (1, True), (2**64 - 1, True),
])
def test_metadata_requires_canonical_positive_u64(monkeypatch, field, value, qualified):
    stats = run(Records(), monkeypatch)
    evidence = stats['result_evidence']
    for receipt in evidence['seed_receipts']:
        receipt[field] = value
    for sample in evidence['samples']:
        sample['result'][field] = value
    result = compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})
    assert result['qualified'] is qualified
    if not qualified:
        assert result['reasons'] == ['missing or invalid versioned record metadata']


@pytest.mark.parametrize('damage', ['missing', 'error', 'boolean', 'key', 'extra', 'zero', 'overflow'])
def test_every_seed_receipt_is_validated_even_when_not_sampled(monkeypatch, damage):
    stats = run(Records(), monkeypatch)
    evidence = stats['result_evidence']
    assert all(sample['request'][1]['key'] != 'k/000000' for sample in evidence['samples'])
    receipt = evidence['seed_receipts'][0]
    if damage == 'missing':
        evidence['seed_receipts'][0] = None
    elif damage == 'error':
        evidence['seed_receipts'][0] = {'error': 'synthetic seed failure'}
    elif damage == 'boolean':
        receipt['version'] = True
    elif damage == 'key':
        receipt['key'] = 'k/000019'
    elif damage == 'extra':
        receipt['error'] = 'synthetic seed failure'
    elif damage == 'zero':
        receipt['updated_at_ms'] = 0
    else:
        receipt['version'] = 2**64
    result = compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})
    assert result['qualified'] is False
    assert result['reasons']


@pytest.mark.parametrize('clock,qualified', [
    (10**400, False), (-1, False), (True, False), (False, False),
    (float('inf'), False), (float('-inf'), False), (float('nan'), False),
    (0, True), (1, True), (0.125, True),
])
def test_clock_validation_returns_controlled_refusal(monkeypatch, clock, qualified):
    stats = run(Records(), monkeypatch)
    stats['samples_ms'][0] = clock
    if qualified:
        stats.update(bench._latency_summary(stats['samples_ms'], retain_samples=False))
    result = compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})
    assert result['qualified'] is qualified
    if not qualified:
        assert result['reasons'] == ['invalid or incomplete result population']
        cell = charter.metric_cell(
            [{'workloads': {'record_get_points': {'tofudb': stats, 'sqlite': deepcopy(stats)}}}],
            {'status': 'PASS', 'reasons': []}, 'record_get_points', {'name': 'p50_ms'}, 'sqlite', 1)
        assert cell['status'] == 'INSUFFICIENT'
        assert not cell.get('observations')


@pytest.mark.parametrize('field', ['mean_ms', 'p50_ms', 'p95_ms', 'p99_ms',
                                  'min_ms', 'max_ms', 'ops_per_sec'])
def test_inconsistent_summary_cannot_describe_another_clock_population(monkeypatch, capsys, field):
    native = run(Records(), monkeypatch)
    native.update(bench._latency_summary([100.0] * 4))
    native['sample_order'] = 'sequential-operation-start'
    sqlite = deepcopy(native)
    assert compare_results({'tofudb': native, 'sqlite': sqlite})['qualified']
    native[field] = 1
    sqlite[field] = 1000
    result = compare_results({'tofudb': native, 'sqlite': sqlite})
    assert not result['qualified']
    assert 'summary' in result['reasons'][0]
    cell = charter.metric_cell(
        [{'workloads': {'record_get_points': {'tofudb': native, 'sqlite': sqlite}}}],
        {'status': 'PASS', 'reasons': []}, 'record_get_points', {'name': field}, 'sqlite', 1)
    assert cell['status'] == 'INSUFFICIENT'
    assert not cell.get('observations')
    bench._print_report({'engines': ['tofudb', 'sqlite'],
                         'workloads': {'record_get_points': {'tofudb': native, 'sqlite': sqlite}}})
    summary = capsys.readouterr().out.split('tofudb speedup vs sqlite (mean latency ratio):')[1]
    assert 'unqualified' in summary
    assert 'FASTER' not in summary
    assert 'slower' not in summary
    assert '1000.00x' not in summary


def test_requalification_uses_collector_rounding_and_percentile_ranks(monkeypatch):
    stats = run(Records(), monkeypatch)
    stats.update(bench._latency_summary([0.12345678, 8.0, 3.0, 0.01]))
    stats['sample_order'] = 'sequential-operation-start'
    assert stats['mean_ms'] == 2.783364
    assert stats['p50_ms'] == 3.0
    assert stats['p95_ms'] == stats['p99_ms'] == 8.0
    assert compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})['qualified']


@pytest.mark.parametrize('value', [None, True, float('inf'), float('nan'), 10**400])
def test_invalid_summary_values_return_controlled_refusal(monkeypatch, value):
    stats = run(Records(), monkeypatch)
    stats['mean_ms'] = value
    result = compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})
    assert not result['qualified']
    assert result['reasons']


def test_unrepresentable_clock_aggregate_returns_controlled_refusal(monkeypatch):
    import sys

    stats = run(Records(), monkeypatch)
    stats['samples_ms'] = [sys.float_info.max] * 4
    result = compare_results({'tofudb': stats, 'sqlite': deepcopy(stats)})
    assert not result['qualified']
    assert result['reasons']
