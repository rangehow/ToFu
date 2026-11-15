"""Charter witness emitters with deterministic in-memory authorities; never a measurement."""
from collections import Counter
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import tofudb_charter_assess as charter
from scripts import tofudb_perf_compare as bench
from scripts.tofudb_workloads.witnesses import CharterWitnesses, WitnessPorts

pytestmark = pytest.mark.unit

SCALE = bench._SCALES['tiny']
DEFINITIONS = {item['witness']['name']: item for item in charter.POLICY['dimensions'] if 'witness' in item}


class FakeAuthority:
    """Thread-safe in-memory authority exposing the witness engine ports."""

    def __init__(self, name, events, *, block_after=None, lose_last=False):
        self.name = name
        self.events = events
        self.lock = threading.Lock()
        self.records = {}
        self.turns = Counter()
        self.block_after = block_after
        self.puts = 0
        self.last = None
        self.lose_last = lose_last
        self.killed = threading.Event()
        self.stopped = False

    def execute(self, operation, payload, command):
        if operation == 'record.put':
            with self.lock:
                blocked = self.killed.is_set() or (
                    self.block_after is not None and self.puts >= self.block_after)
            if blocked:
                self.killed.wait(30)
                raise ConnectionError('authority process was killed')
            with self.lock:
                self.records[payload['namespace'], payload['key']] = payload['value']
                self.puts += 1
                self.last = payload['namespace'], payload['key']
            return {}
        if operation == 'record.get':
            value = self.records.get((payload['namespace'], payload['key']))
            return None if value is None else {'value': value, 'version': 1}
        if operation == 'turn.append_settled':
            with self.lock:
                self.turns[payload['conversation_id']] += 1
            return {}
        if operation == 'turn.sync.snapshot':
            self.events.append((self.name, 'open', self.turns[payload['conversation_id']]))
            return {}
        if operation == 'conversation.list':
            sidebar = [count for conversation, count in self.turns.items() if '_sbh_' in conversation]
            self.events.append((self.name, 'list', min(sidebar, default=0), max(sidebar, default=0)))
            return {'items': []}
        if operation in ('conversation.create', 'conversation.get'):
            return {}
        raise AssertionError(f'unexpected operation {operation}')

    def sigkill_authority(self):
        self.killed.set()
        return os.getpid() + 1

    def reopen_after_sigkill(self):
        with self.lock:
            if self.lose_last:
                self.records.pop(self.last)
            self.killed.clear()
            # The witness writes one writability probe, then the next round.
            self.block_after = self.puts + 1 + SCALE['crash_appends']
        return 1.5

    def second_authority_attempt(self):
        return {'mechanism': 'fake', 'outcome': 'refused', 'diagnostic': 'lease held'}

    def stop(self):
        self.stopped = True
        self.events.append((self.name, 'stop'))


def authority_row(engine):
    return {'engine': engine.name, 'transport': 'embedded' if engine.name == 'tofudb' else 'sidecar',
            'artifact': None, 'volume': {'filesystem': 'xfs', 'device': 42}}


def witnesses(events, engines=None, *, host=None, measure=bench.measure_workload, order=('tofudb', 'sqlite')):
    engines = engines if engines is not None else {}

    def fresh_engine(name, **options):
        engine = engines.get(name) or FakeAuthority(name, events)
        engines[name] = engine
        events.append((name, 'start', options))
        return engine

    ports = WitnessPorts(fresh_engine=fresh_engine, describe=authority_row, process_ids=lambda engine: [],
                         measure=measure, minimum_tail_samples=bench._MINIMUM_TAIL_SAMPLES,
                         **({'host': host} if host is not None else {}))
    return CharterWitnesses(inputs=bench._workload_inputs(SCALE, bench._OWNER_ID, ''),
                            ports=bench._workload_ports(), witness=ports, engine_order=order,
                            specs=bench._CHARTER_WITNESS_SPECS)


