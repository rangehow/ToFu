"""scripts/eval_runs_retention.py: the .eval-runs retention owner keeps live runs and
reclaims only the reconstructible tmp/cache of finished ones."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'eval_runs_retention.py'


def load():
    spec = importlib.util.spec_from_file_location('eval_runs_retention_script', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_run(root: Path, name: str) -> Path:
    run = root / name
    runtime = run / '.runtime'
    for leaf in ('tmp', 'cache', 'state', 'data', 'home', 'config'):
        (runtime / leaf).mkdir(parents=True)
    (runtime / 'tmp' / 'scratch.bin').write_bytes(b'x' * 4096)
    (runtime / 'cache' / 'uv').mkdir()
    (runtime / 'cache' / 'uv' / 'wheel.whl').write_bytes(b'y' * 8192)
    (runtime / 'state' / 'session.json').write_text('{}')
    (run / 'results.json').write_text('{"score": 1}')
    (run / 'trials').mkdir()
    (run / 'trials' / 'trial-1.log').write_text('evidence\n')
    return run


def rows(report):
    return {row['runtime']: row for row in report['units']}


def test_finished_scratch_is_reclaimed_and_durable_run_data_is_kept(tmp_path, monkeypatch):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'tb-20261102')
    session = make_run(root / 'harbor-env', 'harbor-20261117T095213Z')
    monkeypatch.setattr(script, 'scan_processes', lambda: [])

    first = script.run(root, apply=True, processes=[])
    assert {row['state'] for row in first['units']} == {'live'}
    assert all(row['reasons'] == ['unobserved'] for row in first['units'])
    assert (run / '.runtime' / 'tmp' / 'scratch.bin').exists()

    dry = script.run(root, apply=False, processes=[])
    assert dry['finished'] == {'units': 2, 'bytes': 2 * (4096 + 8192), 'files': 4}
    assert (run / '.runtime' / 'cache' / 'uv' / 'wheel.whl').exists()

    applied = script.run(root, apply=True, processes=[])
    assert rows(applied)['tb-20261102/.runtime']['removed']
    for owner in (run, session):
        assert not (owner / '.runtime' / 'tmp').exists()
        assert not (owner / '.runtime' / 'cache').exists()
        assert (owner / '.runtime' / 'state' / 'session.json').read_text() == '{}'
        assert (owner / '.runtime' / 'home').is_dir()
        assert (owner / 'results.json').read_text() == '{"score": 1}'
        assert (owner / 'trials' / 'trial-1.log').read_text() == 'evidence\n'


@pytest.mark.parametrize('process, reason', [
    (lambda run: dict(paths=[str(run / 'trials' / 'trial-1.log')]), 'open-handle'),
    (lambda run: dict(paths=[str(run)]), 'open-handle'),
    (lambda run: dict(text=f'TMPDIR={run}/.runtime/tmp\n'), 'process-reference'),
])
def test_a_run_a_process_uses_is_live_and_kept(tmp_path, monkeypatch, process, reason):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'eval-matrix-20261112')
    script.run(root, apply=False, processes=[])
    holder = script.Process(4242, **process(run))
    monkeypatch.setattr(script, 'scan_processes', lambda: [holder])

    report = script.run(root, apply=True, processes=[holder])
    row = rows(report)['eval-matrix-20261112/.runtime']
    assert row['state'] == 'live' and reason in row['reasons'] and row['removed'] == []
    assert (run / '.runtime' / 'cache' / 'uv' / 'wheel.whl').exists()


def test_recent_writes_keep_a_run_live(tmp_path):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'sandbox-migration-20261118')
    script.run(root, apply=False, processes=[])
    (run / '.runtime' / 'tmp' / 'new-trial.tmp').write_bytes(b'z')

    report = script.run(root, apply=True, processes=[])
    assert rows(report)['sandbox-migration-20261118/.runtime']['reasons'] == ['changed']
    assert (run / '.runtime' / 'tmp' / 'new-trial.tmp').exists()


def test_a_result_store_that_names_the_scratch_keeps_it(tmp_path):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'harbor-env-20261117')
    (root / 'takeover-matrix' ).mkdir()
    (root / 'takeover-matrix' / 'artifacts.json').write_text(json.dumps(
        {'image_cache': str(run / '.runtime' / 'cache')}))
    script.run(root, apply=False, processes=[])

    report = script.run(root, apply=True, processes=[])
    assert 'result-store-reference' in rows(report)['harbor-env-20261117/.runtime']['reasons']
    assert (run / '.runtime' / 'cache' / 'uv' / 'wheel.whl').exists()


def test_a_process_that_appears_before_removal_wins(tmp_path, monkeypatch):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'frontier-harness-20261112')
    script.run(root, apply=False, processes=[])
    late = script.Process(77, paths=[str(run / '.runtime' / 'cache' / 'uv')])
    monkeypatch.setattr(script, 'scan_processes', lambda: [late])

    report = script.run(root, apply=True, processes=[])
    row = rows(report)['frontier-harness-20261112/.runtime']
    assert row['state'] == 'live' and row['reasons'] == ['open-handle'] and row['removed'] == []
    assert (run / '.runtime' / 'tmp' / 'scratch.bin').exists()


def test_cli_dry_run_reports_without_removing(tmp_path, capsys):
    script = load()
    root = tmp_path / '.eval-runs'
    run = make_run(root, 'tb21-native-20261102')
    assert script.main(['--root', str(root)]) == 0
    assert script.main(['--root', str(root), '--json']) == 0
    report = json.loads(capsys.readouterr().out.split('\n', 2)[-1])
    assert report['apply'] is False
    assert (run / '.runtime' / 'tmp' / 'scratch.bin').exists()
