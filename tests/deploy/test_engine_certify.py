"""scripts/engine_certify.py: one command runs the engine gate and publishes a write-once record.

The gate's checks run real engines, Cargo and a 4,096-segment fixture; these
specifications cover what decides a certification without them: the pass
criteria GATE-PUBLISH-CARRY confirmed (clean stop, drill legs, flake rerun),
hard-link snapshots that never touch the fixture, publication the restart
preflight's reader accepts exactly once, and refusals before any work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time

import pytest

from scripts.engine_certification import gate
from serverctl_pkg import engine_certification as certification
from serverctl_pkg import native_engine as engines
from tests.server.test_engine_certification_gate import make_engine_tree
from tests.server.test_native_engine_update import binary

pytestmark = pytest.mark.unit

# Junit cases the gate fixtures name under tmp_path, not repository files.
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/server/test_a.py', 'tests/server/test_b.py', 'tests/storage/test_c.py'}

SHUTDOWN_DONE = ('{"level":"info","message":"step=authority_checkpoint state=done elapsed_ms=809",'
                 '"source":"tofu-db","target":"shutdown_timing"}')
KILLED_STOP = 'Storage stop step=child_exit state=timeout pid=114930 elapsed_s=10.000; killing child'


@pytest.mark.parametrize('log, wal, seed, ok', [
    (f'seed ready\n{SHUTDOWN_DONE}\nloadtest done', {'active-0000000000000015.wal': 0}, True, True),
    (f'seed ready\n{KILLED_STOP}', {'active-0000000000000006.wal': 608668661}, True, False),
    (f'{SHUTDOWN_DONE}\n{KILLED_STOP}', {'active-1.wal': 0}, True, False),
    ('step=authority_checkpoint state=done (not the engine shutdown record)', {'active-1.wal': 0}, True, False),
    (SHUTDOWN_DONE, {'active-1.wal': 4096}, True, False),
    (SHUTDOWN_DONE, {'active-1.wal': 0}, False, False),
])
def test_the_drill_source_must_be_a_demonstrably_clean_stop(log, wal, seed, ok):
    assert gate.clean_stop_verdict(log, wal, seed)[0] is ok


def _drill(**changes):
    probes = {
        'crash': {'rc': 0, 'built_before_kill': False, 'commits': [
            {'label': 'before-first-table'}, {'label': 'during-build-1'}, {'label': 'during-build-2'}]},
        'killstop-candidate': {'rc': 0, 'commits': [{}] * 30,
                               'last_step': 'step=authority_checkpoint state=start'},
        'killstop-deployed': {'rc': 0, 'commits': [{}] * 30,
                              'last_step': 'step=authority_checkpoint state=start'},
    }
    rows = {'clean': ([], 0), 'crash': (['2099-12-01', '2099-12-02', '2099-12-03'], 0),
            'killstop-candidate': ([], 30), 'killstop-deployed': ([], 30)}
    legs = {(source, engine): {
        'serve_rc': 0,
        'serve': {'exit': 0, 'refusal_count': 0, 'write': {'ok': True}, 'sha256': 'f' * 64,
                  'december_2099': december, 'november_2099_rows': november},
        'verify': {'rc': 0, 'verdict': 'ok', 'missing_blocks': 0}}
        for source, (december, november) in rows.items() for engine in ('candidate', 'deployed')}
    for path, value in changes.items():
        target, *keys = path.split('.')
        node = probes[target] if target in probes else legs[tuple(target.split('/'))]
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return probes, legs


@pytest.mark.parametrize('change', [
    {}, {'crash.built_before_kill': True}, {'crash.rc': 1},
    {'killstop-deployed.last_step': 'step=wal_rotate state=start'},
    {'killstop-candidate.kill_step_never_reached': True},
    {'crash/deployed.serve.sha256': 'e' * 64}, {'clean/candidate.serve.refusal_count': 1},
    {'clean/candidate.serve.write': None}, {'crash/deployed.serve.december_2099': ['2099-12-01']},
    {'killstop-deployed/candidate.serve.november_2099_rows': 29},
    {'killstop-candidate/deployed.verify.missing_blocks': 1}, {'clean/deployed.verify.verdict': 'corrupt'},
    {'clean/deployed.serve_rc': 1},
])
def test_every_drill_leg_shares_one_digest_and_every_acknowledged_write(change):
    ok, summary = gate.drill_verdict(*_drill(**change))
    assert ok is (change == {}), summary


def test_offline_verify_output_is_read_for_its_verdict_and_missing_blocks():
    line = ('format=tofu-db.verify.v1 verdict=ok control_generation=297 payload_segments_ok=4095/4095 '
            'missing_blocks=0 performance=[{"phase":"control"}]')
    assert gate.parse_verify(f'# 2026-11-12 ["tofu-db","verify"]\n{line}') == {'verdict': 'ok', 'missing_blocks': 0}
    assert gate.parse_verify('verify failed before a verdict') == {'verdict': None, 'missing_blocks': None}


def test_only_the_files_owning_failures_or_errors_are_rerun(tmp_path):
    for name in ('tests/server/test_a.py', 'tests/server/test_b.py', 'tests/storage/test_c.py'):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text('')
    junit = tmp_path / 'report.xml'
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.server.test_a" name="test_ok"/>'
        '<testcase classname="tests.server.test_b.TestFlow" name="test_x"><failure message="x"/></testcase>'
        '<testcase classname="tests.storage.test_c" name="test_y"><error message="sidecar startup '
        'exceeded its hard timeout"/></testcase>'
        '</testsuite></testsuites>')
    assert gate.failed_test_files(junit, tmp_path) == ['tests/server/test_b.py', 'tests/storage/test_c.py']


def test_snapshots_hard_link_immutable_files_and_never_touch_the_fixture(tmp_path):
    fixture = tmp_path / 'fixture'
    (fixture / 'payload-segments/00').mkdir(parents=True)
    (fixture / 'blocks').mkdir()
    (fixture / 'payload-segments/00/segment-1').write_bytes(b'segment')
    (fixture / 'blocks/block-1').write_bytes(b'block')
    for name in ('CONTROL', 'LOCK', 'active-0000000000000001.wal'):
        (fixture / name).write_bytes(name.encode())
    copy = tmp_path / 'copy'
    gate.snapshot_authority(fixture, copy)
    assert (copy / 'payload-segments/00/segment-1').stat().st_ino == (fixture / 'payload-segments/00/segment-1').stat().st_ino
    assert (copy / 'blocks/block-1').stat().st_ino == (fixture / 'blocks/block-1').stat().st_ino
    for name in ('CONTROL', 'LOCK', 'active-0000000000000001.wal'):
        assert (copy / name).stat().st_ino != (fixture / name).stat().st_ino
        (copy / name).write_bytes(b'rewritten by the engine')
        assert (fixture / name).read_bytes() == name.encode()


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A certification of a synthetic checkout whose checks are stood in."""
    repo = make_engine_tree(tmp_path / 'repo')
    fixture = tmp_path / 'fixture'
    (fixture / 'payload-segments').mkdir(parents=True)
    (fixture / 'CONTROL').write_bytes(b'control')
    certification_run = gate.Certification(repo, 'c' * 40, fixture, tmp_path / 'work', [], 'spec', ['--fixture', 'x'])
    certification_run.checkout = repo
    certification_run.work.mkdir(parents=True)
    certification_run.evidence.mkdir(parents=True)
    source = engines.source_fingerprint(repo)
    candidate = binary(tmp_path / 'build/tofu-db', source)

    def record(check_id, ok=True):
        certification_run._record(check_id, time.monotonic(), gate._utc(), ok)
        return ok

    def certify(check_id, built, output, _extra):
        output.mkdir()
        shutil.copy2(built, output / 'tofu-db')
        return record(check_id)
    monkeypatch.setattr(certification_run, 'build', lambda: record('build') and candidate)
    monkeypatch.setattr(certification_run, 'certify', certify)
    monkeypatch.setattr(certification_run, 'cargo', lambda: record('cargo-lib') and record('cargo-fastpath'))
    monkeypatch.setattr(certification_run, 'ff_native', lambda engine: record('ff-native'))
    monkeypatch.setattr(certification_run, 'load', lambda engine: record('load') and tmp_path / 'load')
    monkeypatch.setattr(certification_run, 'rollback', lambda *args: record('rollback'))
    certification_run.stand_in = record
    return certification_run, source


