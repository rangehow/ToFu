"""Offline contracts for staging and publishing portability backend results."""
import hashlib
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.portability import report, run, scenario
from evaluations.portability import __main__ as cli
from evaluations.portability.__main__ import CollectionPublicationError, _cell_job
from evaluations.portability.journal import COLLECTION_STAGE_PREFIX, read_journal

pytestmark = pytest.mark.unit

DECLARATION = scenario.Scenario(id='synthetic', version='1', metric='score', tasks=({'id': 't'},),
                                baseline=None, tofu=None, score=None)


@pytest.fixture
def collection(monkeypatch, tmp_path):
    out = tmp_path / 'new' / 'nested' / 'collected'
    environment = {'host': 'fixture'}
    monkeypatch.setattr(scenario, 'load_scenario', lambda _name: DECLARATION)
    record = {'scenario': DECLARATION.id, **DECLARATION.provenance(), 'creator': 'offline',
              'environment': 'offline', 'actual_environment': environment,
              'environment_fingerprint': hashlib.sha256(
                  json.dumps(environment, sort_keys=True).encode()).hexdigest(),
              'model': 'fixture', 'arm': 'baseline', 'task': 't', 'score': 1.0,
              'attempt_id': 'attempt', 'artifact_path': 'artifacts/attempt.json'}
    content = json.dumps({'binding': run.artifact_binding(record), 'artifact': {'fixture': True}}).encode()
    record['artifact_sha256'] = hashlib.sha256(content).hexdigest()
    payloads = {record['artifact_path']: content}
    calls = []

    class Compute:
        def submit(self, spec):
            return SimpleNamespace(job_id='offline', to_dict=lambda: {'job_id': 'offline'})

        def effective_config(self, ref):
            return {'config': {}}

        def verify(self, *args):
            return []

        def status(self, ref):
            return SimpleNamespace(terminal=True, state='succeeded', exit_code=0)

        def logs(self, ref):
            return 'synthetic backend log'

        def artifacts(self, ref, names, destination):
            calls.append(tuple(names))
            for name in names:
                dest = Path(destination) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if name not in payloads:
                    raise FileNotFoundError(f'synthetic missing artifact: {name}')
                dest.write_bytes(payloads[name])
                assert not report.load([out]), 'reports must not see staging journals'
                assert not report.load([Path(destination)]), 'a staging path is not a report root'
            if names != ['results.jsonl'] and 'replacement' in payloads:
                (Path(destination) / 'results.jsonl').write_bytes(payloads['replacement'])

    monkeypatch.setitem(sys.modules, 'lib.compute.jobs', SimpleNamespace(
        JobSpec=lambda **kwargs: SimpleNamespace(validate=lambda: SimpleNamespace(**kwargs)),
        backend=lambda *_args, **_kwargs: Compute()))
    args = SimpleNamespace(backend_option=[], backend='offline', python='unused',
                           environment='offline', remote_repo=None, out=str(out))
    return out, record, payloads, calls, args


@pytest.mark.parametrize('entrypoint', ['standalone', 'cli'])
def test_backend_publishes_validated_references_before_journal(collection, monkeypatch, capsys, tmp_path, entrypoint):
    out, record, payloads, calls, args = collection
    existing = tmp_path
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()
    rename = Path.rename
    fsync = os.fsync
    synced = set()
    post_rename_synced = set()
    published = False

    def observe_sync(fd):
        fsync(fd)
        status = os.fstat(fd)
        (post_rename_synced if published else synced).add((status.st_dev, status.st_ino))

    def identity(path):
        status = path.stat()
        return status.st_dev, status.st_ino

    def directories(target):
        return (target, target.parent, out, out.parent, out.parent.parent, existing)

    def inspect_publication(staged, destination):
        nonlocal published
        if staged.name != 'results.jsonl':
            return rename(staged, destination)
        assert not destination.exists()
        assert not report.load([out])
        assert run.valid_artifact(destination, record)
        artifact = destination.parent / record['artifact_path']
        assert {identity(p) for p in (staged, artifact, artifact.parent, *directories(destination.parent))} <= synced
        renamed = rename(staged, destination)
        published = True
        return renamed

    monkeypatch.setattr(os, 'fsync', observe_sync)
    monkeypatch.setattr(Path, 'rename', inspect_publication)
    if entrypoint == 'cli':
        assert _run_cli(monkeypatch, out) == 0, capsys.readouterr().out
        (target,) = (out / 'cells').iterdir()
    else:
        target = _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert {identity(p) for p in directories(target)} <= post_rename_synced
    assert identity(existing.parent) not in post_rename_synced
    assert calls == [('results.jsonl',), ('artifacts/attempt.json',)]
    assert list(report.load([out])) == [record]
    assert (target / 'results.jsonl').read_bytes() == payloads['results.jsonl']
    assert set(p.name for p in target.iterdir()) == {'results.jsonl', 'artifacts', 'job.json', 'job.log'}