def test_history_sweep_grows_one_matched_corpus_per_engine_and_alternates_engines():
    events = []
    witness = witnesses(events).run('history_sweep')
    spec = bench._CHARTER_WITNESS_SPECS['history_sweep']
    expected = {'open_windowed_long_history': [SCALE['history_turns'] * f for f in spec['length_multipliers']],
                'sidebar_with_history': [SCALE['sidebar_hist_turns'] * f for f in spec['length_multipliers']]}
    assert witness['lengths'] == expected
    assert len(expected['open_windowed_long_history']) >= spec['minimum_lengths']
    measured = [event for event in events if event[1] in ('open', 'list')]
    reads = SCALE['reads'] + 5  # measure_workload's warmup reads the same corpus
    for index, (open_turns, sidebar_turns) in enumerate(zip(*expected.values())):
        for slot, engine in enumerate(('tofudb', 'sqlite')):
            start = (index * 2 + slot) * 2 * reads
            block = measured[start:start + 2 * reads]
            assert {event[:3] for event in block[:reads]} == {(engine, 'open', open_turns)}
            assert {event for event in block[reads:]} == {(engine, 'list', sidebar_turns, sidebar_turns)}
    stops = [index for index, event in enumerate(events) if event[1] == 'stop']
    assert len(stops) == 2 and min(stops) > max(index for index, event in enumerate(events)
                                                 if event[1] in ('open', 'list'))
    for engine in ('tofudb', 'sqlite'):
        points = witness['engines'][engine]['points']
        assert [point['length_index'] for point in points] == [0, 1, 2]
        assert all(point['workloads'][name]['iterations'] == SCALE['reads']
                   for point in points for name in spec['workloads'])
    observed = charter.history_rule([witness, witness], spec, DEFINITIONS['history_sweep'])
    assert not [reason for reason in observed['reasons'] if 'required' in reason or 'match' in reason]


@pytest.mark.parametrize('failing,expected', [('sqlite', 'INSUFFICIENT'), ('tofudb', 'FAIL')])
def test_history_sweep_keeps_the_other_engine_when_one_leg_fails(failing, expected):
    events = []

    class FailsBeyondFirstLength(FakeAuthority):
        def execute(self, operation, payload, command):
            conversation = payload.get('conversation_id', '')
            if (operation == 'turn.append_settled' and conversation.endswith('_history_open')
                    and self.turns[conversation] >= SCALE['history_turns']):
                raise RuntimeError('WAL barrier wait exceeded its deadline')
            return super().execute(operation, payload, command)

    witness = witnesses(events, {failing: FailsBeyondFirstLength(failing, events)}).run('history_sweep')
    other = 'tofudb' if failing == 'sqlite' else 'sqlite'
    assert 'WAL barrier wait exceeded' in witness['engines'][failing]['error']
    assert [point['length_index'] for point in witness['engines'][other]['points']] == [0, 1, 2]
    assert sum(event[1] == 'stop' for event in events) == 2
    spec = bench._CHARTER_WITNESS_SPECS['history_sweep']
    assert charter.history_rule([witness, witness], spec, DEFINITIONS['history_sweep'])['status'] == expected


def test_volume_recovery_kills_mid_write_and_reads_back_every_acknowledged_write():
    events = []
    engines = {name: FakeAuthority(name, events, block_after=SCALE['crash_appends'])
               for name in ('tofudb', 'sqlite')}
    witness = witnesses(events, engines).run('volume_recovery')
    assert [event[2] for event in events if event[1] == 'start'] == [{'daemon': True}] * 2
    for name in ('tofudb', 'sqlite'):
        entry = witness['engines'][name]
        assert entry['second_authority']['outcome'] == 'refused'
        assert len(entry['rounds']) == SCALE['maintenance_rounds']
        for number, row in enumerate(entry['rounds'], start=1):
            assert row['signal'] == 'SIGKILL' and row['separate_process'] is True
            assert row['unacknowledged_at_kill'] is True and row['reopened'] is True
            assert row['acknowledged_before_kill'] == number * (SCALE['crash_appends'] + 1) - 1
            assert row['acknowledged_total'] == row['acknowledged_before_kill']
            assert row['acknowledged_lost'] == 0
            assert row['unacknowledged_outcomes'] == {'present': 0, 'absent': 1, 'changed': 0}
            assert row['writable_after_recovery'] is True
    observed = charter.recovery_rule([witness, witness], {}, DEFINITIONS['volume_recovery'])
    assert observed['status'] == 'PASS', observed


