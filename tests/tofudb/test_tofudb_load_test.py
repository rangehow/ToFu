"""Safety and accounting contract of the production-shaped TofuDB load test.

The harness must never read or write production state: its measuring child
gets a scrubbed environment rooted in the private working directory, and a
root inside a live Tofu data directory is refused. Latency percentiles are
exact over recorded samples, and engine accounting is an interval delta.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'tofudb_load_test.py'


@pytest.fixture(scope='module')
def harness():
    spec = importlib.util.spec_from_file_location('tofudb_load_test', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_child_environment_inherits_no_production_settings(harness, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/live/tofu')
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '/live/tofu/data/native-storage-connection.json')
    monkeypatch.setenv('TOFU_DATA_DIR', '/live/tofu/data')
    monkeypatch.setenv('PYTHONPATH', '/live/tofu')
    engine = tmp_path / 'engine' / 'tofu-db'
    env = harness._child_environment(tmp_path, engine, 'a' * 64, 'normal')

    assert 'TOFU_STORAGE_CONNECTION_FILE' not in env
    for key in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'TOFU_DATA_DIR', 'TOFU_PROJECT_PATH'):
        assert Path(env[key]).is_relative_to(tmp_path), key
    assert env['PYTHONPATH'] == str(harness.PROJECT_ROOT)
    assert env['TOFUDB_BIN'] == str(engine)
    assert env['TOFU_DB_DURABILITY'] == 'normal'
    assert 'TOFU_DB_DURABILITY' not in harness._child_environment(tmp_path, engine, 'a' * 64, 'auto')
    assert 'TOFU_DB_REQUEST_PROFILE_EVERY' not in env
    profiled = harness._child_environment(tmp_path, engine, 'a' * 64, 'normal', request_profile=True)
    assert profiled['TOFU_DB_REQUEST_PROFILE_EVERY'] == '1'


@pytest.mark.parametrize('backend', ['tofudb', 'sqlite'])
def test_child_environment_keeps_the_medium_client_for_both_backends(harness, tmp_path, monkeypatch, backend):
    """Production reaches DolphinFS through a preloaded userspace client; a
    child without it measures kernel FUSE, a different medium."""
    monkeypatch.setenv('LD_PRELOAD', '/opt/client/libpreload.so')
    monkeypatch.setenv('LIBBGFS_CONFIG_FILE', '/opt/client/preload.conf')
    monkeypatch.setenv('FAULTFS_FSYNC_US', '770')
    monkeypatch.setenv('TOFU_UNRELATED_SETTING', '1')
    engine = tmp_path / 'engine' / 'tofu-db' if backend == 'tofudb' else None
    env = harness._child_environment(tmp_path, engine, 'a' * 64, 'strict', backend)
    assert env['LD_PRELOAD'] == '/opt/client/libpreload.so'
    assert env['LIBBGFS_CONFIG_FILE'] == '/opt/client/preload.conf'
    assert env['FAULTFS_FSYNC_US'] == '770'
    assert 'TOFU_UNRELATED_SETTING' not in env


def test_root_inside_a_live_checkout_is_refused(harness, tmp_path):
    checkout = tmp_path / 'tofu'
    (checkout / 'data').mkdir(parents=True)
    (checkout / 'data' / 'native-storage-connection.json').write_text('{}')
    with pytest.raises(SystemExit, match='inside a live Tofu checkout'):
        harness._refuse_live_root(checkout / 'data' / 'scratch')
    with pytest.raises(SystemExit, match='live Tofu data root'):
        harness._refuse_live_root(checkout / 'data')
    harness._refuse_live_root(tmp_path / 'private')


def test_root_on_a_memory_filesystem_is_refused(harness, tmp_path, monkeypatch):
    """The root persists by design (--reuse-seed), so on tmpfs it is unreclaimable RAM.

    2026-11-15: 31 load-test roots left on /dev/shm held 15 GiB of the 35 GiB
    container memory cgroup until they were removed by hand.
    """
    from serverctl_pkg import local_placement
    mountinfo = ('22 1 0:20 / / rw - xfs /dev/md0 rw\n'
                 f'40 22 0:41 / {tmp_path}/shm rw - tmpfs shm rw\n')
    real_describe = local_placement.describe_volume
    monkeypatch.setattr(local_placement, 'describe_volume',
                        lambda path, **kw: real_describe(path, mountinfo_text=mountinfo))
    with pytest.raises(SystemExit, match='memory filesystem'):
        harness._refuse_memory_root(tmp_path / 'shm' / 'load')
    harness._refuse_memory_root(tmp_path / 'disk' / 'load')


def test_final_storage_stop_lets_the_engine_finish_its_ordered_shutdown(harness, monkeypatch):
    """The run's authority is a rollback source, so it must end as a clean stop.

    The runtime's own 10 s default SIGKILLed engines whose ordered shutdown
    (final checkpoint) takes longer; the harness stops like the production
    native storage host does.
    """
    from serverctl_pkg.restart_budget import NATIVE_DAEMON_STOP_SECONDS
    import lib.storage.service as service
    stops, installed = [], []
    monkeypatch.setattr(service, 'install_runtime_for_test', installed.append)
    runner = harness.Harness(SimpleNamespace(random_seed=1), Path('/unused'))
    runner.runtime = SimpleNamespace(stop=lambda timeout=10.0: stops.append(timeout))
    runner.stop_storage()
    assert stops == [NATIVE_DAEMON_STOP_SECONDS]
    assert installed == [None]


def test_recorder_reports_exact_percentiles_and_error_codes(harness):
    recorder = harness.Recorder()
    for value in range(1, 101):
        recorder.samples['op'].append(value / 1000)

    class Refused(Exception):
        code = 'database_timeout'

    def refuse():
        raise Refused()

    with pytest.raises(Refused):
        recorder.timed('op', refuse)
    summary = recorder.summary(10.0)
    assert summary['op']['n'] == 100
    assert summary['op']['p50_ms'] == pytest.approx(51.0)
    assert summary['op']['p99_ms'] == pytest.approx(99.0)
    assert summary['op']['errors'] == {'database_timeout': 1}
    assert summary['op:error']['n'] == 1


def test_the_phase_stop_settlement_herd_is_reported_apart_from_turn_settlement(harness, tmp_path, monkeypatch):
    """Stopping a phase settles every running Turn at the same moment; that herd
    queues on the one client process and must not read as one Turn's settlement."""
    import lib.durable_worker as durable_worker
    import lib.turn_lifecycle as turn_lifecycle

    run = harness.Harness(SimpleNamespace(random_seed=7), tmp_path)
    run.conversations = {kind: [f'{kind}-1'] for kind in harness.PRODUCTION_PROFILE['active_kind_weights']}
    run.bookkeeping = lambda state, rng: None
    monkeypatch.setitem(harness.PRODUCTION_PROFILE, 'events_per_turn', (2, 3))
    monkeypatch.setitem(harness.PRODUCTION_PROFILE, 'events_per_attempt_per_s', 1e9)
    turns = []

    def create_turn_pair(conv_id, **kwargs):
        turns.append(conv_id)
        return {'attempt': {'attemptId': f'attempt-{len(turns)}'}}

    class Store:
        def claim_next(self, **kwargs):
            return {'payload': {'attemptId': f'attempt-{len(turns)}', 'conversationId': turns[-1],
                                'turnId': f'turn-{len(turns)}'}}

        def heartbeat(self, *args, **kwargs):
            pass

        def complete(self, *args, **kwargs):
            pass

    def record_task_event(task, wire, task_event=None):
        if len(turns) == 2 and wire['type'] != 'done':
            run.stop.set()  # the phase ends while the second Turn streams
        return 'carried'

    monkeypatch.setattr(durable_worker, 'StorageWorkerJobStore', Store)
    monkeypatch.setattr(durable_worker, 'WorkerJobClaim', SimpleNamespace(
        from_document=lambda document, expected_worker_id: SimpleNamespace(task_id='task', fencing_token=1)))
    monkeypatch.setattr(durable_worker, 'WorkerExecutionContext', lambda claim: None)
    monkeypatch.setattr(turn_lifecycle, 'create_turn_pair', create_turn_pair)
    monkeypatch.setattr(turn_lifecycle, 'dispatch_attempt_to_worker', lambda principal, attempt_id: None)
    monkeypatch.setattr(turn_lifecycle, 'mark_task_started', lambda *args, **kwargs: None)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record_task_event)

    run.agent(0, 1.0)
    summary = run.recorder.summary(1.0)
    assert len(turns) == 2
    assert summary['turn.event.record:terminal']['n'] == 1
    assert summary['turn.event.record:terminal:phase_stop']['n'] == 1
    assert run.counters['turns_completed'] == 2