def test_a_passing_gate_publishes_exactly_one_record_the_preflight_accepts(run):
    certification_run, source = run
    contract = engines.storage_contract_sha256(certification_run.repo)
    published = certification_run.run(source)
    assert published == certification.record_directory(certification_run.repo, source, contract)
    record, problem = certification.passing_record(certification_run.repo, source, contract)
    assert record is not None, problem
    assert record.commit == 'c' * 40 and record.operator
    assert {check['id'] for check in json.loads(record.certification_file.read_text())['checks']} \
        == set(certification.REQUIRED_CHECKS)
    assert not list(certification.certifications_root(certification_run.repo).glob('.staging-*'))
    with pytest.raises(gate.CertificationError, match='never replaced'):
        gate.publish_record(certification_run.repo, certification_run.work / 'engine',
                            json.loads(record.certification_file.read_text()), 'again')


def test_a_failed_check_records_nothing_and_keeps_its_evidence(run, monkeypatch):
    certification_run, source = run
    monkeypatch.setattr(certification_run, 'load', lambda engine: certification_run.stand_in('load', False) or None)
    monkeypatch.setattr(certification_run, 'rollback', lambda *args: pytest.fail('no drill without a clean source'))
    assert certification_run.run(source) is None
    contract = engines.storage_contract_sha256(certification_run.repo)
    assert not certification.record_directory(certification_run.repo, source, contract).exists()
    result = json.loads((certification_run.evidence / 'result.json').read_text())
    assert result['verdict'] == 'fail'
    assert {check['id']: check['verdict'] for check in result['checks']}['load'] == 'fail'