def test_backend_artifact_fetch_cannot_replace_validated_journal(collection):
    out, record, payloads, calls, args = collection
    # Distinct wire formatting also verifies that publication does not reserialize.
    original = ('  ' + json.dumps(record) + ' \n').encode()
    payloads['results.jsonl'] = original
    payloads['replacement'] = (json.dumps({**record, 'task': 'wrong-task'}) + '\n').encode()

    target = _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')

    assert calls == [('results.jsonl',), ('artifacts/attempt.json',)]
    assert (target / 'results.jsonl').read_bytes() == original
    assert list(report.load([out])) == [record]
    assert not list(target.glob(f'{COLLECTION_STAGE_PREFIX}*'))


def _run_cli(monkeypatch, out):
    monkeypatch.setattr(cli, '_creators', lambda _models: {'fixture': 'offline'})
    return cli.main(['run', '--scenario', 'synthetic', '--models', 'fixture', '--arms', 'baseline',
                     '--backend', 'offline', '--environment', 'offline', '--out', str(out)])


def test_missing_backend_dependency_stops_before_registry_lookup(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, 'lib.compute.jobs', None)
    monkeypatch.setattr(cli, '_creators', lambda *_: pytest.fail('must not call the registry'))
    out = tmp_path / 'not-created'
    with pytest.raises(SystemExit) as caught:
        cli.main(['run', '--scenario', 'synthetic', '--models', 'fixture',
                  '--backend', 'offline', '--out', str(out)])
    assert caught.value.code == 2
    assert '--backend requires the optional lib.compute.jobs package' in capsys.readouterr().err
    assert not out.exists()


def test_backend_publishes_unlinked_infrastructure_attempt(collection):
    out, record, payloads, calls, args = collection
    for field in ('attempt_id', 'artifact_path', 'artifact_sha256'):
        del record[field]
    record.update(score=None, error_kind='infrastructure', error='synthetic availability failure')
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()

    target = _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert calls == [('results.jsonl',)]
    assert list(report.load([out])) == [record]
    assert not (target / 'artifacts').exists()


@pytest.mark.parametrize(('failure', 'exception', 'message'), [
    ('missing', FileNotFoundError, 'synthetic missing artifact'),
    ('corrupt', ValueError, 'does not match its journal reference'),
    ('torn-journal', ValueError, 'incomplete_final_row'),
    ('malformed-journal', ValueError, 'malformed_row'),
    ('invalid-schema', ValueError, 'row 2: malformed_row'),
    ('partial-binding', ValueError, 'malformed_row'),
    ('missing-binding', ValueError, 'Missing artifact binding'),
])
def test_backend_collection_failure_leaves_diagnostics_not_journal(collection, failure, exception, message):
    out, record, payloads, calls, args = collection
    name = record['artifact_path']
    if failure == 'missing':
        del payloads[name]
    elif failure == 'corrupt':
        payloads[name] += b'damaged'
    elif failure == 'partial-binding':
        del record['artifact_path']
    elif failure == 'missing-binding':
        for field in ('attempt_id', 'artifact_path', 'artifact_sha256'):
            del record[field]
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()
    if failure == 'torn-journal':
        payloads['results.jsonl'] += b'{"score":'
    elif failure == 'malformed-journal':
        payloads['results.jsonl'] += b'{"score":\n' + payloads['results.jsonl']
    elif failure == 'invalid-schema':
        payloads['results.jsonl'] += (json.dumps({**record, 'creator': []}) + '\n').encode()

    with pytest.raises(CollectionPublicationError, match=f'journal was not published.*{message}') as caught:
        _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert isinstance(caught.value.__cause__, exception)

    expected = [('results.jsonl',)]
    if failure in ('missing', 'corrupt'):
        expected.append((name,))
    assert calls == expected
    assert not report.load([out])
    assert not list(out.rglob('results.jsonl'))
    (target,) = (out / 'cells').iterdir()
    assert set(p.name for p in target.iterdir()) == {'job.json', 'job.log'}
    assert (target / 'job.log').read_text() == 'synthetic backend log'