def test_engine_delta_is_an_interval_and_charges_bytes_per_change(harness):
    def metrics(calls, nanos, wal, frames):
        return {'commitStages': [{'stage': 'authority_wait', 'calls': calls, 'totalNanos': nanos}],
                'durability': {'walEncodedBytes': wal, 'projectionCheckpointBytes': 0},
                'listener': {'requestFrames': frames, 'acceptedConnections': 1, 'peakActiveConnections': 3}}

    delta = harness._engine_delta(
        metrics(10, 10_000_000, 1_000, 5), metrics(30, 50_000_000, 21_000, 105),
        {'cpu_s': 1.0, 'write_bytes': 100, 'wchar': 100},
        {'cpu_s': 3.0, 'write_bytes': 40_100, 'wchar': 50_100}, 10.0, changes=20)
    assert delta['stages']['authority_wait'] == {'calls': 20, 'avg_ms': 2.0, 'thread_eq': 0.004}
    assert delta['durability']['walEncodedBytes'] == 20_000
    assert delta['listener']['requestFrames'] == 100
    assert delta['process']['write_bytes_per_change'] == 2_000
    assert delta['process']['wal_bytes_per_change'] == 1_000
    assert delta['cpu_cores'] == 0.2


def test_engine_delta_reports_catalog_publication_stages_per_call(harness):
    # Catalog publications (spill packing on the commit path) cost catalog_install,
    # catalog_publish, step_chunk and pack_filter; a phase reports each per call.
    publication_stages = ('catalog_install', 'catalog_publish', 'step_chunk', 'pack_filter')

    def metrics(calls, nanos):
        return {'commitStages': [{'stage': stage, 'calls': calls, 'totalNanos': nanos}
                                 for stage in publication_stages]}

    stages = harness._engine_delta(metrics(1, 1_000_000), metrics(5, 401_000_000), {}, {}, 10.0,
                                   changes=0)['stages']
    for stage in publication_stages:
        assert stages[stage] == {'calls': 4, 'avg_ms': 100.0, 'thread_eq': 0.04}


