"""Measurement verdicts fail closed on slow, absent or incomparable evidence."""
from copy import deepcopy
from uuid import uuid4
from functools import cache
import hashlib

import pytest

from benchmarks.conversation_acceptance import (
    POLICY, REPORT_FORMAT, SQLITE_BOUNDARIES, WRITER_METRICS_BOUNDARIES, assess,
)
from benchmarks.conversation_compare import backend_order, compare

from benchmarks.conversation_provenance import FORMAT, REQUIRED_SOURCES
pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/new_runtime_owner.py'}


def source_snapshot():
    return {'native_source_sha256': 'a' * 64,
            'files': {name: 'a' * 64 for name in (*REQUIRED_SOURCES, 'tofu_db/wire.py')}}


@pytest.fixture(autouse=True)
def synthetic_checkout(monkeypatch):
    # Verdict tests are pure/offline; no repository scan or native executable.
    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', source_snapshot)
    monkeypatch.setattr('benchmarks.conversation_compare.capture_sources', source_snapshot)


@cache
def expected_fixture_hashes():
    return {str(i): hashlib.sha256(b''.join(
        hashlib.shake_256(f'{i}:{j}'.encode()).hexdigest(4096).encode() for j in range(4)
    )).hexdigest() for i in range(1000)}


def evidence(backend='tofudb'):
    report = {'format': REPORT_FORMAT, 'backend': backend, 'correctness_passed': True, 'passed': True,
            'conversations': 1000, 'rounds': 4, 'durability': 'strict', 'admission_seconds': 2,
            'benchmark_source_sha256': 'a' * 64,
            'chunk_bytes': 8192, 'restart_while_running': True, 'stream_interval_ms': 1000,
            'rpc_deadline_seconds': POLICY['rpc_deadline_seconds'],
            'running_restart_ms': 10, 'restart_ms': 20, 'recovered_running': 1000,
            'peak_simultaneous_running': 1000, 'integrity_returncode': 0,
            'recovered_completed': 1000, 'recovered_exact_content': 1000,
            'recovered_exact_replay': 1000,
            'authority': {'backend': backend, 'crash_scope': 'sigkill', 'filesystem': 'fuse.bgfuse', 'volume_device': 42, 'rpc_deadline_seconds': POLICY['rpc_deadline_seconds']},
            'latency_ms': {stage: {'min': 0, 'p50': 0, 'p95': 0, 'p99': 100, 'max': 500 if stage == 'settle' else 2000, 'total_ms': 5000 if stage == 'settle' else 20000, 'count': 4000 if stage == 'stream_scheduled' else 1000}
                           for stage in POLICY['p99_budget_ms']}}

    latency = report['latency_ms']
    for stage in ('accept', 'bind', 'start', 'stream'):
        latency[stage] = {'count': 4000 if stage == 'stream' else 1000,
                          'min': 0, 'p50': 0, 'p95': 0, 'p99': 0, 'max': 0, 'total_ms': 0}
    latency['settle_scheduled'] = deepcopy(latency['settle'])
    execution = uuid4().hex
    directory = '/private/' + execution
    report['authority']['volume_inode'] = int(execution, 16)
    report.update(stream_event_kind='phase', terminal_barrier=False, carry_stream_events=False,
                  recover_interrupted=False, execution_id=execution, measurement_completed=True, error_count=0, errors=[], stage_failures={},
                  observed_stream_events=4000, expected_stream_events=4000,
                  readiness_observation={'probes': 1, 'unhealthy_probes': 0,
                                         'max_consecutive_unhealthy': 0, 'failure_examples': []},
                  recovered_content_sha256_by_index=dict(expected_fixture_hashes()),
                  event_window_monotonic_seconds={'clock': 'CLOCK_MONOTONIC', 'start': 5, 'finish': 9})
    report['storage_metrics_provenance'] = {
        key: {'process_id': pid, 'authority_incarnation': incarnation,
              'process_started_unix_seconds': 100 + incarnation, 'boundary': boundary,
              'benchmark_calls_quiescent': True, 'observed_monotonic_seconds': interval}
        for key, pid, incarnation, boundary, interval in (
            ('admission_storage_metrics', 11, 0, 'admission-before-running-restart', [1, 2]),
            ('productive_start_storage_metrics', 12, 1, 'recovered-before-provider-release', [3, 4]),
            ('storage_metrics', 12, 1, 'completed-before-terminal-restart', [9, 10]),
        )}
    report['authority']['authority_directory'] = directory
    report['crash_recovery_receipts'] = {
        'all_running_before_stream_release': {'killed_pid': 11, 'exit_code': -9, 'reopened_pid': 12, 'authority_directory': directory},
        'after_settlement': {'killed_pid': 12, 'exit_code': -9, 'reopened_pid': 13, 'authority_directory': directory},
    }
    if backend == 'sqlite':
        report['sqlite_loaded_artifacts'] = {
            boundary: {'pid': pid, 'sqlite': {'path': '/private/libsqlite3.so', 'sha256': 'a' * 64},
                       'vfs': {'path': '/private/tofu_wal_coalesce.so', 'sha256': 'b' * 64},
                       'writer_configuration_at_open': {'journal_mode': 'wal', 'synchronous': 2,
                                                        'coalescing_vfs_selected': True}}
            for boundary, pid in zip(SQLITE_BOUNDARIES, (11, 11, 12, 12, 12, 13, 13), strict=True)
        }
        report['authority']['sqlite_fastpath'] = {'active': False}
        for key in WRITER_METRICS_BOUNDARIES:
            row = report['sqlite_loaded_artifacts'][key]
            report[key] = {'writer_configuration_at_open': deepcopy(row['writer_configuration_at_open'])}

    artifact = None
    declared = None
    if backend == 'tofudb':
        report.update({key: {'walDurability': 'strict'} for key in WRITER_METRICS_BOUNDARIES})
        identity = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64}
        report['binary_sha256'] = 'b' * 64
        report['authority'].update(client_mode='application', build_identity=identity,
                                   checkout_source_sha256='a' * 64, source_matches_checkout=True)
        declared = {'binary_sha256': report['binary_sha256']}
        artifact = {**declared, 'build_identity': deepcopy(identity)}
    report['provenance'] = {
        'format': FORMAT, 'sources_before': source_snapshot(), 'sources_after': source_snapshot(),
        'declared_artifact': declared, 'artifact_before': deepcopy(artifact),
        'artifact_after': deepcopy(artifact),
    }
    return report

def test_complete_low_latency_evidence_passes_only_its_declared_scope():
    result = assess(evidence())
    assert result['passed']
    assert 'browser-send-to-visible-feedback' in result['uncovered']


@pytest.mark.parametrize('receipt', [None, [], {}, {'format': 'unsupported'}])
def test_unsupported_provenance_refuses_without_reading_the_checkout(monkeypatch, receipt):
    report = evidence()
    report['provenance'] = receipt

    def forbidden():
        raise AssertionError('unsupported provenance cannot use a checkout inventory')

    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', forbidden)
    result = assess(report)
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['passed'] and not result['latency_passed']
    assert not result['current_source_qualified'] and not result['frozen_build_comparable']
    assert 'provenance receipt missing or unsupported' in result['provenance']['frozen_failures']


