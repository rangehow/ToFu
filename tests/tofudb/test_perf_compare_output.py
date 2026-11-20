"""Fresh report publication through the CLI with toy engines and metadata only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

import scripts.tofudb_perf_compare as bench

pytestmark = pytest.mark.unit


@pytest.fixture
def output_cli(monkeypatch, tmp_path):
    events = []

    class Engine:
        def __init__(self, *args, **kwargs):
            events.append('construct')

        def start(self):
            events.append('start')

        def stop(self):
            events.append('stop')

    def forbidden(*args, **kwargs):
        pytest.fail('output fixture must not launch processes or measure workloads')

    for name in ('LegacyEngine', 'RawSqliteEngine', 'TofuDbEngine'):
        monkeypatch.setattr(bench, name, Engine)
    monkeypatch.setattr(bench.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(bench, '_resolve_tofudb_binary', forbidden)
    monkeypatch.setattr(bench, 'measure_workload', forbidden)
    monkeypatch.setattr(bench, '_workloads', lambda *args: [])
    monkeypatch.setattr(bench, '_measurement_metadata', lambda *args: {
        'scope': 'toy-output-only', 'writer': threading.current_thread().name})
    monkeypatch.setattr(bench, '_engine_resource_report', lambda *args: {})
    monkeypatch.setattr(bench, '_print_report', lambda report: None)
    monkeypatch.setattr(bench, '_cid', lambda: 'toy-collection')
    monkeypatch.setattr(bench.os, 'sched_getaffinity', lambda pid: {0})
    monkeypatch.setattr('platform.node', lambda: 'toy-host')
    monkeypatch.setattr('platform.platform', lambda: 'toy-platform')
    monkeypatch.setattr(bench, '_reset_postgres_database',
                        lambda dsn: events.append('reset'))
    monkeypatch.delenv('TOFUDB_PERF_RETRY_BACKPRESSURE', raising=False)
    output = tmp_path / 'report.json'
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'sqlite', '--output', str(output)])
    return SimpleNamespace(output=output, events=events)


def test_fresh_report_retains_format_and_no_output_remains_optional(output_cli, monkeypatch, capsys):
    output, events = output_cli.output, output_cli.events
    assert bench.main() == 0
    report = json.loads(output.read_text())
    assert report['schema'] == 'tofu.perf_compare.v2'
    assert report['engines'] == ['sqlite']
    assert report['measurement']['scope'] == 'toy-output-only'
    assert report['workloads'] == {} and report['skipped'] == {}
    assert output.read_bytes() == json.dumps(report, indent=2, sort_keys=True).encode('utf-8')
    assert events == ['construct', 'start', 'stop']
    assert set(output.parent.iterdir()) == {output}
    assert f'report written to {output}' in capsys.readouterr().out

    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', '--engines', 'sqlite'])
    accepted = output.read_bytes()
    assert bench.main() == 0
    assert output.read_bytes() == accepted
    assert 'report written' not in capsys.readouterr().out


@pytest.mark.parametrize('basename', [
    pytest.param('r' * 215 + '.json', id='ascii-220-bytes'),
    pytest.param('é' * 107 + 'r.json', id='utf8-220-bytes'),
])
def test_valid_long_output_name_publishes_complete_report(output_cli, monkeypatch, capsys, basename):
    output = output_cli.output.with_name(basename)
    assert len(os.fsencode(output.name)) == 220
    output.write_bytes(b'toy-valid-target')
    assert output.read_bytes() == b'toy-valid-target'
    output.unlink()
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'sqlite', '--output', str(output)])

    assert bench.main() == 0
    report = json.loads(output.read_text())
    assert report['schema'] == 'tofu.perf_compare.v2'
    assert report['measurement']['scope'] == 'toy-output-only'
    assert output.read_bytes() == json.dumps(report, indent=2, sort_keys=True).encode('utf-8')
    assert output_cli.events == ['construct', 'start', 'stop']
    assert set(output.parent.iterdir()) == {output}
    assert f'report written to {output}' in capsys.readouterr().out


def test_second_invocation_preserves_accepted_report_even_if_content_would_match(output_cli, capsys):
    assert bench.main() == 0
    accepted = output_cli.output.read_bytes()
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 2
    assert output_cli.output.read_bytes() == accepted
    assert output_cli.events == ['construct', 'start', 'stop']
    assert 'report written' not in capsys.readouterr().out


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink', 'dangling-symlink'])
def test_occupied_report_refuses_before_engine_or_reset(output_cli, monkeypatch, capsys, kind):
    output = output_cli.output
    accepted = b'{"schema":"tofu.perf_compare.v2","accepted":"toy"}\n'
    target = output.parent / 'accepted.json'
    if kind == 'file':
        output.write_bytes(accepted)
    elif kind == 'directory':
        output.mkdir()
        (output / 'accepted.json').write_bytes(accepted)
    else:
        if kind == 'symlink':
            target.write_bytes(accepted)
        output.symlink_to(target.name)
    before = {path.name for path in output.parent.iterdir()}
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'postgres', '--postgres-dsn', 'toy-disposable',
        '--postgres-reset', '--retry-backpressure', '--output', str(output)])
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 2
    assert output_cli.events == []
    assert 'TOFUDB_PERF_RETRY_BACKPRESSURE' not in os.environ
    if kind == 'file':
        assert output.read_bytes() == accepted
    elif kind == 'directory':
        assert (output / 'accepted.json').read_bytes() == accepted
    else:
        assert output.is_symlink() and output.readlink() == Path(target.name)
        if kind == 'symlink':
            assert target.read_bytes() == accepted
        else:
            assert not target.exists()
    assert {path.name for path in output.parent.iterdir()} == before
    captured = capsys.readouterr()
    assert '--output' in captured.err and 'exists' in captured.err
    assert 'report written' not in captured.out


def test_late_writer_cannot_replace_accepted_report(output_cli, monkeypatch, capsys):
    output = output_cli.output
    accepted = b'{"schema":"tofu.perf_compare.v2","accepted":"late-toy-writer"}\n'
    monkeypatch.setattr(bench, '_print_report', lambda report: output.write_bytes(accepted))
    with pytest.raises(FileExistsError):
        bench.main()
    assert output.read_bytes() == accepted
    assert output_cli.events == ['construct', 'start', 'stop']
    assert set(output.parent.iterdir()) == {output}
    assert 'report written' not in capsys.readouterr().out


def test_two_cli_writers_publish_one_complete_report_without_clobber(output_cli, monkeypatch, capsys):
    rendezvous = threading.Barrier(2)
    monkeypatch.setattr(bench, '_print_report', lambda report: rendezvous.wait(timeout=30))

    def invoke():
        writer = threading.current_thread().name
        try:
            return writer, bench.main()
        except FileExistsError:
            return writer, 'occupied'

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert sorted(str(status) for _, status in outcomes) == ['0', 'occupied']
    report = json.loads(output_cli.output.read_text())
    winner = next(writer for writer, status in outcomes if status == 0)
    assert report['schema'] == 'tofu.perf_compare.v2'
    assert report['measurement']['writer'] == winner
    assert set(output_cli.output.parent.iterdir()) == {output_cli.output}
    assert output_cli.events.count('stop') == 2
    assert capsys.readouterr().out.count('report written to') == 1


@pytest.mark.parametrize('failure', ['serialization', 'write', 'file-sync', 'link', 'directory-sync'])
def test_failed_publication_never_reports_success_or_leaves_partial_report(
        output_cli, monkeypatch, capsys, failure):
    output = output_cli.output
    error = OSError('injected publication failure')

    def fail(*args, **kwargs):
        raise error

    if failure == 'serialization':
        monkeypatch.setattr(bench.json, 'dumps', fail)
    elif failure == 'write':
        original = Path.open

        class PartialWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, payload):
                self.stream.write(payload[:7])
                raise error

        def open_stream(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return PartialWriter(stream) if path.suffix == '.partial' else stream

        monkeypatch.setattr(Path, 'open', open_stream)
    elif failure == 'link':
        monkeypatch.setattr(bench.os, 'link', fail)
    else:
        original_sync = bench.os.fsync

        def sync(descriptor):
            directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
            if directory == (failure == 'directory-sync'):
                raise error
            return original_sync(descriptor)

        monkeypatch.setattr(bench.os, 'fsync', sync)

    with pytest.raises(OSError, match='injected publication failure'):
        bench.main()
    assert output_cli.events == ['construct', 'start', 'stop']
    if failure == 'directory-sync':
        report = json.loads(output.read_text())
        assert report['schema'] == 'tofu.perf_compare.v2'
        assert set(output.parent.iterdir()) == {output}
    else:
        assert not output.exists()
        assert list(output.parent.iterdir()) == []
    assert 'report written' not in capsys.readouterr().out


def test_fresh_report_keeps_ordinary_creation_permissions(output_cli):
    reference = output_cli.output.parent / 'ordinary-new-file'
    reference.write_text('toy')
    assert bench.main() == 0
    assert stat.S_IMODE(output_cli.output.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)


def test_missing_output_parent_is_not_created(output_cli, monkeypatch, capsys):
    output = output_cli.output.parent / 'missing-parent' / 'report.json'
    monkeypatch.setattr(bench.sys, 'argv', [
        'benchmark', '--engines', 'sqlite', '--output', str(output)])
    with pytest.raises(FileNotFoundError):
        bench.main()
    assert output_cli.events == ['construct', 'start', 'stop']
    assert not output.parent.exists()
    assert 'report written' not in capsys.readouterr().out
