"""Offline regression tests for payload presence in linked portability artifacts."""
import hashlib
import json

import pytest

from evaluations.portability import models, report, run
from evaluations.portability.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.fixture(params=[{'text': 'answer'}, None], ids=['object', 'null'])
def linked_pair(request, monkeypatch, tmp_path):
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'offline'})
    monkeypatch.setattr(models, '_request', lambda *_: {
        'model': 'subject', 'choices': [{'finish_reason': 'stop',
                                       'message': {'content': 'answer'}}]})

    def arm(task, client, scratch):
        client.ask('fixture')
        return request.param

    scenario = Scenario('fixture', '1', 'metric', ({'id': 'a'},), arm, arm, lambda *_: 1.0)
    identities = {'subject': 'a' * 64}
    for arm_name in ('baseline', 'tofu'):
        results = run.run_cell(scenario, 'subject', arm_name, tmp_path,
                               identity_reader=lambda model, _: identities[model])
    return scenario, identities, results, request.param


def test_linked_payload_allows_scored_pair_and_reuse(linked_pair):
    scenario, identities, results, payload = linked_pair
    records = report.load([results.parent])
    for record in records:
        envelope = json.loads((results.parent / record['artifact_path']).read_bytes())
        assert envelope['artifact'] == payload
        assert run.valid_artifact(results, record)
    assert report.summarise(records)['fixture']['models'][0]['paired']['n'] == 1
    assert run.completed_keys(results, scenario, deployment_identities=identities) == {
        ('fixture', 'subject', arm, 'a') for arm in ('baseline', 'tofu')}


def test_binding_only_artifact_does_not_score_or_resume(linked_pair):
    scenario, identities, results, _ = linked_pair
    baseline, tofu = (json.loads(line) for line in results.read_text().splitlines())
    artifact_path = results.parent / baseline['artifact_path']
    envelope = json.loads(artifact_path.read_bytes())
    del envelope['artifact']
    assert envelope == {'binding': run.artifact_binding(baseline)}
    content = json.dumps(envelope).encode()
    artifact_path.write_bytes(content)
    baseline['artifact_sha256'] = hashlib.sha256(content).hexdigest()
    results.write_text(json.dumps(baseline) + '\n' + json.dumps(tofu) + '\n')

    assert not run.valid_artifact(results, baseline)
    loaded = report.load([results.parent])
    assert loaded[0]['score'] is None and loaded[0]['error_kind'] == 'infrastructure'
    summary = report.summarise(loaded)['fixture']
    assert summary['models'][0]['paired']['n'] == 0
    assert summary['infrastructure_failures'] == 1
    assert run.completed_keys(results, scenario, deployment_identities=identities) == {
        ('fixture', 'subject', 'tofu', 'a')}