def test_supported_provenance_captures_a_fresh_complete_inventory_on_every_assessment(monkeypatch):
    report = evidence()
    current = source_snapshot()
    observations = []

    def capture():
        observations.append(deepcopy(current))
        return deepcopy(current)

    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', capture)
    assert assess(report)['passed']
    current['files']['lib/new_runtime_owner.py'] = 'c' * 64
    result = assess(report)
    assert not result['current_source_qualified'] and not result['passed']
    assert result['frozen_build_comparable']
    assert len(observations) == 2 and observations[0] != observations[1]


@pytest.mark.parametrize('p99', [47000, 16534, float('nan'), float('inf'), None, -1])
def test_correct_content_does_not_make_slow_or_invalid_streaming_pass(p99):
    report = evidence()
    report['latency_ms']['stream_scheduled']['p99'] = p99
    result = assess(report)
    assert result['correctness_passed']
    assert not result['passed']
    assert not result['latency_passed']


@pytest.mark.parametrize('mutation', ['missing_metric', 'missing_sample', 'failed', 'smoke', 'no_restart', 'no_schedule'])
def test_incomplete_or_failed_evidence_cannot_pass(mutation):
    report = evidence()
    if mutation == 'missing_metric':
        del report['latency_ms']['stream_scheduled']
    elif mutation == 'missing_sample':
        report['latency_ms']['stream_scheduled']['count'] -= 1
    elif mutation == 'failed':
        report['correctness_passed'] = False
    elif mutation == 'smoke':
        report['conversations'] = 8
    elif mutation == 'no_restart':
        report['restart_while_running'] = False
    else:
        del report['stream_interval_ms']
    assert not assess(report)['passed']


