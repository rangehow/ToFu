"""Charter closure is a deterministic assessment, not a wall-clock CI test."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from benchmarks.conversation_provenance import FORMAT, REQUIRED_SOURCES
from scripts import tofudb_charter_assess as charter


pytestmark = pytest.mark.unit

SOURCES = {'native_source_sha256': 'a' * 64,
           'files': {name: 'b' * 64 for name in REQUIRED_SOURCES}}
BUILD = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64, 'version': '0.2.0'}


def witness_spec(name):
    return next(item['witness'] for item in charter.POLICY['dimensions']
                if item.get('witness', {}).get('name') == name)


def witness_authority(engine, volume, transport=None):
    transport = transport or ('embedded' if engine == 'tofudb' else 'sidecar')
    artifact = None
    if engine == 'tofudb':
        artifact = {'binary_sha256': 'c' * 64}
        if transport == 'embedded':
            artifact['shared_library_sha256'] = 'd' * 64
    return {'engine': engine, 'transport': transport, 'artifact': artifact,
            'volume': {'filesystem': volume, 'device': 42}}


def history_witness(volume, native=(1.0, 1.0, 1.0), control=(3.0, 3.0, 3.0)):
    spec = witness_spec('history_sweep')
    engines = {}
    for engine, values in (('tofudb', native), ('sqlite', control)):
        engines[engine] = {'authorities': [witness_authority(engine, volume)], 'points': [
            {'length_index': index, 'workloads': {name: {'p50_ms': value} for name in spec['workloads']}}
            for index, value in enumerate(values)]}
    return {'workloads': spec['workloads'], 'engines': engines,
            'lengths': {name: [base * factor for factor in spec['length_multipliers']]
                        for name, base in zip(spec['workloads'], (400, 10))}}


def recovery_witness(volume):
    def entry(engine):
        return {'authorities': [witness_authority(engine, volume, 'daemon' if engine == 'tofudb' else None)],
                'second_authority': {'outcome': 'refused'},
                'rounds': [{'signal': 'SIGKILL', 'killed_pid': 4242, 'harness_pid': 7,
                            'separate_process': True, 'unacknowledged_at_kill': True,
                            'reopened': True, 'acknowledged_total': 100 * (round_ + 1),
                            'acknowledged_lost': 0, 'writable_after_recovery': True,
                            'unacknowledged_outcomes': {'present': 0, 'absent': 1, 'changed': 0}}
                           for round_ in range(3)]}
    return {'engines': {'tofudb': entry('tofudb'), 'sqlite': entry('sqlite')}}


def distracted_witness(volume, pressure_p99=2.0):
    spec = witness_spec('distracted_cohorts')
    cpus = [6, 7][:spec['cpus']]
    blocks = [{'condition': condition, 'benchmark_confined': True,
               'stressors': [{'alive_at_end': True, 'cpu_seconds': 1.5, 'affinity': cpus}
                             for _ in cpus] if condition == 'pressure' else []}
              for condition in ('idle', 'pressure', 'pressure', 'idle')]
    cohorts = {name: {'idle': {'iterations': 1000, 'p99_ms': 1.0},
                      'pressure': {'iterations': 1000, 'p99_ms': pressure_p99}}
               for name in spec['workloads']}
    return {'cpus': cpus, 'memory_pressure': {'bytes': 1 << 20}, 'engines': {
        engine: {'authorities': [witness_authority(engine, volume)], 'blocks': deepcopy(blocks),
                 'cohorts': deepcopy(cohorts)} for engine in ('tofudb', 'sqlite')}}


def horizontal_witness(volume, native=(100, 99, 98, 97), control=(50, 40, 35, 30)):
    sweep = witness_spec('horizontal_scaling')['readers']
    return {'readers': sweep, 'engines': {
        engine: {'authorities': [witness_authority(engine, volume)], 'readers': {
            str(readers): {'writes': 150, 'writer_ops_per_sec': rate,
                           'reads_completed': 500 if readers else 0,
                           'reads_overlapping_writer': 480 if readers else 0}
            for readers, rate in zip(sweep, rates)}}
        for engine, rates in (('tofudb', native), ('sqlite', control))}}


def witnessed_reports():
    """The synthetic cohorts plus complete joined witnesses; never a measurement."""
    rows = reports()
    for row in rows:
        volume = row['resources']['tofudb']['filesystem']
        row['charter_witnesses'] = {
            'format': 'tofu.storage-charter-witnesses/v1',
            'history_sweep': history_witness(volume), 'volume_recovery': recovery_witness(volume),
            'distracted_cohorts': distracted_witness(volume),
            'horizontal_scaling': horizontal_witness(volume)}
    return rows


def idle_rows(rows, volume=None):
    """The receipts witnesses are read from: unlabelled cohorts, optionally one volume."""
    return [row for row in rows if not row['measurement'].get('distracted_hardware')
            and (volume is None or row['resources']['tofudb']['filesystem'] == volume)]


def reports():
    """Complete synthetic operation cohorts; never a performance measurement."""
    workloads = sorted({name for dim in charter.POLICY['dimensions'] for name in dim.get('workloads', [])})
    artifact = {'binary_sha256': 'c' * 64, 'build_identity': BUILD,
                'shared_library_sha256': 'd' * 64, 'embedded_build_identity': BUILD}
    authority = {'backend': 'tofudb', 'build_identity': BUILD,
                 'checkout_source_sha256': 'a' * 64, 'source_matches_checkout': True,
                 'client_mode': 'embedded', 'shared_library_sha256': 'd' * 64,
                 'embedded_build_identity': BUILD}
    result = []
    for volume in ('xfs', 'fuse.bgfuse'):
        for stress in (False, True):
            for repeat in range(2):
                provenance = {}
                resources = {}
                for engine in charter.POLICY['required_engines']:
                    provenance[engine] = {
                        'format': FORMAT, 'sources_before': SOURCES, 'sources_after': SOURCES,
                        'declared_artifact': {'binary_sha256': 'c' * 64, 'shared_library_sha256': 'd' * 64} if engine == 'tofudb' else None,
                        'artifact_before': artifact if engine == 'tofudb' else None,
                        'artifact_after': artifact if engine == 'tofudb' else None,
                    }
                    resources[engine] = {'filesystem': volume, 'volume_device': 42, 'footprint': {
                        'schema_version': 1, 'timing': {'phase': 'post_workloads_before_shutdown', 'steady_state': False},
                        'total': {'complete': True, 'logical_bytes': 50 if engine == 'tofudb' else 100},
                    }}
                measurement = {
                    'native_client': 'embedded', 'profiling_requested': False,

                    'benchmark_source_sha256': 'b' * 64,
                    'runtime_policy': {
                        'tofudb': {phase: {'wal_durability': 'strict', 'profiling': False, 'profile_sample_every': 0}
                                   for phase in ('before', 'after')},
                        'sqlite': {phase: {'synchronous': 2, 'fastpath_active': False}
                                   for phase in ('before', 'after')},
                        'rawsqlite': {phase: {'synchronous': 2, 'journal_mode': 'wal'}
                                      for phase in ('before', 'after')},
                    },
                    'equivalent_durability': True, 'work_spec': {'scale': 'synthetic'},
                    'native_binary_sha256': 'c' * 64, 'host_identity': {'id': 'same-host', 'affinity': [0, 1]},
                    'collection_id': f'{volume}-{stress}-{repeat}',
                    'engine_order': ['tofudb', 'sqlite', 'rawsqlite'][::1 if repeat == 0 else -1],
                    'workload_order': workloads, 'provenance': provenance,
                    'authority': {'tofudb': authority},
                }
                if stress:
                    measurement['distracted_hardware'] = {'kind': 'fixture', 'observed_overlap': True}
                statistics = {}
                for workload in workloads:
                    statistics[workload] = {}
                    for engine, value in (('tofudb', 1), ('sqlite', 3), ('rawsqlite', 2)):
                        statistics[workload][engine] = {
                            'p50_ms': value, 'p99_ms': value, 'iterations': 1000,
                            'writer_p99_ms': value, 'writer_iterations': 1000,
                            'reader_p99_ms': value, 'reads_completed': 1000,
                            'ops_per_sec': 100 / value,

                            'mean_ms': value, 'shutdown': {'mean_ms': value},
                            'first_read': {'mean_ms': value}, 'stabilization_seconds': value,
                            'scope': 'fixture',
                        }
                result.append(deepcopy({
                    'schema': 'tofu.perf_compare.v2', 'scale': 'large', 'measurement': measurement,
                    'env': {'native': {'profile': False}, 'sqlite_synchronous': 2, 'sqlite_runtime': {'version': 'fixture'}},
                    'resources': resources, 'workloads': statistics,
                    'summary': {'failed_engines': {}}, 'skipped_engines': {},
                }))
    return result


def dimension(result, name):
    return next(item for item in result['dimensions'] if item['id'] == name)


def test_complete_operation_cells_pass_but_do_not_waive_missing_capacity():
    result = charter.assess(reports(), current_sources=SOURCES)
    gaps = {'history_read', 'degraded_fuse', 'horizontal_reads', 'distracted_hardware', 'conversation_capacity'}
    assert all(item['status'] == 'PASS' for item in result['dimensions'] if item['id'] not in gaps)
    assert all(dimension(result, name)['status'] == 'INSUFFICIENT' for name in gaps)
    assert dimension(result, 'history_read')['numeric_status'] == 'PASS'
    assert dimension(result, 'history_read')['witness_status'] == 'INSUFFICIENT'
    assert 'history_sweep witness missing' in charter.markdown(result)
    assert dimension(result, 'conversation_capacity')['status'] == 'INSUFFICIENT'
    assert result['comprehensive_victory'] is False
    assert result['status'] == 'INSUFFICIENT'


def test_joined_witnesses_close_three_clauses_and_leave_the_budget_to_the_owner():
    result = charter.assess(witnessed_reports(), current_sources=SOURCES)
    for name in ('history_read', 'degraded_fuse', 'horizontal_reads'):
        assert dimension(result, name)['status'] == 'PASS', name
    distracted = dimension(result, 'distracted_hardware')
    assert distracted['status'] == 'INSUFFICIENT'
    assert {cell['volume'] for cell in distracted['cells']} == {'local', 'fuse'}
    for cell in distracted['cells']:
        assert cell['reasons'] == ['budget requires owner decision']
        assert {row['ratio'] for row in cell['observations'] if row['engine'] == 'tofudb'} == {2.0}
    assert dimension(result, 'conversation_capacity')['status'] == 'INSUFFICIENT'
    assert result['comprehensive_victory'] is False


def set_history(rows, native, control):
    for row in rows:
        volume = row['resources']['tofudb']['filesystem']
        row['charter_witnesses']['history_sweep'] = history_witness(volume, native, control)


@pytest.mark.parametrize('native,control,expected', [
    ((1.0, 1.5, 2.0), (3.0, 3.0, 3.0), 'FAIL'),   # grows beyond its repeat range; sidecar flat
    ((1.0, 2.0, 4.0), (2.0, 4.0, 8.0), 'PASS'),   # grows, but no steeper than the sidecar
    ((1.0, 2.0, 4.0), (5.0, 5.0, 3.0), 'FAIL'),   # slower than the sidecar at the longest history
])
def test_history_flatness_is_relative_to_the_sidecar_scaling_in_the_same_run(native, control, expected):
    rows = witnessed_reports()
    set_history(rows, native, control)
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')
    assert gate['numeric_status'] == 'PASS'
    assert gate['witness_status'] == expected
    assert gate['status'] == expected


def test_flat_history_inside_its_repeat_range_passes_even_when_the_sidecar_is_flatter():
    rows = witnessed_reports()
    for volume in ('xfs', 'fuse.bgfuse'):
        for row, native in zip(idle_rows(rows, volume), ((1.0, 1.1, 1.05), (1.2, 1.0, 1.15))):
            row['charter_witnesses']['history_sweep'] = history_witness(volume, native, (3.0, 2.9, 2.8))
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')
    assert gate['witness_status'] == 'PASS'
    flatness = [row for cell in gate['cells'] if 'witness' in cell for row in cell['observations']
                if row['check'] == 'flatness']
    assert flatness and all(row['within_repeat_range'] for row in flatness)


def test_disagreeing_history_repeats_are_insufficient_not_averaged():
    rows = witnessed_reports()
    for volume in ('xfs', 'fuse.bgfuse'):
        for row, control in zip(idle_rows(rows, volume), ((2.0, 4.0, 8.0), (2.0, 2.0, 2.0))):
            row['charter_witnesses']['history_sweep'] = history_witness(volume, (1.0, 2.0, 4.0), control)
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')
    assert gate['witness_status'] == 'INSUFFICIENT'
    assert any('in some repeats' in reason for cell in gate['cells'] for reason in cell['reasons'])


def test_one_history_length_or_unmatched_points_cannot_establish_flat_cost():
    rows = witnessed_reports()
    for row in rows:
        sweep = row['charter_witnesses']['history_sweep']
        sweep['lengths'] = {name: values[:2] for name, values in sweep['lengths'].items()}
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')['status'] == 'INSUFFICIENT'
    rows = witnessed_reports()
    for row in rows:
        row['charter_witnesses']['history_sweep']['engines']['sqlite']['points'].pop()
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')['status'] == 'INCOMPARABLE'


@pytest.mark.parametrize('mutate,expected', [
    (lambda entry: entry['rounds'][1].update(acknowledged_lost=1), 'FAIL'),
    (lambda entry: entry['second_authority'].update(outcome='admitted'), 'FAIL'),
    (lambda entry: entry['rounds'][0].update(reopened=False), 'FAIL'),
    (lambda entry: entry['rounds'][2]['unacknowledged_outcomes'].update(changed=1, absent=0), 'FAIL'),
    (lambda entry: entry['rounds'][0].update(writable_after_recovery=False), 'FAIL'),
    (lambda entry: entry['second_authority'].update(outcome='error'), 'INSUFFICIENT'),
    (lambda entry: entry['rounds'][0].update(separate_process=False), 'INSUFFICIENT'),
    (lambda entry: entry['rounds'][0].update(unacknowledged_at_kill=False), 'INSUFFICIENT'),
    (lambda entry: entry.update(rounds=[]), 'INSUFFICIENT'),
])
def test_native_volume_lock_and_acknowledged_crash_recovery_decide_degraded_fuse(mutate, expected):
    rows = witnessed_reports()
    for row in idle_rows(rows, 'fuse.bgfuse'):
        mutate(row['charter_witnesses']['volume_recovery']['engines']['tofudb'])
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'degraded_fuse')
    assert gate['numeric_status'] == 'PASS'
    assert gate['witness_status'] == expected
    assert gate['status'] == expected


def test_sidecar_recovery_failure_is_reported_but_its_witness_must_exist():
    rows = witnessed_reports()
    for row in idle_rows(rows, 'fuse.bgfuse'):
        row['charter_witnesses']['volume_recovery']['engines']['sqlite']['rounds'][0]['acknowledged_lost'] = 2
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'degraded_fuse')
    assert gate['status'] == 'PASS'
    observations = [row for cell in gate['cells'] if 'witness' in cell for row in cell['observations']]
    assert {row['status'] for row in observations if row['engine'] == 'sqlite'} == {'FAIL'}
    for row in idle_rows(rows, 'fuse.bgfuse'):
        del row['charter_witnesses']['volume_recovery']['engines']['sqlite']
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'degraded_fuse')['status'] == 'INCOMPARABLE'


def test_local_recovery_witnesses_cannot_stand_in_for_the_fuse_volume():
    rows = witnessed_reports()
    for row in idle_rows(rows, 'fuse.bgfuse'):
        del row['charter_witnesses']['volume_recovery']
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'degraded_fuse')
    assert gate['status'] == 'INSUFFICIENT'
    assert {cell['volume'] for cell in gate['cells']} == {'fuse'}


@pytest.mark.parametrize('mutate', [
    lambda row: row['artifact'].update(binary_sha256='e' * 64),
    lambda row: row['artifact'].update(shared_library_sha256='e' * 64),
    lambda row: row['volume'].update(device=43),
    lambda row: row['volume'].update(filesystem='tmpfs'),
])
def test_witness_on_another_artifact_or_volume_is_incomparable(mutate):
    rows = witnessed_reports()
    mutate(idle_rows(rows)[0]['charter_witnesses']['history_sweep']['engines']['tofudb']['authorities'][0])
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'history_read')['status'] == 'INCOMPARABLE'


def test_failed_witness_execution_is_a_failure_and_malformed_sections_are_refused():
    rows = witnessed_reports()
    target = idle_rows(rows)[0]['charter_witnesses']
    target['horizontal_scaling'] = {'error': 'BenchError: writer completed 3/150 writes'}
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'horizontal_reads')['status'] == 'FAIL'
    target['horizontal_scaling'] = 'passed'
    with pytest.raises(charter.CatalogError, match='charter witnesses'):
        charter.assess(rows, current_sources=SOURCES)
    rows = witnessed_reports()
    idle_rows(rows)[0]['charter_witnesses']['horizontal_scaling']['engines']['tofudb']['readers'] = ['junk']
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'horizontal_reads')['status'] == 'INSUFFICIENT'


@pytest.mark.parametrize('mutate,reason', [
    (lambda witness: witness['engines']['tofudb']['blocks'][1].update(benchmark_confined=False), 'escaped the CPU set'),
    (lambda witness: witness['engines']['tofudb']['blocks'][2]['stressors'][0].update(cpu_seconds=0), 'did not overlap'),
    (lambda witness: witness['engines']['tofudb']['blocks'][2]['stressors'][0].update(affinity=[0, 1]), 'did not overlap'),
    (lambda witness: witness['engines']['tofudb']['blocks'][0]['stressors'].append({}), 'idle block ran stressors'),
    (lambda witness: witness['engines']['tofudb']['blocks'].append(
        witness['engines']['tofudb']['blocks'].pop(0)), 'not interleaved'),
    (lambda witness: witness['engines']['sqlite']['cohorts']['conversation_open']['idle'].update(iterations=999), 'samples'),
    (lambda witness: witness['memory_pressure'].update(bytes=0), 'memory pressure'),
    (lambda witness: witness.update(cpus=[1, 2, 3, 4]), 'CPU set'),
])
def test_unconfined_or_unmatched_pressure_cohorts_are_not_mistaken_for_the_budget_gap(mutate, reason):
    rows = witnessed_reports()
    mutate(idle_rows(rows)[0]['charter_witnesses']['distracted_cohorts'])
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'distracted_hardware')
    assert gate['status'] == 'INSUFFICIENT'
    local = next(cell for cell in gate['cells'] if cell['volume'] == 'local')
    assert any(reason in text for text in local['reasons'])
    assert 'budget requires owner decision' not in local['reasons']


@pytest.mark.parametrize('native,control,expected', [
    ((100, 50, 40, 30), (100, 99, 98, 97), 'FAIL'),   # readers block native, not the sidecar
    ((100, 50, 40, 30), (100, 50, 40, 30), 'PASS'),   # no worse than WAL-reader slowdown
    ((100, 101, 100, 99), (100, 99, 98, 97), 'PASS'),  # inside the writer-only repeat range
])
def test_reads_must_not_block_the_writer_relative_to_wal_reader_scaling(native, control, expected):
    rows = witnessed_reports()
    for row in rows:
        volume = row['resources']['tofudb']['filesystem']
        row['charter_witnesses']['horizontal_scaling'] = horizontal_witness(volume, native, control)
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'horizontal_reads')['status'] == expected


@pytest.mark.parametrize('name,dimension_id', [
    ('history_sweep', 'history_read'), ('volume_recovery', 'degraded_fuse'),
    ('distracted_cohorts', 'distracted_hardware'), ('horizontal_scaling', 'horizontal_reads'),
])
@pytest.mark.parametrize('engine,expected', [('tofudb', 'FAIL'), ('sqlite', 'INSUFFICIENT')])
def test_failed_native_leg_fails_and_failed_baseline_leg_is_unresolved(name, dimension_id, engine, expected):
    rows = witnessed_reports()
    for row in rows:
        row['charter_witnesses'][name]['engines'][engine] = {'error': 'BenchError: engine stalled'}
    gate = dimension(charter.assess(rows, current_sources=SOURCES), dimension_id)
    assert gate['witness_status'] == expected
    assert any(f'{engine} witness leg failed' in reason for cell in gate['cells'] for reason in cell['reasons'])


def test_writer_witness_without_overlapping_readers_is_insufficient():
    rows = witnessed_reports()
    points = idle_rows(rows)[0]['charter_witnesses']['horizontal_scaling']['engines']['tofudb']['readers']
    points['2']['reads_overlapping_writer'] = 0
    gate = dimension(charter.assess(rows, current_sources=SOURCES), 'horizontal_reads')
    assert gate['status'] == 'INSUFFICIENT'
    assert any('did not overlap' in reason for cell in gate['cells'] for reason in cell['reasons'])


def test_every_policy_witness_has_one_assessor_rule_and_one_harness_runner():
    from scripts import tofudb_perf_compare as bench
    from scripts.tofudb_workloads.witnesses import CharterWitnesses

    names = {item['witness']['name'] for item in charter.POLICY['dimensions'] if 'witness' in item}
    assert names == set(charter.WITNESS_RULES) == set(bench._CHARTER_WITNESS_SPECS)
    assert all(callable(getattr(CharterWitnesses, name)) for name in names)


def test_search_visibility_is_not_waived_by_fast_queries():
    rows = reports()
    for row in rows:
        row['workloads']['conversation_search']['tofudb']['stabilization_seconds'] = 12
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'search')['status'] == 'FAIL'


def test_zero_visibility_measurements_do_not_raise_or_hide_a_regression():
    rows = reports()
    for row in rows:
        for stats in row['workloads']['conversation_search'].values():
            stats['stabilization_seconds'] = 0
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'search')['status'] == 'PASS'
    for row in rows:
        row['workloads']['conversation_search']['tofudb']['stabilization_seconds'] = 1
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'search')['status'] == 'FAIL'
    json.dumps(result, allow_nan=False)


def test_maintenance_includes_shutdown_and_first_read():
    rows = reports()
    for row in rows:
        row['workloads']['maintenance_reopen']['tofudb']['shutdown']['mean_ms'] = 15
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'maintenance')['status'] == 'FAIL'


def test_failed_maintenance_is_a_failure_even_without_success_scope_fields():
    rows = reports()
    for row in rows:
        row['workloads']['maintenance_reopen']['tofudb'] = {'error': 'recovery read failed'}
    result = dimension(charter.assess(rows, current_sources=SOURCES), 'maintenance')
    assert result['status'] == 'FAIL'
    assert all(item['observed_status'] == 'FAIL' for item in result['cells'])


@pytest.mark.parametrize('family', ['record.get', 'credential.list'])
def test_maintenance_cannot_pass_with_a_reported_boot_read_gap(family):
    rows = reports()
    for row in rows:
        row['workloads']['maintenance_reopen']['tofudb']['cold_read_unsupported'] = {
            family: 'historical missing or failed cold read',
        }
    result = dimension(charter.assess(rows, current_sources=SOURCES), 'maintenance')
    assert result['status'] == 'INSUFFICIENT'
    assert any('cold-read' in reason for item in result['cells'] for reason in item['reasons'])


def test_empty_evidence_generates_actionable_missing_dimensions():
    result = charter.assess([], current_sources=SOURCES)
    assert len(result['dimensions']) == len(charter.POLICY['dimensions'])
    assert {item['status'] for item in result['dimensions']} == {'INSUFFICIENT'}
    text = charter.markdown(result)
    assert 'interactive_write' in text and 'conversation_capacity' in text
    assert 'no operation reports' in text


def test_repeated_receipt_is_not_an_independent_repeat():
    row = reports()[0]
    with pytest.raises(charter.CatalogError, match='duplicate'):
        charter.assess([row, deepcopy(row)], current_sources=SOURCES)


def test_duplicate_collection_id_is_incomparable_even_if_bytes_differ():
    rows = reports()
    rows[1]['measurement']['collection_id'] = rows[0]['measurement']['collection_id']
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_small_sample_p99_does_not_inherit_claimed_sufficiency():
    rows = reports()
    for row in rows:
        for values in row['workloads']['turn_append_large'].values():
            values.update(iterations=20, p99_sample_count_sufficient=True)
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_write')['status'] == 'INSUFFICIENT'
    assert dimension(result, 'interactive_read')['status'] == 'PASS'


def test_known_current_regression_is_visible_despite_other_missing_evidence():
    rows = reports()
    for row in rows:
        row['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'FAIL'
    assert dimension(result, 'engine_floor')['status'] == 'FAIL'
    assert result['status'] == 'FAIL'
    assert result['comprehensive_victory'] is False


def test_opposite_repeat_outcomes_are_insufficient_not_averaged():
    rows = reports()
    rows[0]['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_profiling_and_missing_provenance_are_not_admitted():
    rows = reports()
    rows[0]['measurement']['profiling_requested'] = True
    rows[4]['measurement'].pop('provenance')
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'
    assert not result['comprehensive_victory']


def test_frozen_source_is_historical_not_a_current_regression():
    current = deepcopy(SOURCES)
    current['files']['lib/storage/v2_client.py'] = 'f' * 64
    rows = reports()
    for row in rows:
        row['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=current)
    gate = dimension(result, 'interactive_read')
    assert gate['status'] == 'HISTORICAL'
    assert any(item.get('observed_status') == 'FAIL' for item in gate['cells'])


def test_changed_native_artifact_is_incomparable():
    rows = reports()
    rows[0]['measurement']['provenance']['tofudb']['artifact_after']['binary_sha256'] = 'f' * 64
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_host_and_work_mismatch_cannot_hide_in_pooled_results():
    rows = reports()
    rows[1]['measurement']['host_identity'] = {'id': 'other-host'}
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_reverse_order_is_required():
    rows = reports()
    rows[1]['measurement']['engine_order'] = rows[0]['measurement']['engine_order'][:]
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_footprint_requires_total_scope_not_legacy_root_only():
    rows = reports()
    rows[0]['resources']['tofudb'].pop('footprint')
    rows[0]['resources']['tofudb']['data_size_bytes'] = 1
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'footprint')['status'] == 'INSUFFICIENT'


def test_footprint_phases_are_not_interchangeable():
    rows = reports()
    rows[0]['resources']['sqlite']['footprint']['timing']['phase'] = 'after_close'
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'footprint')['status'] == 'INCOMPARABLE'


def test_idle_hardware_does_not_satisfy_distracted_dimension():
    rows = [row for row in reports() if not row['measurement'].get('distracted_hardware')]
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'distracted_hardware')['status'] == 'INSUFFICIENT'
    assert dimension(result, 'interactive_read')['status'] == 'PASS'


def test_unknown_volume_is_not_silently_called_local():
    rows = reports()
    for row in rows:
        row['resources']['tofudb']['filesystem'] = 'nfs4'
    result = charter.assess(rows, current_sources=SOURCES)
    assert result['unclassified']
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_summary_and_nonfinite_values_are_refused():
    with pytest.raises(charter.CatalogError, match='raw perf_compare'):
        charter.assess([{'decision': 'victory', 'passed': True}], current_sources=SOURCES)
    rows = reports()
    rows[0]['workloads']['record_get_points']['tofudb']['p50_ms'] = float('nan')
    with pytest.raises(ValueError):
        charter.assess(rows, current_sources=SOURCES)


def test_policy_has_all_charter_dimensions_and_no_aggregate_speedup():
    ids = [item['id'] for item in charter.POLICY['dimensions']]
    assert len(ids) == len(set(ids)) == 13
    assert {'engine_floor', 'footprint', 'distracted_hardware', 'swarm_write_tail', 'conversation_capacity'} <= set(ids)
    assert all((charter.ROOT / item['owner']).is_file() for item in charter.POLICY['dimensions'])
    assert dimension(charter.assess([], current_sources=SOURCES), 'engine_floor')['status'] == 'INSUFFICIENT'


def test_cli_refuses_curated_input_without_writing(tmp_path, monkeypatch, capsys):
    import benchmarks.conversation_provenance as provenance
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({'passed': True}))
    output = tmp_path / 'report.json'
    assert charter.main([str(source), '--json-output', str(output)]) == 2
    assert not output.exists()
    assert 'refused' in capsys.readouterr().err


def test_cli_writes_deterministic_outputs_and_cannot_overwrite(tmp_path, monkeypatch):
    import benchmarks.conversation_provenance as provenance
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    output, markdown = tmp_path / 'report.json', tmp_path / 'report.md'
    assert charter.main(['--json-output', str(output), '--markdown-output', str(markdown)]) == 1
    assert json.loads(output.read_text())['status'] == 'INSUFFICIENT'
    assert 'INSUFFICIENT' in markdown.read_text()
    assert charter.main(['--json-output', str(output), '--markdown-output', str(markdown)]) == 1
    output.write_text('user-owned conflicting content')
    assert charter.main(['--json-output', str(output)]) == 2
    assert output.read_text() == 'user-owned conflicting content'


def test_cli_help_never_loads_native_or_creates_authority():
    command = subprocess.run([sys.executable, str(charter.ROOT / 'scripts/tofudb_charter_assess.py'), '--help'],
                             capture_output=True, text=True, check=False)
    assert command.returncode == 0
    assert '--markdown-output' in command.stdout


@pytest.mark.parametrize('engine,field,value', [
    ('tofudb', 'profiling', True),
    ('tofudb', 'profile_sample_every', 1),
    ('tofudb', 'wal_durability', 'normal'),
    ('sqlite', 'synchronous', 1),
    ('sqlite', 'fastpath_active', True),
    ('rawsqlite', 'synchronous', 1),
    ('rawsqlite', 'journal_mode', 'delete'),
])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_runtime_observations_override_optimistic_summary(engine, field, value, phase):
    row = reports()[0]
    row['measurement']['runtime_policy'][engine][phase][field] = value
    assert charter.qualify(row, SOURCES)['status'] == 'INSUFFICIENT'


def test_missing_runtime_observation_is_not_proof_of_equivalent_durability():
    row = reports()[0]
    del row['measurement']['runtime_policy']['tofudb']['after']
    assert charter.qualify(row, SOURCES)['status'] == 'INSUFFICIENT'


def test_conflicting_benchmark_source_is_incomparable():
    row = reports()[0]
    row['measurement']['benchmark_source_sha256'] = 'f' * 64
    assert charter.qualify(row, SOURCES)['status'] == 'INCOMPARABLE'


def test_mixed_engine_sources_are_incomparable_not_merely_historical():
    row = reports()[0]
    # Break intentional fixture sharing before simulating independent legs.
    receipt = deepcopy(row['measurement']['provenance']['sqlite'])
    receipt['sources_before']['files']['lib/storage/v2_client.py'] = 'f' * 64
    receipt['sources_after'] = deepcopy(receipt['sources_before'])
    row['measurement']['provenance']['sqlite'] = receipt
    assert charter.qualify(row, SOURCES)['status'] == 'INCOMPARABLE'


def test_actual_collector_fields_are_accepted_by_charter(monkeypatch, tmp_path):
    """Exercise collector/assessor composition, not a hand-authored passed flag."""
    from types import SimpleNamespace
    import benchmarks.conversation_provenance as provenance
    from scripts import tofudb_perf_compare as bench

    row = reports()[0]
    receipt = row['measurement']['provenance']['tofudb']
    binary = tmp_path / 'tofu-db'
    binary.write_bytes(b'synthetic-artifact-never-executed')

    (binary.parent / 'libtofu_db.so').write_bytes(b'synthetic-library-never-loaded')
    engines = {'tofudb': SimpleNamespace(
        _binary=binary, _client_mode='embedded', _build_identity=BUILD,
        _embedded_build_identity=BUILD,
        _checkout_source_sha256=SOURCES['native_source_sha256']),
        'sqlite': object(), 'rawsqlite': object()}
    runtime = {name: values['before'] for name, values in row['measurement']['runtime_policy'].items()}
    meta = bench._measurement_metadata(engines)
    meta['benchmark_source_sha256'] = SOURCES['files']['scripts/tofudb_perf_compare.py']
    for name in ('collection_id', 'host_identity', 'workload_order', 'work_spec'):
        meta[name] = row['measurement'][name]
    row['measurement'] = meta
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    monkeypatch.setattr(provenance, 'capture_artifact', lambda *args, **kwargs: receipt['artifact_before'])
    monkeypatch.setattr(bench, '_operation_runtime_evidence', lambda name, engine: runtime[name])
    bench._finish_operation_evidence(
        row, engines, SOURCES, {'tofudb': receipt['declared_artifact']},
        {'tofudb': receipt['artifact_before']}, runtime)
    assert charter.qualify(row, SOURCES) == {'status': 'PASS', 'reasons': []}