def test_engine_delta_reports_growth_reclamation_and_packing(harness):
    def metrics(reclaimed, compactions, retained, packed):
        return {'onlineGarbageCollector': {'total_reclaimed_blocks': reclaimed, 'epochs_completed': 1,
                                           'epoch_error': None},
                'maintenance': {'historyCompactions': compactions, 'retainedHistorySegments': retained},
                'backgroundPacker': {'packedSegments': packed}}

    growth = harness._engine_delta(metrics(5, 2, 900, 1), metrics(105, 12, 64, 4), {}, {}, 10.0,
                                   changes=0)['growth']
    assert growth['onlineGarbageCollector'] == {'total_reclaimed_blocks': 100, 'epochs_completed': 0,
                                                'epoch_error': None}
    assert growth['maintenance'] == {'historyCompactions': 10, 'retainedHistorySegments': 64}
    assert growth['backgroundPacker'] == {'packedSegments': 3}
    assert growth['historyPromotion'] == {}


def _fake_engine(tmp_path):
    """A stand-in `tofu-db` that records `init` and `backup` and writes what each would."""
    calls = tmp_path / 'engine-calls'
    engine = tmp_path / 'fake-tofu-db'
    engine.write_text(
        '#!/bin/sh\n'
        f'echo "$1" >> {calls}\n'
        'case "$1" in\n'
        '  init) mkdir -p "$3" ;;\n'
        '  backup) mkdir -p "$5/snapshots" && : > "$5/snapshots/snapshot-0-0.manifest" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n')
    engine.chmod(0o700)
    return engine, calls


def test_a_fixture_engine_opens_with_a_current_backup_generation(harness, tmp_path, monkeypatch):
    """Without one, its first fallback epoch is due at boot and copies the whole fixture
    into an empty store; production keeps a recent generation."""
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    engine, calls = _fake_engine(tmp_path)
    data = tmp_path / 'project' / 'data'

    assert harness._seed_backup_generation(data, engine) == 'seeded'
    assert calls.read_text().split() == ['init', 'backup']
    assert list((data / 'backups' / 'tofudb' / 'snapshots').glob('*.manifest'))
    assert not list(tmp_path.glob('tofudb-backup-seed-*')), 'the scratch authority is removed'

    assert harness._seed_backup_generation(data, engine) == 'kept'
    assert calls.read_text().split() == ['init', 'backup']


def test_a_store_with_a_closed_generation_is_kept(harness, tmp_path):
    engine, calls = _fake_engine(tmp_path)
    data = tmp_path / 'data'
    (data / 'backups' / 'tofudb' / 'closed' / 'snapshots').mkdir(parents=True)
    (data / 'backups' / 'tofudb' / 'closed' / 'snapshots' / 'snapshot-1-1.manifest').write_bytes(b'x')

    assert harness._seed_backup_generation(data, engine) == 'kept'
    assert not calls.exists()


