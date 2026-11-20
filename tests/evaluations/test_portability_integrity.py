"""Offline regression tests for append-only comparisons and artifact reuse."""
import json
import os
import stat
from errno import ENOSPC
from dataclasses import replace
from pathlib import Path

import pytest

from evaluations.portability import models, report, run
from evaluations.portability.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'offline'})
    monkeypatch.setattr(models, '_request', lambda *_: {
        'model': 'subject', 'choices': [{'finish_reason': 'stop',
                                       'message': {'content': 'answer'}}]})
    def arm(task, client, scratch):
        return {'text': client.ask('fixture')}
    return Scenario('fixture', '1', 'metric', ({'id': 'a'},), arm, arm, lambda *_: 1.0)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize('entrypoint', ['standalone', 'cli'])
def test_artifact_and_directory_links_synced_before_journal_append(monkeypatch, tmp_path, scenario, entrypoint):
    out = tmp_path / 'new-parent' / 'run'
    synced = []
    fsync = run.os.fsync
    open_path = Path.open

    def observe_sync(fd):
        fsync(fd)
        synced.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))

    def observe_open(path, mode='r', *args, **kwargs):
        if path == out / 'results.jsonl' and mode == 'a':
            (artifact,) = (out / 'artifacts').rglob('*.json')
            durable_chain = [artifact, *(p for p in artifact.parents if p.is_relative_to(tmp_path))]
            assert all((p.stat().st_dev, p.stat().st_ino) in synced for p in durable_chain)
        return open_path(path, mode, *args, **kwargs)

    monkeypatch.setattr(run.os, 'fsync', observe_sync)
    monkeypatch.setattr(Path, 'open', observe_open)
    if entrypoint == 'cli':
        from evaluations.portability import __main__ as cli
        from evaluations.portability import scenario as declarations

        monkeypatch.setattr(cli, '_creators', lambda _: {'subject': 'offline'})
        monkeypatch.setattr(declarations, 'load_scenario', lambda _: scenario)
        assert cli.main(['run', '--scenario', 'fixture', '--models', 'subject',
                         '--arms', 'baseline', '--out', str(out)]) == 0
        path = out / 'results.jsonl'
    else:
        path = run.run_cell(scenario, 'subject', 'baseline', out)
    assert run.valid_artifact(path, rows(path)[0])


@pytest.mark.parametrize('stage', ['mkdir', 'open', 'file-sync', 'directory-sync'])
def test_artifact_storage_failure_is_unscored_and_retryable(monkeypatch, tmp_path, scenario, stage):
    failure = OSError(ENOSPC, 'synthetic artifact storage failure')
    mkdir, open_path, fsync = Path.mkdir, Path.open, run.os.fsync

    def fail_mkdir(path, *args, **kwargs):
        if stage == 'mkdir' and 'artifacts' in path.parts:
            raise failure
        return mkdir(path, *args, **kwargs)

    def fail_open(path, mode='r', *args, **kwargs):
        if stage == 'open' and mode == 'xb' and 'artifacts' in path.parts:
            raise failure
        return open_path(path, mode, *args, **kwargs)

    def fail_sync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (stage == 'file-sync' and not is_directory
                or stage == 'directory-sync' and is_directory):
            # Fail just the artifact sync, not the subsequent journal sync.
            fault.setattr(run.os, 'fsync', fsync)
            raise failure
        return fsync(fd)

    proof = lambda *_: 'a'*64
    with monkeypatch.context() as fault:
        fault.setattr(Path, 'mkdir', fail_mkdir)
        fault.setattr(Path, 'open', fail_open)
        fault.setattr(run.os, 'fsync', fail_sync)
        path = run.run_cell(scenario, 'subject', 'baseline', tmp_path, identity_reader=proof)
    (record,) = rows(path)
    assert record['error_kind'] == 'infrastructure' and record['score'] is None
    assert 'synthetic artifact storage failure' in record['error']
    assert not {'attempt_id', 'artifact_path', 'artifact_sha256'} & record.keys()
    summary = report.summarise(report.load([tmp_path]))
    assert 'legacy_unverified' not in summary
    assert summary['fixture']['infrastructure_failures'] == 1
    assert not run.completed_keys(path, scenario, deployment_identities={'subject': 'a'*64})
    assert run.run_cell(scenario, 'subject', 'baseline', tmp_path, identity_reader=proof) == path
    assert run.valid_artifact(path, rows(path)[1])