def test_paired_repeats_retain_missing_failed_and_slower_baselines():
    legs = []
    for repeat in range(2):
        for backend in backend_order(repeat):
            report = evidence(backend)
            report['acceptance'] = assess(report)
            legs.append({'repeat': repeat, 'backend': backend, 'report': report, 'exit_code': 0})
    assert compare(legs, 2, 'fuse.bgfuse')['passed']
    assert not compare(legs[:-1], 2, 'fuse.bgfuse')['passed']
    assert not compare(legs, 2, 'ext4')['passed']
    changed = deepcopy(legs)
    changed[0]['report']['latency_ms']['stream_scheduled']['p99'] = 101
    assert not compare(changed, 2, 'fuse.bgfuse')['passed']
    changed = deepcopy(legs)
    changed[1]['report']['correctness_passed'] = False
    changed[1]['report']['latency_ms']['stream_scheduled']['p99'] = 1
    result = compare(changed, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in result['comparisons'] if row['repeat'] == 0)
    changed = deepcopy(legs)
    changed[0]['report']['rounds'] = 5
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['stream_event_kind'] = 'delta'
    changed[1]['report']['stream_event_kind'] = 'phase'
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['carry_stream_events'] = True
    changed[1]['report']['carry_stream_events'] = False
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['terminal_barrier'] = True
    changed[1]['report']['terminal_barrier'] = False
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['latency_ms']['stream_scheduled']['p99'] = float('nan')
    result = compare(changed, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert not next(row for row in result['comparisons']
                    if row['repeat'] == 0 and row['metric'] == 'stream_scheduled')['comparable']


def test_embedded_abandon_cannot_claim_process_crash_coverage():
    report = evidence()
    report['authority']['crash_scope'] = 'in-process-abandon'
    result = assess(report)
    assert result['correctness_passed'] and result['latency_passed']
    assert not result['coverage_passed'] and not result['passed']


@pytest.mark.parametrize('actual', [None, 5, 120])
def test_different_or_missing_rpc_deadlines_cannot_prove_comparison_coverage(actual):
    report = evidence()
    report['authority']['rpc_deadline_seconds'] = actual
    assert not assess(report)['coverage_passed']


def test_benchmark_deadline_is_scoped_and_reapplied_after_restart(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', '5')
    seen = []
    def launch():
        seen.append(os.environ['TOFU_STORAGE_RPC_DEADLINE_S'])
        return 1.0
    authority = ConversationAuthority('sqlite', None, tmp_path)
    authority.engine = SimpleNamespace(_client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds']),
                                      start=launch, crash_cycle=launch)
    authority.start()
    authority.crash_cycle()
    assert seen == [str(POLICY['rpc_deadline_seconds'])] * 2
    assert os.environ['TOFU_STORAGE_RPC_DEADLINE_S'] == '5'
    authority.engine._client._timeout = 5
    with pytest.raises(RuntimeError, match='RPC deadline differs'):
        authority.start()


@pytest.mark.parametrize('field', ['running_restart_ms', 'restart_ms'])
@pytest.mark.parametrize('value', [None, True, '10', float('nan'), float('inf'), -1])
def test_requested_restart_without_actual_measurement_leaves_coverage_gap(field, value):
    report = evidence()
    report[field] = value
    result = assess(report)
    assert result['correctness_passed'] and result['latency_passed']
    assert not result['coverage_passed'] and not result['passed']


@pytest.mark.parametrize('field', ['peak_simultaneous_running', 'recovered_running'])
@pytest.mark.parametrize('value', [None, 992])
def test_incomplete_running_recovery_cannot_claim_full_capacity_coverage(field, value):
    report = evidence()
    report[field] = value
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('value', [None, 1, False])
def test_skipped_or_failed_integrity_verification_leaves_coverage_gap(value):
    report = evidence()
    report['integrity_returncode'] = value
    assert not assess(report)['coverage_passed']


def test_unspecified_crash_mechanism_cannot_prove_process_crash():
    report = evidence()
    del report['authority']['crash_scope']
    assert not assess(report)['coverage_passed']


def test_zero_duration_restart_is_valid_measured_evidence():
    report = evidence()
    report['running_restart_ms'] = report['restart_ms'] = 0
    assert assess(report)['passed']


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_authority_declares_the_actual_process_crash_boundary(backend, tmp_path):
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    authority = ConversationAuthority(backend, tmp_path / 'tofu-db', tmp_path, native_client='application')
    authority.engine = SimpleNamespace(
        _data_dir=tmp_path, _project_root=tmp_path,
        _client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds'],
                                metrics=lambda **kwargs: {'fastpath': {'active': False}}),
        _build_identity={}, _checkout_source_sha256='')
    assert authority.describe()['crash_scope'] == 'sigkill'


@pytest.mark.parametrize('field', ['recovered_completed', 'recovered_exact_content', 'recovered_exact_replay'])
def test_partial_post_crash_readback_cannot_claim_full_recovery_coverage(field):
    report = evidence()
    report['carry_stream_events'] = True
    report[field] = 992
    assert not assess(report)['coverage_passed']


def test_startup_readiness_failure_retains_components_and_remains_a_failure():
    from benchmarks.conversation_concurrency import record_readiness_probe
    health = {'probes': 0, 'unhealthy_probes': 0, 'max_consecutive_unhealthy': 0,
              'max_contended_milliseconds': 0, 'failure_examples': []}
    consecutive = record_readiness_probe(health, {'ready': False, 'components': {'search': False}},
        phase='startup', elapsed_seconds=0.25)
    assert consecutive == 1 and health['unhealthy_probes'] == 1
    assert health['failure_examples'] == [{'phase': 'startup', 'elapsed_seconds': 0.25,
        'components': {'search': False}, 'authorityContendedMilliseconds': None, 'probe_error': None}]
    assert record_readiness_probe(health, {'ready': True}, phase='productive', elapsed_seconds=1,
        consecutive_unhealthy=consecutive) == 0
    assert health['unhealthy_probes'] == 1
    report = evidence(); report['correctness_passed'] = False
    assert not assess(report)['passed']


def test_productive_readiness_failures_keep_bounded_examples_and_exact_counts():
    from benchmarks.conversation_concurrency import record_readiness_probe
    health = {'probes': 0, 'unhealthy_probes': 0, 'max_consecutive_unhealthy': 0,
              'max_contended_milliseconds': 0, 'failure_examples': []}
    consecutive = 0
    for index in range(12):
        consecutive = record_readiness_probe(health, {'ready': False, 'probe_error': 'timeout',
            'authorityContendedMilliseconds': index}, phase='productive', elapsed_seconds=index,
            consecutive_unhealthy=consecutive)
    assert health['probes'] == health['unhealthy_probes'] == health['max_consecutive_unhealthy'] == 12
    assert health['max_contended_milliseconds'] == 11
    assert len(health['failure_examples']) == 8
    assert all(row['phase'] == 'productive' and row['probe_error'] == 'timeout' for row in health['failure_examples'])


def test_profiled_timings_cannot_become_capacity_admission():
    report = evidence()
    report['latency_comparison_eligible'] = False
    result = assess(report)
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['latency_passed'] and not result['passed']
    assert any('diagnostic profiling' in failure for failure in result['failures'])


@pytest.mark.parametrize('diagnostic', [
    {'requested_native_profile_every': '1'},
    {'requested_native_profile_every': 10},
    {'requested_native_profile_every': 'unknown'},
    {'requested_client_profile': True},
    {'client_stages_ms': {}},
    {'storage_metrics': {'requestProfile': {'enabled': True}}},
    {'admission_storage_metrics': {'requestProfile': {'enabled': True, 'available': False}}},

    {'productive_start_storage_metrics': {'requestProfile': {'enabled': True}}},
    {'productive_start_storage_metrics': {'requestProfile': {'sampleEvery': 1}}},
    {'terminal_boundary_storage_metrics': {'requestProfile': {'sampleEvery': 2}}},
])
def test_observed_profiling_cannot_be_overridden_by_missing_or_positive_wrapper_verdict(diagnostic):
    def apply_profile(report):
        for key, value in diagnostic.items():
            if key in WRITER_METRICS_BOUNDARIES or key == 'terminal_boundary_storage_metrics':
                report.setdefault(key, {}).update(value)
            else:
                report[key] = value
        report['latency_comparison_eligible'] = True

    report = evidence()
    apply_profile(report)
    result = assess(report)
    assert result['correctness_passed']
    # A terminal snapshot without the declared barrier is also contradictory.
    assert result['coverage_passed'] is ('terminal_boundary_storage_metrics' not in diagnostic)
    assert not result['passed'] and not result['latency_comparison_eligible']
    legs = paired_evidence()
    for leg in legs:
        apply_profile(leg['report'])
    assert all(assess(leg['report'])['current_source_qualified'] for leg in legs)
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'])


@pytest.mark.parametrize('source', [None, '', 'unknown', 'a' * 63])
def test_equal_missing_source_identities_do_not_prove_same_harness(source):
    legs = paired_evidence()
    for leg in legs:
        leg['report']['benchmark_source_sha256'] = source
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'])


@pytest.mark.parametrize('device', [None, True, '42', -1, 99])
def test_same_filesystem_name_does_not_prove_same_authority_volume(device):
    legs = [{'repeat': repeat, 'backend': backend, 'report': evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    for leg in legs:
        if leg['backend'] == 'sqlite':
            leg['report']['authority']['volume_device'] = device
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'])


def test_authority_volume_witness_comes_from_the_actual_directory(tmp_path):
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    authority = ConversationAuthority('tofudb', tmp_path / 'unused-binary', tmp_path)
    authority.engine = SimpleNamespace(_data_dir=tmp_path,
        _client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds']),
        _build_identity={'source_sha256': 'a' * 64}, _checkout_source_sha256='a' * 64)
    assert authority.describe()['volume_device'] == tmp_path.stat().st_dev


def paired_evidence():
    return [{'repeat': repeat, 'backend': backend, 'report': evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]


def test_stable_frozen_native_build_is_not_current_source_qualification():
    report = evidence()
    report['authority']['build_identity']['source_sha256'] = 'c' * 64
    report['authority']['source_matches_checkout'] = False
    for name in ('artifact_before', 'artifact_after'):
        report['provenance'][name]['build_identity']['source_sha256'] = 'c' * 64
    result = assess(report)
    assert result['frozen_build_comparable']
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['current_source_qualified'] and not result['passed']
    assert any('native build source differs' in reason for reason in result['failures'])
    legs = paired_evidence()
    legs[0]['report'] = report
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'] if row['repeat'] == 0)


@pytest.mark.parametrize(('path', 'value'), [
    (('provenance',), None),
    (('provenance',), []),
    (('provenance',), {}),
    (('provenance', 'format'), 'unknown'),
    (('provenance', 'sources_before'), None),
    (('provenance', 'sources_before', 'files'), []),
    (('provenance', 'sources_after'), None),
    (('provenance', 'sources_after', 'native_source_sha256'), 'c' * 64),
    (('provenance', 'declared_artifact'), None),
    (('provenance', 'declared_artifact', 'binary_sha256'), 'c' * 64),
    (('provenance', 'artifact_before'), None),
    (('provenance', 'artifact_after'), None),
    (('provenance', 'artifact_after', 'binary_sha256'), 'c' * 64),
    (('provenance', 'artifact_after', 'build_identity'), {}),
    (('authority',), None),
    (('authority', 'backend'), 'sqlite'),
    (('authority', 'client_mode'), None),
    (('authority', 'build_identity'), None),
    (('authority', 'build_identity', 'format'), 'unknown'),
    (('authority', 'build_identity', 'source_sha256'), 'c' * 64),
    (('authority', 'checkout_source_sha256'), None),
    (('authority', 'checkout_source_sha256'), 'unknown'),
    (('authority', 'checkout_source_sha256'), 'c' * 64),
    (('authority', 'source_matches_checkout'), None),
    (('authority', 'source_matches_checkout'), False),
    (('authority', 'source_matches_checkout'), 1),
    (('authority', 'source_matches_checkout'), 'true'),
    (('binary_sha256',), None),
    (('binary_sha256',), 'b' * 63),
    (('binary_sha256',), 'c' * 64),
    (('benchmark_source_sha256',), 'c' * 64),
])
def test_bad_provenance_cannot_qualify_or_publish_comparison_ratios(path, value):
    legs = paired_evidence()
    report = legs[0]['report']
    target = report
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    # A previously saved optimistic verdict is not an authority.
    report['acceptance'] = {'passed': True, 'current_source_qualified': True}
    result = assess(report)
    assert not result['passed'] and not result['current_source_qualified']
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'] if row['repeat'] == 0)


@pytest.mark.parametrize('name', [
    'benchmarks/conversation_authority.py', 'benchmarks/conversation_acceptance.py',
    'benchmarks/conversation_concurrency_contract.json', 'tofu_db/wire.py',
    'tofu_db/embedded.py', 'tofu_db/generated_contract.py', 'lib/storage/v2_client.py',
    'contracts/storage_v2.json',

    'tofu_workers/__init__.py', 'tofu_workers/executor.py', 'runtime_guards.py',
    'bootstrap_pkg/resource_policy_generated.py',
])
@pytest.mark.parametrize('mutation', ['missing', 'changed-during-run', 'old-checkout'])
def test_entire_relevant_inventory_is_required_not_only_entrypoint_hash(name, mutation):
    report = evidence()
    if mutation == 'missing':
        for phase in ('sources_before', 'sources_after'):
            del report['provenance'][phase]['files'][name]
    else:
        phases = ('sources_after',) if mutation == 'changed-during-run' else ('sources_before', 'sources_after')
        for phase in phases:
            report['provenance'][phase]['files'][name] = 'c' * 64
    assert report['benchmark_source_sha256'] == source_snapshot()['files']['benchmarks/conversation_concurrency.py']
    result = assess(report)
    assert not result['passed'] and not result['current_source_qualified']
    if mutation == 'changed-during-run':
        assert not result['frozen_build_comparable']


def test_missing_provenance_is_rejected_even_for_two_equal_old_reports():
    legs = paired_evidence()
    for leg in legs:
        del leg['report']['provenance']
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])


def test_a_complete_report_cannot_be_relabelled_as_the_other_backend():
    legs = paired_evidence()
    legs[1]['report'] = deepcopy(legs[0]['report'])
    assert assess(legs[1]['report'])['passed']
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'] if row['repeat'] == 0)


def test_failed_current_source_capture_never_falls_back_to_report_claims(monkeypatch):
    def unavailable():
        raise OSError('synthetic unreadable source')
    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', unavailable)
    monkeypatch.setattr('benchmarks.conversation_compare.capture_sources', unavailable)
    assert not assess(evidence())['current_source_qualified']
    result = compare(paired_evidence(), 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])


def pipeline_evidence(backend='tofudb'):
    report = evidence(backend)
    files = source_snapshot()['files']
    manifest = {'coordinator': files['lib/tasks_pkg/event_delivery.py'],
                'benchmark': files['benchmarks/storage_write_pipeline.py']}
    for name in ('lib/tasks_pkg/event_delivery_profile.py',
                 'lib/tasks_pkg/event_delivery_profile_metrics.py',
                 'lib/storage/generated_task_event_delivery_profile.py',
                 'contracts/task_event_delivery_v1.json'):
        manifest[name] = files[name]
    report['delivery_pipeline'] = {
        'contract': 'tofu.storage-write-pipeline/v1', 'submission_mode': 'barrier',
        'delivery_profile_every': 0, 'sources_unchanged': True,
        'source_manifest_sha256': manifest, 'delivery_source_sha256': manifest['coordinator'],
        'benchmark_source_sha256': manifest['benchmark'],
        'coordinator': {'performance_profile': {'enabled': False, 'sampleEvery': 0}},
    }
    return report


def test_current_unprofiled_delivery_pipeline_can_qualify():
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    assert all(assess(leg['report'])['passed'] for leg in legs)
    assert compare(legs, 2, 'fuse.bgfuse')['passed']


@pytest.mark.parametrize('observed', ['requested', 'enabled', 'sampleEvery'])
@pytest.mark.parametrize('verdict', [None, True])
def test_delivery_profiles_are_ineligible_even_when_wrapper_verdict_is_missing_or_optimistic(observed, verdict):
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    for leg in legs:
        report = leg['report']
        if verdict is not None:
            report['latency_comparison_eligible'] = verdict
        if observed == 'requested':
            report['delivery_pipeline']['delivery_profile_every'] = 1
        else:
            report['delivery_pipeline']['coordinator']['performance_profile'][observed] = 1
        result = assess(report)
        assert result['current_source_qualified']
        assert not result['latency_comparison_eligible'] and not result['passed']
    compared = compare(legs, 2, 'fuse.bgfuse')
    assert not compared['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in compared['comparisons'])


def test_foreign_frozen_delivery_source_is_comparable_evidence_not_current_qualification():
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    for leg in legs:
        pipeline = leg['report']['delivery_pipeline']
        # Existing frozen coordinator manifests use these two aliases. They
        # must not be laundered by unchanged/current disk sources around them.
        pipeline['source_manifest_sha256'] = {'coordinator': 'f' * 64, 'benchmark': 'a' * 64}
        pipeline['delivery_source_sha256'] = 'f' * 64
        result = assess(leg['report'])
        assert result['frozen_build_comparable']
        assert not result['current_source_qualified'] and not result['passed']
    compared = compare(legs, 2, 'fuse.bgfuse')
    assert not compared['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in compared['comparisons'])


@pytest.mark.parametrize(('field', 'value'), [
    ('contract', None), ('sources_unchanged', None), ('sources_unchanged', False),
    ('source_manifest_sha256', None), ('source_manifest_sha256', {}),
    ('source_manifest_sha256', {'coordinator': 'not-a-hash', 'benchmark': 'a' * 64}),
    ('delivery_source_sha256', None), ('delivery_source_sha256', 'f' * 64),
    ('benchmark_source_sha256', 'f' * 64),
])
def test_malformed_or_changed_delivery_source_receipts_cannot_qualify(field, value):
    report = pipeline_evidence()
    report['delivery_pipeline'][field] = value
    result = assess(report)
    assert not result['passed']
    assert not result['current_source_qualified'] and not result['frozen_build_comparable']


@pytest.mark.parametrize('other_work', ['direct', 'queued'])
def test_current_source_identity_does_not_make_different_delivery_work_comparable(other_work):
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    for leg in legs:
        if leg['backend'] == 'sqlite':
            if other_work == 'direct':
                del leg['report']['delivery_pipeline']
            else:
                leg['report']['delivery_pipeline']['submission_mode'] = other_work
                latency = leg['report']['latency_ms']
                latency['stream_submission'] = deepcopy(latency['stream_scheduled'])
                latency['stream_submission_scheduled'] = deepcopy(latency['stream_scheduled'])
    assert all(assess(leg['report'])['passed'] for leg in legs)
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])


@pytest.mark.parametrize('damage', ['missing', 'graceful', 'reopen-failed', 'other-store', 'other-child'])
def test_current_coverage_requires_both_verified_crash_cutpoints(damage):
    report = evidence()
    receipts = report['crash_recovery_receipts']
    name = 'after_settlement'
    if damage == 'missing':
        receipts.pop(name)
    elif damage == 'graceful':
        receipts[name]['exit_code'] = 0
    elif damage == 'reopen-failed':
        receipts[name].pop('reopened_pid')
    elif damage == 'other-store':
        receipts[name]['authority_directory'] = '/other'
    else:
        receipts[name]['killed_pid'] = 99
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('damage', ['missing', 'sqlite', 'vfs', 'wrong-child', 'legacy'])
def test_current_pair_refuses_changed_or_missing_sqlite_build_witnesses(damage):
    report = evidence('sqlite')
    report['storage_metrics'] = {'sqlite_version': '3.53.3'}
    assert assess(report)['current_source_qualified']
    row = report['sqlite_loaded_artifacts']['reopen_running']
    if damage == 'missing':
        report['sqlite_loaded_artifacts'].pop('final')
    elif damage in ('sqlite', 'vfs'):
        row[damage]['sha256'] = 'f' * 64  # Same filename and SQLite version.
    elif damage == 'wrong-child':
        row['pid'] = 99
    else:
        report['format'] = 'tofu.conversation-concurrency/v2'
    legs = [{'repeat': 0, 'backend': backend, 'report': item}
            for backend, item in [('tofudb', evidence()), ('sqlite', report)]]
    assert not assess(report)['current_source_qualified']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in compare(legs, 1, 'fuse.bgfuse')['comparisons'])