def test_cargo_specs_keep_their_temporary_files_on_the_work_root_never_tmpfs(run, tmp_path, monkeypatch):
    # In a cgroup without swap tmpfs pages cannot be reclaimed: the gate must not fill /dev/shm on the
    # production host while it runs the engine's fsync-heavy library specifications.
    certification_run, _ = run
    tool = tmp_path / 'bin/cargo'
    tool.parent.mkdir()
    tool.write_text('#!/bin/sh\nexit 0\n')
    tool.chmod(0o755)
    certification_run.environment['PATH'] = str(tool.parent)
    seen = {}

    def run_check(check_id, argv, *, environment=None, **_kwargs):
        temporary = (environment or {}).get('TMPDIR')
        seen[check_id] = temporary
        assert temporary is None or Path(temporary).is_dir()
        log = certification_run.evidence / f'{check_id}.log'
        log.write_text('ok')
        return 0, log
    monkeypatch.setattr(certification_run, '_run', run_check)
    assert gate.Certification.cargo(certification_run)
    assert set(seen) == {'cargo-lib', 'cargo-fastpath'}
    assert Path(seen['cargo-lib']).is_relative_to(certification_run.work)
    assert not Path(seen['cargo-lib']).is_relative_to('/dev/shm')
    assert not Path(seen['cargo-lib']).exists(), 'the private temporary directory is removed afterwards'
    assert seen['cargo-fastpath'] is None


def test_the_fixture_is_required_and_must_be_a_settled_authority(tmp_path, capsys):
    with pytest.raises(SystemExit):
        gate.main([])
    assert '--fixture' in capsys.readouterr().err
    assert gate.main(['--fixture', str(tmp_path)]) == 2
    assert 'not a settled TofuDB authority' in capsys.readouterr().err


@pytest.mark.skipif(not Path('/dev/shm').is_dir(), reason='needs a second filesystem')
def test_a_work_root_off_the_fixtures_filesystem_is_refused_before_any_copy(tmp_path, capsys):
    fixture = tmp_path / 'fixture'
    (fixture / 'payload-segments').mkdir(parents=True)
    (fixture / 'CONTROL').write_bytes(b'control')
    work_root = Path('/dev/shm') / f'engine-certify-spec-{os.getpid()}'
    if work_root.stat().st_dev == fixture.stat().st_dev if work_root.exists() else (
            Path('/dev/shm').stat().st_dev == fixture.stat().st_dev):
        pytest.skip('/dev/shm shares the test filesystem')
    try:
        assert gate.main(['--fixture', str(fixture), '--work-root', str(work_root)]) == 2
        assert 'different filesystems' in capsys.readouterr().err
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