def test_journal_open_failure_propagates_without_a_scored_record(monkeypatch, tmp_path, scenario):
    open_path = Path.open

    def fail_journal_open(path, mode='r', *args, **kwargs):
        if path == tmp_path / 'results.jsonl' and mode == 'a':
            raise OSError(ENOSPC, 'synthetic journal storage failure')
        return open_path(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', fail_journal_open)
    with pytest.raises(OSError, match='synthetic journal storage failure'):
        run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    assert not (tmp_path / 'results.jsonl').exists()


def test_arm_failure_remains_agent_zero(tmp_path, scenario):
    def failing_arm(*_):
        raise ValueError('synthetic arm failure')

    path = run.run_cell(replace(scenario, baseline=failing_arm), 'subject', 'baseline', tmp_path)
    (record,) = rows(path)
    assert record['error_kind'] == 'agent' and record['score'] == 0.0
    row = report.summarise(report.load([tmp_path]))['fixture']['models'][0]
    assert row['errors']['baseline'] == 1 and row['baseline'] == 0.0


def test_journal_groups_valid_comparisons(tmp_path, scenario):
    for s in (scenario, replace(scenario, version='2')):
        for arm in ('baseline', 'tofu'):
            path = run.run_cell(s, 'subject', arm, tmp_path, identity_reader=lambda *_: 'a'*64)
    summaries = report.summarise(rows(path))
    assert len(summaries) == 2
    assert {s['version'] for s in summaries.values()} == {'1', '2'}
    assert all(s['models'][0]['paired']['n'] == 1 for s in summaries.values())


def test_infra_unknown_does_not_poison_valid_pair(tmp_path, scenario):
    for arm in ('baseline', 'tofu'):
        path = run.run_cell(scenario, 'subject', arm, tmp_path, identity_reader=lambda *_: 'a'*64)
    valid = rows(path)
    failed = {**valid[0], 'error_kind': 'infrastructure', 'score': None,
              'subject_deployment': None}
    summary = report.summarise([failed, *valid])
    assert len(summary) == 1
    info = summary['fixture']
    row = info['models'][0]
    assert row['paired']['n'] == 1 and row['deployment_matched']
    assert row['infrastructure_failures'] == 0
    assert info['infrastructure_failures'] == 1
    assert info['unattributed_infrastructure_failures'] == {'subject': {'baseline': 1}}


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'unrelated', 'provenance'])
def test_resume_checks_exact_artifact_and_link(tmp_path, scenario, damage):
    proof = lambda *_: 'a'*64
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path, identity_reader=proof)
    original = rows(path)[0]
    artifact = tmp_path / original['artifact_path']
    if damage == 'missing':
        artifact.unlink()
    elif damage == 'corrupt':
        artifact.write_bytes(artifact.read_bytes() + b' ')
    elif damage == 'unrelated':
        run.run_cell(scenario, 'subject', 'tofu', tmp_path, identity_reader=proof)
        other = rows(path)[1]
        original['artifact_path'] = other['artifact_path']
        original['artifact_sha256'] = other.get('artifact_sha256')
        path.write_text(json.dumps(original) + '\n')
    else:
        original['task'] = 'not-this-task'
        path.write_text(json.dumps(original) + '\n')
    assert not run.completed_keys(path, scenario, deployment_identities={'subject': 'a'*64})


@pytest.mark.parametrize('reason', [None, 'length', 'tool_calls', 'unknown', 'content_filter'])
def test_nonterminal_or_incomplete_response_is_infrastructure(monkeypatch, tmp_path, scenario, reason):
    monkeypatch.setattr(models, '_request', lambda *_: {
        'model': 'subject', 'choices': [{'finish_reason': reason,
                                       'message': {'content': 'partial'}}]})
    s = replace(scenario, score=lambda *_: pytest.fail('Incomplete output must not be scored'))
    row = rows(run.run_cell(s, 'subject', 'baseline', tmp_path))[0]
    assert row['error_kind'] == 'infrastructure' and row['score'] is None