def test_volume_recovery_reports_a_lost_acknowledged_write():
    events = []
    engines = {'tofudb': FakeAuthority('tofudb', events, block_after=SCALE['crash_appends'], lose_last=True),
               'sqlite': FakeAuthority('sqlite', events, block_after=SCALE['crash_appends'])}
    witness = witnesses(events, engines).run('volume_recovery')
    assert witness['engines']['tofudb']['rounds'][0]['acknowledged_lost'] == 1
    assert witness['engines']['sqlite']['rounds'][0]['acknowledged_lost'] == 0
    observed = charter.recovery_rule([witness, witness], {}, DEFINITIONS['volume_recovery'])
    assert observed['status'] == 'FAIL'


def test_writer_failure_before_the_kill_is_not_a_crash_observation():
    events = []

    class Refusing(FakeAuthority):
        def execute(self, operation, payload, command):
            if operation == 'record.put':
                raise RuntimeError('disk full')
            return super().execute(operation, payload, command)

    engines = {'tofudb': Refusing('tofudb', events),
               'sqlite': FakeAuthority('sqlite', events, block_after=SCALE['crash_appends'])}
    witness = witnesses(events, engines).run('volume_recovery')
    assert 'stopped before SIGKILL: RuntimeError: disk full' in witness['engines']['tofudb']['error']
    assert engines['tofudb'].stopped
    assert witness['engines']['sqlite']['rounds'][0]['acknowledged_lost'] == 0
    observed = charter.recovery_rule([witness, witness], {}, DEFINITIONS['volume_recovery'])
    assert observed['status'] == 'FAIL'


class YieldingWrites(FakeAuthority):
    """Each write yields the interpreter, as a real durable commit does."""

    def execute(self, operation, payload, command):
        if operation == 'record.put':
            time.sleep(0.002)
        return super().execute(operation, payload, command)


def test_horizontal_scaling_mirrors_reader_counts_and_requires_overlap():
    events = []
    engines = {name: YieldingWrites(name, events) for name in ('tofudb', 'sqlite')}
    witness = witnesses(events, engines).run('horizontal_scaling')
    sweep = bench._CHARTER_WITNESS_SPECS['horizontal_scaling']['readers']
    assert witness['readers'] == sweep and witness['block_order'] == sweep + sweep[::-1]
    for name in ('tofudb', 'sqlite'):
        entry = witness['engines'][name]
        assert [block['readers'] for block in entry['blocks']] == witness['block_order']
        for readers in sweep:
            pooled = entry['readers'][str(readers)]
            assert pooled['writes'] == 2 * SCALE['concurrent_writes']
            assert pooled['writer']['iterations'] == pooled['writes']
            assert (pooled['reads_completed'] > 0) == bool(readers)
            assert 0 <= pooled['reads_overlapping_writer'] <= pooled['reads_completed']
            assert (pooled['reads_overlapping_writer'] > 0) == bool(readers)
        assert all('reader_samples' not in block for block in entry['blocks'])
    observed = charter.horizontal_rule([witness, witness], bench._CHARTER_WITNESS_SPECS['horizontal_scaling'],
                                       DEFINITIONS['horizontal_scaling'])
    assert observed['status'] in ('PASS', 'FAIL', 'INSUFFICIENT')
    assert not [reason for reason in observed['reasons'] if 'required' in reason or 'overlap' in reason]


