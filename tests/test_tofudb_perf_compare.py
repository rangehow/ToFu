"""Offline lifecycle contracts for the three-authority comparison harness."""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from scripts.tofudb_perf_compare import LegacyEngine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['none', 'startup', 'workload'])
def test_report_exit_status_preserves_incomplete_comparison(
        monkeypatch, tmp_path, failure):
    import json
    import scripts.tofudb_perf_compare as bench

    stopped = []

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            if failure == 'startup':
                raise bench.BenchError('startup failed')

        def stop(self):
            stopped.append(True)

    def measure(*args):
        if failure == 'workload':
            raise bench.BenchError('request failed')
        return {'mean_ms': 1.0}

    output = tmp_path / 'report.json'
    monkeypatch.setattr(bench, 'LegacyEngine', Engine)
    monkeypatch.setattr(bench, '_workloads', lambda *args: [
        ('example', None, None, 1)])
    monkeypatch.setattr(bench, 'measure_workload', measure)
    monkeypatch.setattr(bench, '_print_report', lambda report: None)
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'sqlite', '--output', str(output)])
    assert bench.main() == (0 if failure == 'none' else 1)
    report = json.loads(output.read_text())
    assert bool(report['skipped']) == (failure == 'startup')
    assert bool(stopped) == (failure != 'startup')
    if failure == 'workload':
        assert 'request failed' in report['workloads']['example']['sqlite']['error']


@pytest.fixture
def fake_storage(monkeypatch, tmp_path):
    import lib.storage
    import lib.storage.service
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))
    states = []

    def supervisor(**kwargs):
        state = SimpleNamespace(
            backend=kwargs['backend'], env=dict(os.environ), stopped=False,
            fail_start=False, fail_stop=False)
        states.append(state)
        return state

    class Runtime:
        def __init__(self, supervisor, **kwargs):
            self.state = supervisor

        def start(self):
            if self.state.fail_start:
                raise RuntimeError('startup failed')

        def client(self, **kwargs):
            return self.state

        def stop(self):
            self.state.stopped = True
            if self.state.fail_stop:
                raise RuntimeError('stop failed')

    def reject_global_runtime(*args, **kwargs):
        pytest.fail('benchmark must not replace the process storage runtime')

    monkeypatch.setattr(lib.storage, 'StorageSupervisor', supervisor)
    monkeypatch.setattr(lib.storage, 'StorageRuntime', Runtime)
    monkeypatch.setattr(lib.storage.service, 'install_runtime_for_test',
                        reject_global_runtime)
    return states


@pytest.mark.parametrize('order', [('postgres', 'sqlite'), ('sqlite', 'postgres')])
@pytest.mark.parametrize('inherited', [False, True])
def test_engines_isolate_environment_and_keep_own_clients(
        monkeypatch, fake_storage, order, inherited):
    keys = ('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',
            'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', 'TOFU_STORAGE_TOKEN',
            'TOFU_STORAGE_CONNECTION_FILE')
    for key in keys:
        if inherited:
            monkeypatch.setenv(key, 'caller-owned-value')
        else:
            monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)
    engines = [LegacyEngine(name, 'postgresql://unused.invalid/disposable')
               for name in order]
    try:
        assert dict(os.environ) == before
        for engine in engines:
            engine.start()
            assert dict(os.environ) == before
        for engine, state in zip(engines, fake_storage):
            assert engine._client is state
            assert not state.stopped
            assert 'TOFU_STORAGE_CONNECTION_FILE' not in state.env
            if state.backend == 'postgres':
                assert Path(state.env[keys[0]]).read_text() == (
                    'postgresql://unused.invalid/disposable')
            else:
                assert keys[0] not in state.env
    finally:
        for engine in reversed(engines):
            engine.stop()
    assert dict(os.environ) == before
    assert all(state.stopped for state in fake_storage)
    assert all(not engine._project_root.parent.exists() for engine in engines)
    assert all(not engine._dsn_file.exists() for engine in engines
               if engine._dsn_file is not None)