def test_multiple_deployments_pair_only_within_exact_identity(tmp_path, scenario):
    for deployment in ('a', 'b'):
        for arm in ('baseline', 'tofu'):
            path = run.run_cell(scenario, 'subject', arm, tmp_path,
                                identity_reader=lambda *_, d=deployment: d*64)
    info = report.summarise(rows(path))['fixture']
    assert len(info['models']) == 2
    assert {r['subject_deployment'] for r in info['models']} == {'a'*64, 'b'*64}
    assert all(r['paired']['n'] == 1 for r in info['models'])
    assert all(r['namespace']['actual_environment'] == {'host': 'offline'} for r in info['models'])
    text = report.markdown({'fixture': info})
    assert 'a'*64 in text and 'b'*64 in text


def test_infrastructure_counts_once_across_deployments_and_comparisons(tmp_path, scenario):
    for version, deployment in [('1', 'a'), ('1', 'b'), ('2', 'a')]:
        for arm in ('baseline', 'tofu'):
            path = run.run_cell(replace(scenario, version=version), 'subject', arm, tmp_path,
                                identity_reader=lambda *_, d=deployment: d*64)
    original = rows(path)
    failed = {**original[0], 'error_kind': 'infrastructure', 'score': None}
    unknown = {**failed, 'subject_deployment': None, 'arm': 'tofu'}
    summary = report.summarise([*original, failed, unknown])
    by_version = {info['version']: info for info in summary.values()}
    assert by_version['1']['infrastructure_failures'] == 2
    assert by_version['1']['unattributed_infrastructure_failures'] == {'subject': {'tofu': 1}}
    attributed = {r['subject_deployment']: r['infrastructure_failures']
                  for r in by_version['1']['models']}
    assert attributed == {'a'*64: 1, 'b'*64: 0}
    assert by_version['2']['infrastructure_failures'] == 0
    assert all(r['paired']['n'] == 1 for info in summary.values() for r in info['models'])
    assert 'Unattributed infrastructure failures' in report.markdown(summary)


@pytest.mark.parametrize('legacy_shape', ['minimal', 'unlinked', 'path-only'])
def test_report_preserves_legacy_rows_separately_from_verified_results(tmp_path, scenario, legacy_shape):
    for arm in ('baseline', 'tofu'):
        path = run.run_cell(scenario, 'subject', arm, tmp_path, identity_reader=lambda *_: 'a'*64)
    original = rows(path)
    legacy = {'scenario': 'fixture', 'model': 'subject', 'arm': 'baseline', 'task': 'a', 'score': 0}
    if legacy_shape != 'minimal':
        omitted = {'attempt_id', 'artifact_sha256'}
        if legacy_shape == 'unlinked':
            omitted.add('artifact_path')
        legacy = {**{k: v for k, v in original[0].items() if k not in omitted}, 'score': 0}
    path.write_text('\n'.join(json.dumps(row) for row in [legacy, *original]) + '\n')
    before = path.read_bytes()
    summary = report.summarise(report.load([tmp_path]))
    assert summary['fixture'] == report.summarise(original)['fixture']
    assert summary['legacy_unverified']['records'] == [legacy]
    assert 'Legacy / unverified' in report.markdown(summary)
    assert path.read_bytes() == before


def test_unknown_scored_attempt_does_not_suppress_known_pair(tmp_path, scenario):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    for arm in ('baseline', 'tofu'):
        run.run_cell(scenario, 'subject', arm, tmp_path, identity_reader=lambda *_: 'a'*64)
    result = report.summarise(rows(path))['fixture']['models']
    known = next(r for r in result if r['subject_deployment'] == 'a'*64)
    unknown = next(r for r in result if r['subject_deployment'] is None)
    assert known['paired']['n'] == 1
    assert unknown['paired']['n'] == 0