def test_horizontal_scaling_surfaces_a_failed_reader():
    events = []

    class FailingReads(FakeAuthority):
        def execute(self, operation, payload, command):
            if operation == 'conversation.list':
                raise RuntimeError('wedged reader')
            return super().execute(operation, payload, command)

    engines = {'tofudb': FailingReads('tofudb', events), 'sqlite': YieldingWrites('sqlite', events)}
    witness = witnesses(events, engines).run('horizontal_scaling')
    assert 'wedged reader' in witness['engines']['tofudb']['error']
    assert witness['engines']['sqlite']['readers']['4']['reads_overlapping_writer'] > 0
    observed = charter.horizontal_rule([witness, witness], bench._CHARTER_WITNESS_SPECS['horizontal_scaling'],
                                       DEFINITIONS['horizontal_scaling'])
    assert observed['status'] == 'FAIL'


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None


class FakeHost:
    """Records affinity and stressor decisions instead of changing the host."""

    def __init__(self):
        self.current = [0, 1, 2, 3, 4, 5, 6, 7]
        self.calls = []
        self.started = []
        self.ticks = 0

    def allowed_cpus(self):
        return [0, 1, 2, 3, 4, 5, 6, 7]

    def last_level_caches(self, cpus):
        return {'0-7': 1 << 20}

    def siblings(self, cpus):
        return [cpu + 64 for cpu in cpus]

    def affinities(self, pids):
        return {pid: list(self.current) for pid in pids}

    def confine(self, pids, cpus):
        self.calls.append(('confine', list(cpus)))
        self.current = list(cpus)

    def restore(self, affinities, default):
        self.calls.append(('restore', list(default)))
        self.current = list(default)

    def start_stressors(self, cpus, total_bytes):
        processes = [FakeProcess(1000 + len(self.started) * 10 + index) for index, _ in enumerate(cpus)]
        self.started.append((list(cpus), total_bytes, processes))
        return processes

    def stop_stressors(self, processes):
        for process in processes:
            process.stopped = True

    def process_cpu_seconds(self, pid):
        self.ticks += 1
        return float(self.ticks)

    def cpu_times(self, cpus):
        self.ticks += 1
        return {cpu: (self.ticks * 50, self.ticks * 100) for cpu in cpus}