def test_sidecar_leg_runs_the_same_isolated_mix_without_engine_settings(harness, tmp_path, monkeypatch):
    """`--backend sqlite` measures the SQL Sidecar on the identical workload: the
    child stays isolated, selects the sqlite store, and inherits no TofuDB engine
    pinning or engine tuning that would not apply to it."""
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '/live/tofu/data/native-storage-connection.json')
    env = harness._child_environment(tmp_path, None, '', 'normal', 'sqlite')
    native_deadline = lambda: harness._child_environment(
        tmp_path, tmp_path / 'tofu-db', 'a' * 64, 'normal')['TOFU_STORAGE_RPC_DEADLINE_S']

    assert env['TOFU_PERSONAL_STORAGE_BACKEND'] == 'sqlite'
    assert env['TOFU_STORAGE_FASTPATH'] == 'off'
    assert env['TOFU_STORAGE_RPC_DEADLINE_S'] == native_deadline() == '20'
    assert 'TOFU_STORAGE_CONNECTION_FILE' not in env
    for key in ('HOME', 'TMPDIR', 'TOFU_DATA_DIR', 'TOFU_PROJECT_PATH'):
        assert Path(env[key]).is_relative_to(tmp_path), key
    assert not [key for key in env if key.startswith(('TOFUDB_', 'TOFU_DB_'))]
    native = harness._child_environment(tmp_path, tmp_path / 'tofu-db', 'a' * 64, 'normal')
    assert native['TOFU_PERSONAL_STORAGE_BACKEND'] == 'tofudb'


def test_sidecar_leg_needs_no_engine_binary(harness, tmp_path, monkeypatch):
    launched = {}

    def run(command, env, cwd):
        launched.update(command=command, env=env)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness.subprocess, 'run', run)
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    assert harness.main(['--root', str(tmp_path / 'root'), '--backend', 'sqlite', '--engine', '']) == 0
    assert '--engine' not in launched['command']
    assert launched['command'][launched['command'].index('--backend') + 1] == 'sqlite'
    with pytest.raises(SystemExit, match='must name a tofu-db binary'):
        harness.main(['--root', str(tmp_path / 'root'), '--engine', ''])


def test_maintenance_prunes_through_the_application_retention_sweep(harness, monkeypatch):
    # Production pages event.prune at event_log's batch size and follows
    # has_more; one large prune per sweep held the writer for seconds on an
    # aged root, a stall production never produces.
    import time

    from lib.tasks_pkg import event_log
    monkeypatch.setattr(event_log, '_PRUNE_BATCH_INTERLEAVE_S', 0)
    run = harness.Harness(SimpleNamespace(random_seed=7), Path('/unused'))
    pages = iter([True, True, False, False])
    calls = []

    class Client:
        def command(self, op, payload, *args, **kwargs):
            calls.append((op, payload))
            more = next(pages)
            if payload['retention_class'] == 'structural':
                run.stop.set()
            return {'deleted': payload['limit'] if more else 3, 'has_more': more}

    monkeypatch.setattr(run, 'client', lambda write=False: Client())
    before_ms = int(time.time() * 1000)
    run.maintenance(100.0)
    after_ms = int(time.time() * 1000)

    assert [op for op, _ in calls] == ['event.prune'] * 4
    assert [payload['retention_class'] for _, payload in calls] == ['streaming'] * 3 + ['structural']
    assert all(payload['limit'] == event_log._PRUNE_BATCH_ROWS for _, payload in calls)
    stream_cutoff = calls[0][1]['created_before_ms']
    structural_cutoff = calls[-1][1]['created_before_ms']
    assert before_ms - event_log.EVENT_TTL_MS <= stream_cutoff <= after_ms - event_log.EVENT_TTL_MS
    assert before_ms - event_log.STRUCTURAL_TTL_MS <= structural_cutoff <= after_ms - event_log.STRUCTURAL_TTL_MS
    assert run.recorder.summary(1.0)['event.prune']['n'] == 4


def test_sync_viewers_advance_their_cursor_like_the_browser_stream(harness):
    # Changes pages carry ``head``/``hasMore``/``events[].syncSeq``; a cursor
    # that never advances replays the whole change history on every poll.
    events = [{'syncSeq': 7}, {'syncSeq': 9}]
    assert harness._next_sync_cursor({'head': 40, 'hasMore': True, 'events': events}, 3) == 9
    assert harness._next_sync_cursor({'head': 40, 'hasMore': False, 'events': events}, 3) == 40
    assert harness._next_sync_cursor({'head': 0, 'hasMore': False, 'events': []}, 12) == 12
    assert harness._next_sync_cursor({'head': 5, 'resetRequired': True}, 12) == 12
    assert harness._next_sync_cursor(None, 12) == 12


def test_synthetic_text_has_exactly_the_requested_size(harness):
    """Payload byte volume is the workload: a text that starts near the end of
    the sentence must still fill the requested size."""
    import random
    rng = random.Random(1)
    for size in (1, 64, len(harness._TEXT) - 1, len(harness._TEXT), 5_000):
        for _ in range(50):
            assert len(harness._text(rng, size)) == size