def test_backend_collection_retains_referenced_artifacts(monkeypatch, tmp_path, scenario):
    import shutil
    import sys
    from types import SimpleNamespace
    from evaluations.portability.__main__ import _cell_job

    source = tmp_path / 'source'
    path = run.run_cell(scenario, 'subject', 'baseline', source, identity_reader=lambda *_: 'a'*64)
    calls = []
    class Spec:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        def validate(self):
            return self
    class Compute:
        def submit(self, spec):
            return SimpleNamespace(job_id='offline', to_dict=lambda: {})
        def effective_config(self, ref):
            return {'config': {}}
        def verify(self, *args):
            return []
        def status(self, ref):
            return SimpleNamespace(terminal=True, state='succeeded', exit_code=0)
        def logs(self, ref):
            return ''
        def artifacts(self, ref, names, destination):
            calls.extend(names)
            for name in names:
                dest = Path(destination) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, dest)
    monkeypatch.setitem(sys.modules, 'lib.compute.jobs',
                        SimpleNamespace(JobSpec=Spec, backend=lambda *_args, **_kwargs: Compute()))
    args = SimpleNamespace(backend_option=[], backend='offline', python='python',
                           environment='inline', remote_repo=None, out=str(tmp_path / 'collected'))
    monkeypatch.setattr('evaluations.portability.scenario.load_scenario', lambda _name: scenario)
    target = _cell_job(args, 'fixture', 'subject', 'baseline', None)
    original = rows(path)[0]
    assert original['artifact_path'] in calls
    assert (target / original['artifact_path']).read_bytes() == (source / original['artifact_path']).read_bytes()
    assert run.valid_artifact(target / 'results.jsonl', original)


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_report_load_excludes_unverifiable_artifacts(tmp_path, scenario, damage):
    for arm in ('baseline', 'tofu'):
        path = run.run_cell(scenario, 'subject', arm, tmp_path, identity_reader=lambda *_: 'a'*64)
    original = rows(path)
    target = tmp_path / original[0]['artifact_path']
    if damage == 'missing':
        target.unlink()
    elif damage == 'corrupt':
        target.write_bytes(target.read_bytes() + b'changed')
    summary = report.summarise(report.load([tmp_path]))
    assert 'legacy_unverified' not in summary
    info = summary['fixture']['models'][0]
    assert info['paired']['gain'] is None
    assert info['infrastructure_failures'] == 1
    assert path.read_text().splitlines() == [json.dumps(record) for record in original]


def test_resume_binding_does_not_coerce_score_types(tmp_path, scenario):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path, identity_reader=lambda *_: 'a'*64)
    record = rows(path)[0]
    assert record['score'] == 1.0
    record['score'] = True
    path.write_text(json.dumps(record) + '\n')
    assert not run.valid_artifact(path, record)
    assert not run.completed_keys(path, scenario, deployment_identities={'subject': 'a'*64})


@pytest.mark.parametrize(('tail', 'kind'), [
    (b'{"scenario":', 'incomplete_final_row'),
    (b'{"scenario":"\xc3', 'incomplete_final_row'),
    (b'{"score":1,"score":0}\n', 'malformed_row'),
    (b'{"score":1e999}\n', 'malformed_row'),
    (b'{broken}\n{"later":"not admitted"}\n', 'malformed_row'),
])
def test_report_keeps_committed_prefix_and_exposes_journal_issue(tmp_path, scenario, tail, kind):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path, identity_reader=lambda *_: 'a'*64)
    original = path.read_bytes() + tail
    path.write_bytes(original)
    summary = report.summarise(report.load([tmp_path]))
    assert summary['fixture']['models'][0]['baseline'] == 1.0
    assert summary['journal_issues']['issues'] == [{'path': str(path), 'line': 2, 'kind': kind}]
    assert kind in report.markdown(summary)
    assert path.read_bytes() == original


