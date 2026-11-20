"""Offline identity and matched-comparison boundaries for portability runs."""
import json
from dataclasses import replace

import pytest

from evaluations.portability import models, report, run
from evaluations.portability.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def synthetic_host_identity(monkeypatch):
    client = models.ModelClient
    completed = run.completed_keys
    proof = lambda model, payload: ('b' if model == 'judge' else 'a') * 64
    monkeypatch.setattr(run, 'ModelClient', lambda model, *, identity_reader=None:
                        client(model, identity_reader=identity_reader or proof))

    def known(*args, **kwargs):
        kwargs.setdefault('deployment_identities', {'subject': 'a'*64, 'judge': 'b'*64})
        return completed(*args, **kwargs)
    monkeypatch.setattr(run, 'completed_keys', known)


def arm(task, subject, scratch):
    return {'text': subject.ask(task['prompt'])}


@pytest.fixture
def scenario():
    return Scenario('fixture', '1', 'pass_fraction', ({'id': 'a', 'prompt': 'Task'},),
                    arm, arm, lambda task, artifact, judge: 1.0)


def response(model='subject'):
    return {'model': model, 'choices': [{'finish_reason': 'stop', 'message': {'content': 'answer'}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}


@pytest.mark.parametrize('reported', [None, '', 'other'])
def test_client_rejects_wrong_or_unreported_identity(monkeypatch, reported):
    calls = []
    monkeypatch.setattr(models, '_request', lambda *args:
                        calls.append(args) or response(reported))
    client = models.ModelClient('subject')
    with pytest.raises(models.ModelIdentityError, match='Model identity mismatch'):
        client.ask('Task')
    assert len(calls) == 1
    assert client.usage['calls'] == 0


def test_client_accepts_exact_identity_and_accounts_usage(monkeypatch):
    monkeypatch.setattr(models, '_request', lambda *args: response())
    client = models.ModelClient('subject')
    assert client.ask('Task') == 'answer'
    assert client.usage == {'calls': 1, 'prompt_tokens': 3, 'completion_tokens': 2}


def test_identity_failure_is_infrastructure_and_retried_on_resume(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(models, '_request', lambda *args: response('other'))
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    record = json.loads(path.read_text())
    assert record['error_kind'] == 'infrastructure'
    assert record['score'] is None
    assert not run.completed_keys(path, scenario)
    monkeypatch.setattr(models, '_request', lambda *args: response())
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[-1]['score'] == 1.0
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    assert len(path.read_text().splitlines()) == 2


@pytest.mark.parametrize('change', [dict(version='2'), dict(metric='new_metric'),
                                   dict(judge_model='judge'), dict(comparison_version='3'),
                                   dict(tasks=({'id': 'a', 'prompt': 'Changed'},))])
def test_resume_does_not_reuse_changed_comparison(monkeypatch, tmp_path, scenario, change):
    monkeypatch.setattr(models, '_request', lambda *args: response())
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    changed = replace(scenario, **change)
    assert run.completed_keys(path, scenario)
    assert not run.completed_keys(path, changed)


def records(scenario):
    return [{'scenario': scenario.id, **scenario.provenance(),
             **run.environment_provenance('inline'), 'model': 'subject',
             'creator': 'creator', 'arm': name, 'task': 'a', 'score': score,
             'subject_deployment': 'a'*64, 'judge_deployment': 'b'*64 if scenario.judge_model else None,
             'error': None, 'error_kind': None,
             'attempt_id': 'synthetic', 'artifact_path': 'artifacts/synthetic.json',
             'artifact_sha256': 'c'*64}
            for name, score in [('baseline', 0.0), ('tofu', 1.0)]]


@pytest.mark.parametrize('field,value', [('version', '2'), ('metric', 'other'),
                                        ('judge_model', 'other'), ('tasks_digest', 'e'*64),
                                        ('comparison_version', '3'), ('environment', 'remote')])
def test_report_separates_mismatched_arms(scenario, field, value):
    rows = records(scenario)
    rows[1][field] = value
    summary = report.summarise(rows)
    assert len(summary) == 2
    assert all(info['models'][0]['paired']['n'] == 0 for info in summary.values())


@pytest.mark.parametrize('field', ['version', 'metric', 'judge_model', 'tasks_digest',
                                   'comparison_version', 'environment',
                                   'actual_environment', 'environment_fingerprint'])
def test_report_preserves_rows_without_complete_provenance_as_unverified(scenario, field):
    rows = records(scenario)
    del rows[1][field]
    summary = report.summarise(rows)
    assert summary['fixture'] == report.summarise(rows[:1])['fixture']
    assert summary['legacy_unverified']['records'] == rows[1:]


def test_report_pairs_matching_comparison(scenario):
    summary = report.summarise(records(scenario))['fixture']
    assert summary['version'] == '1'
    assert summary['models'][0]['paired']['n'] == 1
    assert summary['models'][0]['paired']['gain'] == 1.0


def test_judge_identity_failure_is_not_a_measured_zero(monkeypatch, tmp_path, scenario):
    def request(path, body):
        return response('subject' if body['model'] == 'subject' else 'wrong-judge')
    monkeypatch.setattr(models, '_request', request)
    judged = replace(scenario, judge_model='judge',
                     score=lambda task, artifact, judge: float(judge.ask('Score')))
    path = run.run_cell(judged, 'subject', 'tofu', tmp_path)
    row = json.loads(path.read_text())
    assert row['error_kind'] == 'infrastructure'
    assert row['score'] is None
    assert 'wrong-judge' in row['error']


def test_resume_rejects_legacy_digestless_row(tmp_path, scenario):
    row = records(scenario)[0]
    del row['tasks_digest']
    path = tmp_path / 'results.jsonl'
    path.write_text(json.dumps(row) + '\n')
    assert not run.completed_keys(path, scenario)


@pytest.mark.parametrize('label', ['inline', 'remote'])
def test_resume_requires_measured_environment_not_only_user_label(monkeypatch, tmp_path,
                                                                  scenario, label):
    monkeypatch.setattr(models, '_request', lambda *args: response())
    monkeypatch.setattr(run.platform, 'node', lambda: 'synthetic-host-a')
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path, environment=label)
    assert run.completed_keys(path, scenario, environment=label)
    monkeypatch.setattr(run.platform, 'node', lambda: 'synthetic-host-b')
    assert not run.completed_keys(path, scenario, environment=label)
    run.run_cell(scenario, 'subject', 'baseline', tmp_path, environment=label)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]['environment'] == rows[1]['environment'] == label
    assert rows[0]['actual_environment']['host'] == 'synthetic-host-a'
    assert rows[1]['actual_environment']['host'] == 'synthetic-host-b'
    assert rows[0]['environment_fingerprint'] != rows[1]['environment_fingerprint']


def test_report_separates_cross_host_arms_even_with_same_label(monkeypatch, scenario):
    monkeypatch.setattr(run.platform, 'node', lambda: 'synthetic-host-a')
    rows = records(scenario)
    monkeypatch.setattr(run.platform, 'node', lambda: 'synthetic-host-b')
    rows[1].update(run.environment_provenance('inline'))
    summary = report.summarise(rows)
    assert len(summary) == 2
    assert all(info['models'][0]['paired']['n'] == 0 for info in summary.values())


@pytest.mark.parametrize('field', ['comparison_version', 'environment',
                                   'environment_fingerprint', 'actual_environment'])
def test_resume_rejects_legacy_namespace(tmp_path, scenario, field):
    row = records(scenario)[0]
    del row[field]
    path = tmp_path / 'results.jsonl'
    path.write_text(json.dumps(row) + '\n')
    assert not run.completed_keys(path, scenario)


@pytest.mark.parametrize('unknown', [None, ''])
def test_unknown_requested_identity_is_rejected_before_transport(monkeypatch, unknown):
    monkeypatch.setattr(models, '_request', lambda *args: pytest.fail('No call for unknown model'))
    client = models.ModelClient(unknown)
    with pytest.raises(models.ModelIdentityError, match='unknown'):
        client.ask('Task')