@pytest.mark.parametrize('failure', ['startup', 'stop', 'unlink'])
def test_environment_restored_even_when_startup_or_cleanup_fails(
        monkeypatch, fake_storage, failure):
    import lib.storage

    original = lib.storage.StorageSupervisor

    def supervisor(**kwargs):
        state = original(**kwargs)
        state.fail_start = failure == 'startup'
        state.fail_stop = True
        return state

    monkeypatch.setattr(lib.storage, 'StorageSupervisor', supervisor)
    monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', 'caller-dsn')
    before = dict(os.environ)
    engine = LegacyEngine('postgres', 'postgresql://unused.invalid/disposable')
    if failure == 'startup':
        with pytest.raises(RuntimeError, match='startup failed'):
            engine.start()
    else:
        engine.start()
        if failure == 'unlink':
            original_unlink = Path.unlink

            def fail_unlink(path, **kwargs):
                if path == engine._dsn_file:
                    raise OSError('unlink failed')
                return original_unlink(path, **kwargs)

            with monkeypatch.context() as patcher:
                patcher.setattr(Path, 'unlink', fail_unlink)
                with pytest.raises(OSError, match='unlink failed'):
                    engine.stop()
            engine.stop()
        else:
            engine.stop()
    assert dict(os.environ) == before
    assert fake_storage[0].stopped
    assert not engine._project_root.parent.exists()
    assert not engine._dsn_file.exists()


@pytest.mark.parametrize('selection', ['', ' , ', 'sqlite,sqlite'])
def test_empty_or_duplicate_engine_selection_is_rejected(monkeypatch, selection):
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', '--engines', selection])
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 2


def test_explicit_missing_binary_never_falls_back(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setenv('TOFUDB_PERF_BIN', str(tmp_path / 'missing'))
    with pytest.raises(bench.BenchError, match='executable file'):
        bench._resolve_tofudb_binary()


def test_latency_windows_preserve_growth_and_all_samples(monkeypatch):
    import scripts.tofudb_perf_compare as bench

    durations = list(range(1, 24))
    ticks = iter(t for duration in durations for t in (0, duration * 1_000_000))
    monkeypatch.setattr(bench.time, 'perf_counter_ns', lambda: next(ticks))
    calls = []
    engine = SimpleNamespace(execute=lambda *args: calls.append(args))
    result = bench.measure_workload(
        engine, None, lambda i: ('probe', {'i': i}, False), 23, warmup=2)
    assert len(calls) == 25
    assert result['mean_ms'] == 12.0
    assert result['p99_ms'] == 23.0
    windows = result['windows']
    assert len(windows) <= 10
    assert sum(window['iterations'] for window in windows) == 23
    assert windows[0]['start_iteration'] == 2
    assert windows[0]['mean_ms'] == 2.0
    assert windows[-1]['start_iteration'] == 23
    assert windows[-1]['mean_ms'] == 22.5


@pytest.mark.parametrize('iterations,warmup', [(0, 5), (-1, 5), (1, -1)])
def test_invalid_measurement_bounds_fail_before_engine_io(iterations, warmup):
    import scripts.tofudb_perf_compare as bench

    with pytest.raises(bench.BenchError, match='iterations'):
        bench.measure_workload(None, None, None, iterations, warmup)


@pytest.mark.parametrize('payload,closed,expected', [
    (b'{"port":123}\n', False, None),
    (b'{"port":', True, 'complete readiness'),
    (b'{"port":', False, 'timed out'),
    (b'x' * 4096, False, 'exceeds 4096'),
])
def test_readiness_is_bounded_even_for_partial_line(payload, closed, expected):
    import scripts.tofudb_perf_compare as bench

    reader, writer = os.pipe()
    try:
        os.write(writer, payload)
        if closed:
            os.close(writer)
            writer = None
        if expected:
            with pytest.raises(bench.BenchError, match=expected):
                bench._read_readiness(reader, timeout=0.02)
        else:
            assert bench._read_readiness(reader, timeout=0.02) == '{"port":123}'
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def test_tofudb_init_failure_cleans_disposable_directories(monkeypatch, tmp_path):
    import scripts.tofudb_perf_compare as bench

    monkeypatch.setattr(bench.tempfile, 'tempdir', str(tmp_path))

    def fail_init(*args, **kwargs):
        raise RuntimeError('init failed')

    monkeypatch.setattr(bench.subprocess, 'run', fail_init)
    engine = bench.TofuDbEngine(Path('/unused-binary'))
    with pytest.raises(RuntimeError, match='init failed'):
        engine.start()
    assert engine._dirs
    assert all(not path.exists() for path in engine._dirs)


def test_measurement_metadata_identifies_exact_binary(tmp_path):
    import hashlib
    import scripts.tofudb_perf_compare as bench

    binary = tmp_path / 'tofu-db'
    binary.write_bytes(b'benchmark-build')
    metadata = bench._measurement_metadata({
        'tofudb': SimpleNamespace(_binary=binary), 'sqlite': object()})
    assert metadata['engine_order'] == ['tofudb', 'sqlite']
    assert metadata['scope'] == 'sequential-client-stack'
    assert metadata['tofudb_binary'] == {
        'path': str(binary.resolve()), 'bytes': 15,
        'sha256': hashlib.sha256(b'benchmark-build').hexdigest()}