def _unlinked_failure(record, **updates):
    row = {k: v for k, v in record.items() if k not in ('attempt_id', 'artifact_path', 'artifact_sha256')}
    row.update(score=None, error_kind='infrastructure', error='synthetic availability failure')
    return {**row, **updates}


def test_backend_rejects_unlinked_outcome_missing_runtime_provenance(collection):
    out, record, payloads, calls, args = collection
    row = _unlinked_failure(record)
    del row['actual_environment'], row['environment_fingerprint']
    payloads['results.jsonl'] = (json.dumps(row) + '\n').encode()

    with pytest.raises(CollectionPublicationError, match='journal was not published.*comparison namespace'):
        _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert calls == [('results.jsonl',)]
    assert not list(out.rglob('results.jsonl'))


@pytest.mark.parametrize('fault', [None, 'empty', 'missing', 'duplicate', 'mixed-runtime'])
def test_backend_requires_exact_task_coverage(collection, monkeypatch, fault):
    out, record, payloads, calls, args = collection
    declaration = replace(DECLARATION, tasks=(*DECLARATION.tasks, {'id': 'second'}))
    # The requested scenario locator need not equal the identifier written on rows.
    monkeypatch.setattr(scenario, 'load_scenario', lambda _name: declaration)
    first = _unlinked_failure(record, **declaration.provenance())
    second = {**first, 'task': 'second'}
    rows = {'empty': [], 'missing': [first], 'duplicate': [first, second, first]}.get(fault, [second, first])
    if fault == 'mixed-runtime':
        measured = {'host': 'different-fixture'}
        second.update(actual_environment=measured, environment_fingerprint=hashlib.sha256(
            json.dumps(measured, sort_keys=True).encode()).hexdigest())
    payloads['results.jsonl'] = ''.join(json.dumps(row) + '\n' for row in rows).encode()

    if fault is None:
        target = _cell_job(args, 'fixture_module:DECLARATION', 'fixture', 'baseline', 'offline')
        assert list(report.load([out])) == rows
        assert (target / 'results.jsonl').read_bytes() == payloads['results.jsonl']
    else:
        with pytest.raises(CollectionPublicationError, match='journal was not published'):
            _cell_job(args, 'fixture_module:DECLARATION', 'fixture', 'baseline', 'offline')
        assert not report.load([out])
        assert not list(out.rglob('results.jsonl'))
    assert calls == [('results.jsonl',)]


@pytest.mark.parametrize(('field', 'value'), [
    ('scenario', 'other'), ('model', 'other'), ('arm', 'tofu'), ('creator', 'other'),
    ('environment', 'other'), ('task', 'other'), ('version', 'other'),
    ('comparison_version', 'other'), ('metric', 'other'), ('judge_model', 'other'),
    ('tasks_digest', 'b' * 64),
])
def test_backend_rejects_self_consistent_artifact_for_the_wrong_cell(collection, field, value):
    out, record, payloads, calls, args = collection
    record[field] = value
    content = json.dumps({'binding': run.artifact_binding(record), 'artifact': True}).encode()
    payloads[record['artifact_path']] = content
    record['artifact_sha256'] = hashlib.sha256(content).hexdigest()
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()

    with pytest.raises(CollectionPublicationError, match=f'journal was not published.*{field}'):
        _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert calls == [('results.jsonl',)]
    (target,) = (out / 'cells').iterdir()
    assert {p.name for p in target.iterdir()} == {'job.json', 'job.log'}
    assert not report.load([out])