@pytest.mark.parametrize('partial_binding', [False, True])
def test_proven_historical_storage_error_is_not_an_agent_zero(tmp_path, scenario, partial_binding):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    modern = rows(path)[0]
    bad = {k: v for k, v in modern.items() if k not in {'artifact_path', 'attempt_id', 'artifact_sha256'}}
    error = 'OSError: [Errno 28] No space left on device'
    trace = ('Traceback (most recent call last):\n'
             '  File "/fixture/evaluations/portability/run.py", line 158, in run_cell\n'
             "    with artifact_path.open('xb') as stream:\n"
             f'{error}\n')
    bad.update(score=0, error_kind='agent', error=error, traceback=trace)
    if partial_binding:
        bad.update(attempt_id=modern['attempt_id'], artifact_path=modern['artifact_path'])
    historical = {k: bad[k] for k in ('scenario', 'model', 'task', 'arm', 'score', 'error',
                                      'error_kind', 'traceback')}
    path.write_text(''.join(json.dumps(row) + '\n' for row in (bad, historical)))
    summary = report.summarise(report.load([tmp_path]))
    assert summary['fixture']['infrastructure_failures'] == 1
    assert summary['fixture']['models'][0]['baseline'] is None
    assert summary['legacy_unverified']['records'][0]['score'] is None
    assert summary['legacy_unverified']['records'][0]['error_kind'] == 'infrastructure'
    assert rows(path) == [bad, historical]


@pytest.mark.parametrize('failure', ['errno', 'words', 'similar-call'])
def test_ambiguous_storage_details_do_not_override_an_arm_zero(tmp_path, scenario, failure):
    def failing_arm(*_):
        if failure == 'errno':
            raise OSError(ENOSPC, 'No space left on device')
        if failure == 'words':
            raise OSError('synthetic storage failure: disk full')
        artifact_path = tmp_path / 'missing'
        with artifact_path.open('xb') as stream:
            raise ValueError('ordinary arm failure')

    path = run.run_cell(replace(scenario, baseline=failing_arm), 'subject', 'baseline', tmp_path)
    summary = report.summarise(report.load([tmp_path]))['fixture']
    assert summary['infrastructure_failures'] == 0
    assert summary['models'][0]['baseline'] == 0.0
    assert summary['models'][0]['errors']['baseline'] == 1
    assert rows(path)[0]['error_kind'] == 'agent'


@pytest.mark.parametrize('patch', [
    {'creator': []}, {'subject_deployment': {}}, {'scenario': ''}, {'model': []},
    {'arm': None}, {'task': ''}, {'score': True}, {'score': 1.5}, {'score': float('nan')},
    {'metric': 7}, {'tasks_digest': 'wrong'}, {'environment_fingerprint': '0'*64},
    {'actual_environment': []}, {'attempt_id': None}, {'artifact_sha256': 'bad'},
    {'artifact_path': 'artifacts/../outside.json'}, {'error_kind': 'infrastructure'},
    {'usage': {'calls': True}}, {},
])
def test_schema_invalid_row_stops_at_readable_prefix(tmp_path, scenario, patch):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    good = path.read_bytes()
    row = json.loads(good)
    invalid = {**row, **patch} if patch else {}
    original = good + (json.dumps(invalid) + '\n').encode() + good
    path.write_bytes(original)
    summary = report.summarise(report.load([tmp_path]))
    assert summary['fixture']['models'][0]['baseline'] == 1.0
    assert 'legacy_unverified' not in summary
    assert summary['journal_issues']['issues'] == [
        {'path': str(path), 'line': 2, 'kind': 'malformed_row'}]
    assert path.read_bytes() == original


def test_structured_keys_do_not_collide_with_scenario_ids(tmp_path, scenario):
    import hashlib

    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    first = rows(path)[0]
    namespace = report.summarise([first])['fixture']['namespace']
    old_key = 'fixture@' + hashlib.sha256(json.dumps(namespace, sort_keys=True).encode()).hexdigest()
    structured_key = json.dumps(['fixture', namespace], sort_keys=True)
    records = [first, {**first, 'version': '2'}, {**first, 'scenario': old_key},
               {**first, 'scenario': structured_key}, {**first, 'scenario': 'journal_issues'}]
    summary = report.summarise(records)
    assert len(summary) == len(records)
    assert summary[structured_key]['namespace'] == namespace
    assert {(row['scenario'], row['version']) for row in summary.values()} == {
        (r['scenario'], r['version']) for r in records}