@pytest.mark.parametrize('changed', ['sqlite', 'vfs', 'native'])
def test_campaign_refuses_build_drift_even_when_each_leg_is_stable(changed):
    legs = [{'repeat': repeat, 'backend': backend, 'report': evidence(backend), 'exit_code': 0}
            for repeat in range(2) for backend in backend_order(repeat)]
    assert compare(legs, 2, 'fuse.bgfuse')['passed']
    report = legs[3 if changed == 'native' else 2]['report']
    if changed == 'native':
        report['binary_sha256'] = 'f' * 64
        for key in ('declared_artifact', 'artifact_before', 'artifact_after'):
            report['provenance'][key]['binary_sha256'] = 'f' * 64
    else:
        for row in report['sqlite_loaded_artifacts'].values():
            row[changed]['sha256'] = 'f' * 64
    assert assess(report)['passed']
    campaign = compare(legs, 2, 'fuse.bgfuse')
    assert not campaign['passed']
    assert any('build identity changed between repeats' in gap for gap in campaign['failures'])
    assert len(campaign['comparisons']) == 6
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None for row in campaign['comparisons'])


@pytest.mark.parametrize('field,value', [('synchronous', 1), ('journal_mode', 'delete'),
                                         ('coalescing_vfs_selected', False), ('missing', None)])