def test_backend_artifact_sync_failure_keeps_journal_unpublished(collection, monkeypatch):
    out, record, payloads, _, args = collection
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()
    fsync = os.fsync

    def fail_artifact_sync(fd):
        artifact = next(out.rglob('attempt.json'), None)
        if artifact is not None and os.path.samestat(artifact.stat(), os.fstat(fd)):
            raise OSError('synthetic artifact sync failure')
        fsync(fd)

    monkeypatch.setattr(os, 'fsync', fail_artifact_sync)
    with pytest.raises(CollectionPublicationError, match='journal was not published.*synthetic artifact sync failure'):
        _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
    assert not report.load([out])
    assert not list(out.rglob('results.jsonl'))
    assert not list(out.rglob('attempt.json'))


@pytest.mark.parametrize(('entrypoint', 'fault', 'fail_cleanup', 'expected'), [
    ('standalone', 'rename', False, 'journal was not published'),
    ('standalone', 'rename', True, 'journal was not published'),
    ('standalone', 'sync', False, 'directory sync failed (durability uncertain)'),
    ('standalone', None, True, 'directory sync succeeded'),
    ('standalone', 'sync', True, 'directory sync failed (durability uncertain)'),
    ('cli', 'sync', True, 'directory sync failed (durability uncertain)'),
])
def test_backend_reports_publication_and_cleanup_outcomes(
        collection, monkeypatch, capsys, tmp_path, entrypoint, fault, fail_cleanup, expected):
    out, record, payloads, _, args = collection
    payloads['results.jsonl'] = (json.dumps(record) + '\n').encode()
    target = out / 'cells' / 'synthetic--fixture--baseline--offline'
    result = target / 'results.jsonl'
    rename, fsync, rmtree = Path.rename, os.fsync, shutil.rmtree

    def inject_rename(source, destination):
        if destination == result and fault == 'rename':
            raise OSError('synthetic rename failure')
        return rename(source, destination)

    def inject_sync(fd):
        if (fault == 'sync' and result.exists()
                and os.path.samestat(tmp_path.stat(), os.fstat(fd))):
            raise OSError('synthetic directory sync failure')
        fsync(fd)

    def inject_cleanup(path, *args, **kwargs):
        if fail_cleanup and Path(path).name.startswith(COLLECTION_STAGE_PREFIX):
            raise OSError('synthetic cleanup failure')
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'rename', inject_rename)
    monkeypatch.setattr(os, 'fsync', inject_sync)
    monkeypatch.setattr(shutil, 'rmtree', inject_cleanup)
    if entrypoint == 'cli':
        assert _run_cli(monkeypatch, out) == 1
        diagnostic = capsys.readouterr().out
        assert 'FAILED' in diagnostic and 'done' not in diagnostic
    else:
        with pytest.raises(CollectionPublicationError) as caught:
            _cell_job(args, 'synthetic', 'fixture', 'baseline', 'offline')
        diagnostic = str(caught.value)
        cause = caught.value.__cause__
        if fault and fail_cleanup:
            assert isinstance(cause, ExceptionGroup)
            assert all(isinstance(exc, OSError) for exc in cause.exceptions)
        else:
            assert isinstance(cause, OSError)
    assert str(result) in diagnostic
    assert expected in diagnostic
    assert ('journal is visible' in diagnostic) == (fault != 'rename')
    assert ('synthetic rename failure' in diagnostic) == (fault == 'rename')
    assert ('synthetic directory sync failure' in diagnostic) == (fault == 'sync')
    assert ('staging cleanup failed: OSError: synthetic cleanup failure' in diagnostic) == fail_cleanup
    assert bool(list(target.glob(f'{COLLECTION_STAGE_PREFIX}*'))) == fail_cleanup
    if fault == 'rename':
        assert not result.exists()
        assert not report.load([out])
    else:
        assert read_journal(result) == ([record], [])
        assert run.valid_artifact(result, record)