def test_distracted_cohorts_confine_benchmark_and_pressure_to_one_small_cpu_set():
    events, host = [], FakeHost()
    spec = bench._CHARTER_WITNESS_SPECS['distracted_cohorts']
    witness = witnesses(events, host=host).run('distracted_cohorts')
    cpus = [6, 7][-spec['cpus']:]
    assert witness['cpus'] == cpus and witness['smt_siblings'] == [cpu + 64 for cpu in cpus]
    assert witness['memory_pressure']['bytes'] == 1 << 20
    assert host.calls[0] == ('confine', cpus) and host.calls[-1] == ('restore', host.allowed_cpus())
    assert len(host.started) == 2 * 2  # two pressure blocks per engine
    assert all(started == cpus and total == 1 << 20 and all(process.stopped for process in processes)
               for started, total, processes in host.started)
    per_block = -(-bench._MINIMUM_TAIL_SAMPLES // 2)
    for name in ('tofudb', 'sqlite'):
        entry = witness['engines'][name]
        assert [block['condition'] for block in entry['blocks']] == ['idle', 'pressure', 'pressure', 'idle']
        for block in entry['blocks']:
            assert block['benchmark_confined'] is True
            assert all(stats['iterations'] == per_block for stats in block['workloads'].values())
            assert bool(block['stressors']) == (block['condition'] == 'pressure')
            assert all(row['affinity'] == cpus and row['alive_at_end'] and row['cpu_seconds'] > 0
                       for row in block['stressors'])
        for workload in spec['workloads']:
            for condition in ('idle', 'pressure'):
                cohort = entry['cohorts'][workload][condition]
                assert cohort['iterations'] == 2 * per_block >= bench._MINIMUM_TAIL_SAMPLES
                assert 'samples_ms' not in cohort
    observed = charter.distracted_rule([witness, witness], spec, DEFINITIONS['distracted_cohorts'])
    assert observed['reasons'] == ['budget requires owner decision']
    assert {row['engine'] for row in observed['observations']} == {'tofudb', 'sqlite'}


def test_distracted_cohorts_stop_stressors_and_restore_affinity_when_a_pressure_block_fails():
    events, host = [], FakeHost()
    idle_appends = 5 + -(-bench._MINIMUM_TAIL_SAMPLES // 2)  # warmup plus one idle block

    class Failing(FakeAuthority):
        appends = 0

        def execute(self, operation, payload, command):
            if operation == 'turn.append_settled':
                self.appends += 1
                if self.appends > idle_appends:
                    raise RuntimeError('engine stalled under pressure')
            return super().execute(operation, payload, command)

    witness = witnesses(events, {'tofudb': Failing('tofudb', events)}, host=host).run('distracted_cohorts')
    assert 'engine stalled under pressure' in witness['engines']['tofudb']['error']
    assert [block['condition'] for block in witness['engines']['sqlite']['blocks']] == [
        'idle', 'pressure', 'pressure', 'idle']
    assert host.started and all(process.stopped for _, _, processes in host.started for process in processes)
    assert host.calls[-1] == ('restore', host.allowed_cpus())
    spec = bench._CHARTER_WITNESS_SPECS['distracted_cohorts']
    assert charter.distracted_rule([witness, witness], spec, DEFINITIONS['distracted_cohorts'])['status'] == 'FAIL'


def test_witness_authority_description_joins_the_receipt_artifact_and_volume(tmp_path):
    from scripts.tofudb_bench import _mount_type

    binary = tmp_path / 'tofu-db'
    binary.write_bytes(b'synthetic-executable-never-run')
    (tmp_path / 'libtofu_db.so').write_bytes(b'synthetic-library-never-loaded')
    engine = bench.TofuDbEngine(binary, client_mode='embedded')
    engine._data_dir = tmp_path
    row = bench._witness_authority(engine)
    sidecar = bench.LegacyEngine('sqlite')
    sidecar._project_root = tmp_path
    report = {'measurement': {'native_binary_sha256': row['artifact']['binary_sha256'], 'provenance': {
        'tofudb': {'artifact_before': {'shared_library_sha256': row['artifact']['shared_library_sha256']}}}},
              'resources': {name: {'filesystem': _mount_type(tmp_path), 'volume_device': tmp_path.stat().st_dev}
                            for name in ('tofudb', 'sqlite')}}
    witness = {'engines': {'tofudb': {'authorities': [row]},
                           'sqlite': {'authorities': [bench._witness_authority(sidecar)]}}}
    assert charter.witness_join_reasons(report, witness, 'sqlite') == []
    engine._client_mode = 'wire'
    assert bench._witness_authority(engine)['transport'] == 'daemon'
    assert bench._witness_process_ids(engine) == []


def test_native_second_authority_attempt_classifies_only_the_lease_refusal(monkeypatch, tmp_path):
    outputs = {
        'refused': (1, json.dumps({'type': 'storage.error', 'code': 'database_unavailable', 'retryable': True,
                                   'diagnostic': 'another tofu-db process owns the data directory: busy'})),
        'admitted': (0, json.dumps({'port': 4242, 'schemaId': 1})),
        'error': (1, json.dumps({'type': 'storage.error', 'code': 'database_integrity', 'retryable': False,
                                 'diagnostic': 'corrupt CONTROL'})),
    }
    launched = []

    class Contender:
        def __init__(self, argv, **kwargs):
            launched.append(argv)
            self.returncode, self.stdout = outputs[expected]

        def communicate(self, timeout=None):
            return self.stdout + '\n', ''

    monkeypatch.setattr(bench.subprocess, 'Popen', Contender)
    for expected in outputs:
        engine = bench.TofuDbEngine(tmp_path / 'tofu-db', authority_dir_parent=tmp_path)
        engine._data_dir = tmp_path / 'authority'
        assert engine.second_authority_attempt()['outcome'] == expected
    assert all(argv[1:4] == ['serve', '--data-dir', str(tmp_path / 'authority')] for argv in launched)
    assert len({argv[argv.index('--search-projection-dir') + 1] for argv in launched}) == len(launched)


@pytest.mark.parametrize('start,expected', [
    ('deferred', 'refused'), ('broken', 'error'), ('started', 'admitted'),
])
def test_sidecar_second_authority_attempt_uses_the_production_lease_probe(monkeypatch, tmp_path, start, expected):
    import lib.storage
    from lib.storage.errors import StorageStartDeferred

    stopped = []

    class Supervisor:
        def __init__(self, **kwargs):
            assert kwargs['project_root'] == tmp_path and kwargs['backend'] == 'sqlite'
            assert os.environ.get('TOFU_STORAGE_FASTPATH') is None

        def start(self):
            assert os.environ['TOFU_STORAGE_FASTPATH'] == 'off'
            if start == 'deferred':
                raise StorageStartDeferred('the project lease is held by Storage operation pid=1')
            if start == 'broken':
                raise RuntimeError('import failure')
            return object()

        def stop(self):
            stopped.append(True)

    monkeypatch.delenv('TOFU_STORAGE_FASTPATH', raising=False)
    monkeypatch.setattr(lib.storage, 'StorageSupervisor', Supervisor)
    engine = bench.LegacyEngine('sqlite')
    engine._runtime, engine._project_root = SimpleNamespace(), tmp_path
    assert engine.second_authority_attempt()['outcome'] == expected
    assert bool(stopped) == (expected == 'admitted')
    assert os.environ.get('TOFU_STORAGE_FASTPATH') is None


def test_sidecar_sigkill_reopen_reuses_the_crash_relaunch(monkeypatch):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    relaunched = []
    engine = bench.LegacyEngine('sqlite')
    engine._runtime, engine._child_pid = SimpleNamespace(), child.pid
    monkeypatch.setattr(engine, '_relaunch_after_kill', relaunched.append)
    try:
        assert engine.sigkill_authority() == child.pid
        assert child.wait(timeout=30) == -bench.signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert engine.reopen_after_sigkill() >= 0 and relaunched == [child.pid]


def test_cli_rejects_unknown_witnesses_and_needs_both_compared_authorities(monkeypatch):
    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', '--charter-witnesses', 'history_sweep,bogus'])
    with pytest.raises(SystemExit):
        bench.main()
    result = bench._charter_witnesses(['history_sweep'], {'sqlite': object()}, None, SCALE)
    assert 'tofudb and sqlite' in result['history_sweep']['error']


@pytest.mark.parametrize('failure', [
    {'error': 'BenchError: boom'},
    {'engines': {'tofudb': {'authorities': []}, 'sqlite': {'error': 'BenchError: sidecar stalled'}}},
])
def test_failed_witness_is_retained_and_fails_the_run(monkeypatch, tmp_path, failure):
    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    output = tmp_path / 'report.json'
    monkeypatch.setattr(bench, 'LegacyEngine', Engine)
    monkeypatch.setattr(bench, '_workloads', lambda *args: [('example', None, None, 1)])
    monkeypatch.setattr(bench, 'measure_workload', lambda *args: {'mean_ms': 1.0})
    monkeypatch.setattr(bench, '_print_report', lambda report: None)
    monkeypatch.setattr(bench, '_engine_resource_report', lambda *args: {})
    monkeypatch.setattr(bench, '_charter_witnesses', lambda names, *args: {
        'format': 'tofu.storage-charter-witnesses/v1', **{name: failure for name in names}})
    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', '--engines', 'sqlite', '--output', str(output),
                                            '--charter-witnesses', 'all'])
    assert bench.main() == 1
    report = json.loads(Path(output).read_text())
    assert set(report['charter_witnesses']) == {'format', *bench._CHARTER_WITNESS_SPECS}