def test_mapped_sqlite_build_does_not_prove_writer_configuration(field, value):
    report = evidence('sqlite')
    reopened = report['sqlite_loaded_artifacts']['reopen_running']
    if field == 'missing':
        reopened.pop('writer_configuration_at_open')
    else:
        reopened['writer_configuration_at_open'][field] = value
    verdict = assess(report)
    assert verdict['current_source_qualified'] is (field != 'missing')
    assert not verdict['coverage_passed']


@pytest.mark.parametrize('mode', [None, 'normal'])
def test_native_strict_label_does_not_prove_effective_durability(mode):
    report = evidence()
    report['productive_start_storage_metrics']['walDurability'] = mode
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('damage', ['device', 'cadence', 'barrier', 'order', 'exit', 'timeout', 'one-repeat'])
def test_campaign_refusals_do_not_publish_formal_ratios(damage):
    legs = paired_evidence()
    repeats = 2
    if damage == 'order':
        legs[2:] = reversed(legs[2:])
    elif damage == 'exit':
        legs[-1]['exit_code'] = 9
    elif damage == 'timeout':
        legs[-1].update(exit_code=-9, timed_out=True)
    elif damage == 'one-repeat':
        legs, repeats = legs[:2], 1
    else:
        for leg in legs[2:]:
            report = leg['report']
            if damage == 'device':
                report['authority']['volume_device'] = 43
            elif damage == 'cadence':
                report['stream_interval_ms'] = 500
            else:
                report['terminal_barrier'] = True
    result = compare(legs, repeats, 'fuse.bgfuse')
    assert not result['passed']
    assert len(result['comparisons']) == 3 * repeats
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None for row in result['comparisons'])
    assert any('ineligible' in gap for gap in result['failures'])
    assert not any('slower' in gap for gap in result['failures'])
    if damage in ('exit', 'timeout'):
        assert any(('abnormal leg exit (9)' if damage == 'exit' else 'leg timed out') in gap
                   for gap in result['failures'])


def test_completed_latency_failure_still_allows_compared_ratios():
    legs = paired_evidence()
    leg = legs[0]
    leg.update(exit_code=1)
    leg['report']['passed'] = False
    leg['report']['latency_ms']['stream_scheduled'].update(p99=1500, total_ms=65000)
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['comparable'] for row in result['comparisons'])
    assert any('native P99 slower' in gap for gap in result['failures'])
    assert not any('abnormal' in gap or 'timed out' in gap for gap in result['failures'])


@pytest.mark.parametrize('cadence', [-1, float('nan'), float('inf'), True, '500'])
def test_invalid_cadence_refuses_coverage(cadence):
    report = evidence()
    report['stream_interval_ms'] = cadence
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('damage', ['config', 'pid', 'incarnation'])
def test_sqlite_rejects_contradictory_source_configuration(damage):
    report = evidence('sqlite')
    key = 'productive_start_storage_metrics'
    if damage == 'config':
        report[key]['writer_configuration_at_open'].update(synchronous=1, coalescing_vfs_selected=False)
    else:
        report['storage_metrics_provenance'][key]['process_id' if damage == 'pid' else 'authority_incarnation'] = 99
    assert any('contradicts source metrics or incarnation' in gap for gap in assess(report)['coverage_gaps'])


@pytest.mark.parametrize('damage', ['false-failure', 'stale-pass', 'exception', 'copied', 'witness', 'device', 'shape', 'samples'])
def test_campaign_reconciles_completion_and_requires_qualified_repeats(damage):
    legs = paired_evidence()
    leg = legs[2]
    report = leg['report']
    if damage in ('false-failure', 'exception'):
        leg['exit_code'] = 1
        report['passed'] = False
        if damage == 'exception':
            report['measurement_completed'] = False
    elif damage == 'stale-pass':
        report['latency_ms']['stream_scheduled']['p99'] = 1500
    elif damage == 'copied':
        leg['report'] = deepcopy(legs[1]['report'])
    elif damage == 'witness':
        report['sqlite_loaded_artifacts'].pop('final')
    elif damage == 'device':
        report['authority']['volume_device'] = 43
    elif damage == 'shape':
        report['stream_interval_ms'] = 500
    else:
        report['latency_ms']['stream_scheduled']['count'] = 3999
        report['passed'] = False
        leg['exit_code'] = 1
    verdict = compare(legs, 2, 'fuse.bgfuse')
    assert not verdict['passed']
    for row in verdict['comparisons']:
        formal = damage == 'samples' and row['metric'] != 'stream_scheduled'
        assert row['comparable'] is formal
        assert (row['tofudb_over_sqlite'] is not None) is formal


@pytest.mark.parametrize('field,value', [('error_count', 1), ('stage_failures', {'stream': 1}),
    ('observed_stream_events', 3999), ('readiness_observation', {'probes': 2, 'unhealthy_probes': 1}),
    ('recovered_content_sha256_by_index', {'0': 'a' * 64}), ('latency_ms', None)])
def test_incomplete_or_invalid_observations_cannot_pass(field, value):
    report = evidence()
    report[field] = value
    assert not assess(report)['passed']


@pytest.mark.parametrize('value', [True, False, None, -1, float('nan')])
def test_invalid_numeric_latency_is_not_compared(value):
    report = evidence()
    report['latency_ms']['settle']['p99'] = value
    assert assess(report)['metrics']['settle']['p99_ms'] is None


@pytest.mark.parametrize('damage', ['swap', 'birth', 'backwards', 'window', 'name', 'quiescence',
                                    'pid', 'incarnation', 'sync-type', 'vfs-type', 'relocation'])
def test_contradictory_durability_boundary_receipts_refuse(damage):
    report = evidence('tofudb' if damage in ('pid', 'incarnation') else 'sqlite')
    key, final = 'productive_start_storage_metrics', 'storage_metrics'
    origins = report['storage_metrics_provenance']
    if damage == 'swap':
        for envelope in (report, origins):
            envelope[key], envelope[final] = envelope[final], envelope[key]
    elif damage in ('sync-type', 'vfs-type'):
        config = report[key]['writer_configuration_at_open']
        config.update({'synchronous': 2.0} if damage == 'sync-type' else {'coalescing_vfs_selected': 1})
    elif damage == 'relocation':
        report['authority']['sqlite_fastpath']['active'] = True
    else:
        field, value = {
            'birth': ('process_started_unix_seconds', -1), 'backwards': ('observed_monotonic_seconds', [4, 3]),
            'window': ('observed_monotonic_seconds', [6, 7]), 'name': ('boundary', 'invented'),
            'quiescence': ('benchmark_calls_quiescent', False), 'pid': ('process_id', 999),
            'incarnation': ('authority_incarnation', 9),
        }[damage]
        origins[key][field] = value
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('backend', ['tofudb', 'sqlite'])
def test_terminal_barrier_requires_its_own_effective_settings(backend):
    report = evidence(backend)
    key = 'terminal_boundary_storage_metrics'
    report['terminal_barrier'] = True
    report['storage_metrics_provenance'][key] = {
        **report['storage_metrics_provenance']['productive_start_storage_metrics'],
        'boundary': 'stream-submission-before-settlement', 'benchmark_calls_quiescent': False,
        'observed_monotonic_seconds': [8, 8.5],
    }
    report[key] = deepcopy(report['productive_start_storage_metrics'])
    report['latency_ms']['settle_scheduled'] = shifted_summary(report['latency_ms']['settle'], 500)
    assert assess(report)['passed']
    report[key] = {'walDurability': 'normal', 'writer_configuration_at_open': {
        'journal_mode': 'wal', 'synchronous': 1, 'coalescing_vfs_selected': False}}
    assert not assess(report)['coverage_passed']


def test_timeout_with_truncated_child_report_retains_aggregate(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from benchmarks import conversation_compare as runner

    class Child:
        pid = 123

        def __init__(self, args, **kwargs):
            Path(args[args.index('--output') + 1]).write_text('{"passed":')
            self.killed = False

        def wait(self, *, timeout):
            if not self.killed:
                self.killed = True
                raise subprocess.TimeoutExpired('fixture', timeout)
            return -9

    output, engine = tmp_path / 'pair.json', tmp_path / 'engine'
    engine.touch()
    monkeypatch.setattr(runner.subprocess, 'Popen', Child)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: None)
    monkeypatch.setattr(sys, 'argv', ['runner', '--binary', str(engine), '--directory', str(tmp_path),
                                      '--output', str(output), '--repeats', '1'])
    assert runner.main() == 1
    retained = json.loads(output.read_text())
    assert not retained['acceptance']['passed']
    for leg in retained['legs']:
        assert leg['timed_out'] and leg['exit_code'] == -9
        assert 'JSONDecodeError' in leg['report_read_error']
        assert Path(leg['output']).read_text() == '{"passed":'
        assert Path(leg['log']).is_file()


def test_mount_resolution_rejects_sibling_string_prefix(monkeypatch):
    import io
    import os
    from pathlib import Path
    from scripts import tofudb_bench as bench

    monkeypatch.setattr(os.path, 'realpath', lambda path: path)
    monkeypatch.setattr(bench, 'open', lambda *a, **k: io.StringIO('root / ext4 rw\ndisk /data xfs rw\n'), raising=False)
    assert bench._mount_type(Path('/data/campaign')) == 'xfs'
    assert bench._mount_type(Path('/data-other/campaign')) == 'ext4'


@pytest.mark.parametrize('damage', ['slash', 'dot', 'inode', 'missing-barrier', 'missing-modes',
    'event-kind', 'readiness-example', 'readiness-streak', 'p99-above-max', 'float-pid',
    'huge-restart', 'missing-backend', 'null-leg', 'content-hash'])
def test_campaign_refuses_round_six_counterexamples(damage):
    legs = paired_evidence()
    report = legs[2]['report']
    if damage in ('slash', 'dot', 'inode'):
        original = legs[1]['report']['authority']
        if damage == 'inode':
            report['authority']['volume_inode'] = original.get('volume_inode', 1)
        else:
            directory = original['authority_directory'] + ('/' if damage == 'slash' else '/.')
            report['authority']['authority_directory'] = directory
            for row in report['crash_recovery_receipts'].values():
                row['authority_directory'] = directory
    elif damage == 'missing-barrier':
        report.pop('terminal_barrier', None)
        report['terminal_boundary_storage_metrics'] = {'writer_configuration_at_open': {'synchronous': 1}}
    elif damage in ('missing-modes', 'event-kind'):
        for leg in legs:
            for key in ('terminal_barrier', 'stream_event_kind', 'carry_stream_events', 'recover_interrupted'):
                if damage == 'missing-modes':
                    leg['report'].pop(key, None)
            if damage == 'event-kind':
                leg['report']['stream_event_kind'] = 'bogus'
    elif damage == 'readiness-example':
        report['readiness_observation']['failure_examples'] = [{'phase': 'startup'}]
    elif damage == 'readiness-streak':
        report['readiness_observation']['max_consecutive_unhealthy'] = 1
    elif damage == 'p99-above-max':
        report['latency_ms']['settle']['max'] = 50
    elif damage == 'float-pid':
        report['sqlite_loaded_artifacts']['final']['pid'] = 13.0
    elif damage == 'huge-restart':
        report['restart_ms'] = 10**400
    elif damage == 'missing-backend':
        legs[2].pop('backend')
    elif damage == 'null-leg':
        legs[2] = None
    else:
        report['recovered_content_sha256_by_index']['0'] = 'f' * 64
    verdict = compare(legs, 2, 'fuse.bgfuse')
    assert not verdict['passed']
    assert not any(row['comparable'] for row in verdict['comparisons'])


@pytest.mark.parametrize('damage', ['terminal-origin', 'terminal-witness', 'pid-overflow', 'nul-library',
                                    'percentile-order', 'timed-out-list', 'timed-out-int'])
def test_malformed_producer_shape_never_qualifies(damage):
    legs = paired_evidence()
    leg = legs[2]
    report = leg['report']
    if damage == 'terminal-origin':
        report['storage_metrics_provenance']['terminal_boundary_storage_metrics'] = {}
    elif damage == 'terminal-witness':
        report['sqlite_loaded_artifacts']['terminal_boundary_storage_metrics'] = {}
    elif damage == 'pid-overflow':
        report['crash_recovery_receipts']['after_settlement']['reopened_pid'] = 2**63
        for key in ('reopen_final', 'final'):
            report['sqlite_loaded_artifacts'][key]['pid'] = 2**63
    elif damage == 'nul-library':
        for row in report['sqlite_loaded_artifacts'].values():
            row['sqlite']['path'] = '/private/libsqlite3.so\x00invalid'
    elif damage == 'percentile-order':
        report['latency_ms']['settle']['p95'] = 500
    else:
        leg['timed_out'] = [] if damage == 'timed-out-list' else 0
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert not any(row['comparable'] for row in result['comparisons'])


@pytest.mark.parametrize('field', ['p50', 'p95', 'p99', 'max'])
@pytest.mark.parametrize('value', [None, True, '100', -1, float('inf')])
def test_producer_percentiles_require_finite_nonnegative_numbers(field, value):
    report = evidence()
    report['latency_ms']['settle'].update(p50=50, p95=95)
    report['latency_ms']['settle'][field] = value
    assert not assess(report)['passed']


@pytest.mark.parametrize('damage', ['terminal-witness', 'pre-submission', 'role', 'total-below-max', 'total-below-tail',
                                    'settle-phase', 'stream-phase', 'settle-scheduled-phase',
                                    'admission-phase', 'mixed-witness'])
def test_campaign_refuses_impossible_producer_evidence(damage):
    legs = paired_evidence()
    key = 'terminal_boundary_storage_metrics'
    for leg in legs:
        report = leg['report']
        report['terminal_barrier'] = True
        report[key] = deepcopy(report['productive_start_storage_metrics'])
        report['storage_metrics_provenance'][key] = {
            **report['storage_metrics_provenance']['productive_start_storage_metrics'],
            'boundary': 'stream-submission-before-settlement', 'benchmark_calls_quiescent': False,
            'observed_monotonic_seconds': [8, 8.5],
        }
        report['latency_ms']['settle_scheduled'] = shifted_summary(report['latency_ms']['settle'], 500)
    assert compare(legs, 2, 'fuse.bgfuse')['passed']
    report = legs[2]['report']
    if damage == 'terminal-witness':
        report['sqlite_loaded_artifacts'][key] = deepcopy(report['sqlite_loaded_artifacts']['final'])
    elif damage == 'pre-submission':
        report['storage_metrics_provenance'][key]['observed_monotonic_seconds'] = [6, 7]
    elif damage == 'role':
        for row in report['sqlite_loaded_artifacts'].values():
            row['sqlite'] = deepcopy(row['vfs'])
    elif damage == 'settle-phase':
        report['latency_ms']['settle']['max'] = 2000
    elif damage == 'stream-phase':
        report['latency_ms']['stream_scheduled']['max'] = 3500
    elif damage == 'settle-scheduled-phase':
        report['latency_ms']['settle_scheduled'] = {'max': 1001}
    elif damage == 'admission-phase':
        report['latency_ms']['submit_to_running']['max'] = 2001
    elif damage == 'mixed-witness':
        report['sqlite_loaded_artifacts']['final']['capture_error'] = 'failed capture'
    else:
        report['latency_ms']['settle'].update(p99=10, max=120,
            total_ms=1 if damage == 'total-below-max' else 150)
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert not any(row['comparable'] for row in result['comparisons'])


def test_producer_latency_summary_respects_rank_sum_bounds_and_coincident_ranks():
    from benchmarks.conversation_concurrency import latency_summary
    from benchmarks.conversation_receipts import latency_percentiles

    for size in (1, 2, 3, 20, 100, 1000):
        for samples in ([0.1] * size, [i / 17 for i in range(size)]):
            assert latency_percentiles(latency_summary(samples), size)
    impossible = latency_summary([1.0, 2.0])
    impossible['p50'] = 1.0  # All the reported ranks select the same final sample.
    assert not latency_percentiles(impossible, 2)
    impossible = latency_summary([1.0] * 1000)
    impossible['total_ms'] = 1001.0
    assert not latency_percentiles(impossible, 1000)


@pytest.mark.parametrize('queued', [False, True])
@pytest.mark.parametrize('barrier', [False, True])
def test_pipeline_clock_ownership_matrix(queued, barrier):
    from benchmarks.conversation_concurrency import latency_summary

    legs = [{'repeat': repeat, 'backend': backend, 'exit_code': 0,
             'report': pipeline_evidence(backend)}
            for repeat in range(2) for backend in backend_order(repeat)]
    key = 'terminal_boundary_storage_metrics'
    for leg in legs:
        report = leg['report']
        report.update(terminal_barrier=barrier, stream_interval_ms=0.5)
        report['event_window_monotonic_seconds']['finish'] = 5 + 6 / 1024
        if barrier:
            report[key] = deepcopy(report['productive_start_storage_metrics'])
            report['storage_metrics_provenance'][key] = {
                **report['storage_metrics_provenance']['productive_start_storage_metrics'],
                'boundary': 'stream-submission-before-settlement', 'benchmark_calls_quiescent': False,
                'observed_monotonic_seconds': [5 + 2 / 1024, 5 + 3 / 1024],
            }
        metrics = report['latency_ms']
        metrics['settle'] = latency_summary([1.0] * 1000)
        metrics['settle_scheduled'] = latency_summary([2.5 if barrier else 2.0] * 1000)
        for suffix in ('', '_scheduled'):
            lane = [0.25] * 4
            push = [4.375 - 0.5 * j for j in range(4)]
            metrics['stream' + suffix] = latency_summary((push if queued else lane) * 1000)
            if queued:
                metrics['stream_submission' + suffix] = latency_summary(lane * 1000)
        if queued:
            report['delivery_pipeline']['submission_mode'] = 'queued'
    assert compare(legs, 2, 'fuse.bgfuse')['passed']
    report = legs[2]['report']
    metric = 'stream_submission_scheduled' if queued else 'stream_scheduled'
    report['latency_ms'][metric] = latency_summary([3.0 if barrier else 6.0] * 4000)
    assert not any(row['comparable'] for row in compare(legs, 2, 'fuse.bgfuse')['comparisons'])
    if barrier:
        report['latency_ms'][metric] = latency_summary([0.25] * 4000)
        assert assess(report)['passed']
        report['latency_ms']['settle_scheduled'] = latency_summary([1.0] * 1000)
        assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_mapping_envelope_is_exclusive_to_sqlite_and_known_boundaries(backend):
    report = evidence(backend)
    report.setdefault('sqlite_loaded_artifacts', {})['unknown'] = {}
    assert not assess(report)['passed']


def shifted_summary(summary, offset):
    return {**summary, **{key: summary[key] + offset for key in ('min', 'p50', 'p95', 'p99', 'max')},
            'total_ms': summary['total_ms'] + summary['count'] * offset}


@pytest.mark.parametrize('damage', ['max-only', 'count', 'rank', 'total', 'missing-mode', 'unknown-mode',
                                    'bool-mode', 'missing-settlement', 'floor-mass', 'floor-plus-call'])
def test_pipeline_distributions_refuse_aggregate_contradictions(damage):
    from benchmarks.conversation_concurrency import latency_summary

    report = pipeline_evidence('sqlite')
    key = 'terminal_boundary_storage_metrics'
    report.update(terminal_barrier=True, stream_interval_ms=0.5)
    report['event_window_monotonic_seconds']['finish'] = 5 + 6 / 1024
    report[key] = deepcopy(report['productive_start_storage_metrics'])
    report['storage_metrics_provenance'][key] = {
        **report['storage_metrics_provenance']['productive_start_storage_metrics'],
        'boundary': 'stream-submission-before-settlement', 'benchmark_calls_quiescent': False,
        'observed_monotonic_seconds': [5 + 2 / 1024, 5 + 3 / 1024],
    }
    pipeline = report['delivery_pipeline']
    pipeline['submission_mode'] = 'queued'
    metrics = report['latency_ms']
    metrics.update(stream_scheduled=latency_summary([4.375, 3.875, 3.375, 2.875] * 1000),
                   stream_submission=latency_summary([0.25] * 4000),
                   stream_submission_scheduled=latency_summary([0.25] * 4000),
                   settle=latency_summary([0.5] * 1000), settle_scheduled=latency_summary([2] * 1000))
    assert assess(report)['passed']
    alias = metrics['stream_submission']
    if damage == 'max-only':
        metrics['stream_submission'] = {'max': 0.2}
    elif damage in ('count', 'rank', 'total'):
        field, value = {'count': ('count', 3999), 'rank': ('p95', 0.3), 'total': ('total_ms', 1)}[damage]
        alias[field] = value
    elif damage == 'missing-mode':
        pipeline.pop('submission_mode')
    elif damage in ('unknown-mode', 'bool-mode'):
        pipeline['submission_mode'] = 'unknown' if damage == 'unknown-mode' else False
    elif damage == 'missing-settlement':
        metrics.pop('settle_scheduled')
    elif damage == 'floor-mass':
        metrics['settle_scheduled'] = latency_summary([0] * 499 + [2] * 501)
    else:
        metrics['settle_scheduled'] = latency_summary([1.6] * 1000)
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('relation', ['direct', 'push', 'submission', 'settle', 'admission'])
def test_complete_latency_populations_respect_clock_inclusion(relation):
    from benchmarks.conversation_concurrency import latency_summary

    report = pipeline_evidence('sqlite')
    metrics = report['latency_ms']
    if relation in ('submission', 'push'):
        report['delivery_pipeline']['submission_mode'] = 'queued'
        for key in ('stream_submission', 'stream_submission_scheduled'):
            metrics[key] = latency_summary([0.25, 0.5] * 2000)
    if relation == 'admission':
        outer = 'submit_to_running'
        for name in ('accept', 'bind', 'start'):
            metrics[name] = latency_summary([1.0, 2.0] * 500)
        # Opposite ordering can flatten the enclosing population; no rank sums.
        metrics[outer] = latency_summary([4.0, 5.0] * 500)
        # Ranks individually fit, but the enclosing total misses serial work.
        bad = [4.0] * 1000
    else:
        inner = {'submission': 'stream_submission', 'settle': 'settle'}.get(relation, 'stream')
        size = 1000 if relation == 'settle' else 4000
        outer = inner + '_scheduled'
        metrics[inner] = latency_summary([0.25, 0.5] * (size // 2))
        metrics[outer] = latency_summary([0.375, 0.625] * (size // 2))
        bad = [0.0625, 0.625] * (size // 2)
    assert assess(report)['passed']
    metrics[outer] = latency_summary(bad)
    assert any('contradicts included latency clocks' in gap for gap in assess(report)['coverage_gaps'])


def test_clock_inclusion_handles_binary64_endpoint_rounding():
    from benchmarks.conversation_concurrency import latency_summary
    from benchmarks.conversation_receipts import inclusion_gaps

    # The same endpoint intervals can round differently when summed in ms.
    start, middle, end = 10.0, 10.000013, 10.000033
    call, floor, scheduled = [(b - a) * 1000 for a, b in
                              ((middle, end), (start, middle), (start, end))]
    values = {'settle': latency_summary([call] * 7),
              'settle_scheduled': latency_summary([scheduled] * 7)}
    assert not inclusion_gaps(values, 7, 1, False, floor)


@pytest.mark.parametrize('scheduled_count', [999, 1000])
@pytest.mark.parametrize('scheduled_ms', [1.0, 1.5])
def test_terminal_wait_floor_survives_incomplete_settlement_calls(scheduled_count, scheduled_ms):
    from benchmarks.conversation_concurrency import latency_summary

    report = evidence('sqlite')
    key = 'terminal_boundary_storage_metrics'
    report.update(terminal_barrier=True, stream_interval_ms=0.5)
    report['event_window_monotonic_seconds']['finish'] = 5 + 6 / 1024
    report[key] = deepcopy(report['productive_start_storage_metrics'])
    report['storage_metrics_provenance'][key] = {
        **report['storage_metrics_provenance']['productive_start_storage_metrics'],
        'boundary': 'stream-submission-before-settlement', 'benchmark_calls_quiescent': False,
        'observed_monotonic_seconds': [5 + 2 / 1024, 5 + 3 / 1024],
    }
    report['latency_ms'].update(
        stream=latency_summary([0.25] * 4000),
        stream_scheduled=latency_summary([0.25] * 4000),
        settle=latency_summary([1.0] * 999),
        settle_scheduled=latency_summary([scheduled_ms] * scheduled_count),
    )
    verdict = assess(report)
    gaps = verdict['coverage_gaps']
    assert any('excludes mandatory terminal wait' in gap for gap in gaps) is (scheduled_ms == 1.0)
    assert not any('contradicts included latency clocks' in gap for gap in gaps)
    assert verdict['coverage_passed'] is (scheduled_ms == 1.5 and scheduled_count == 1000)
    assert not verdict['metrics']['settle']['complete']
    assert not verdict['passed']
