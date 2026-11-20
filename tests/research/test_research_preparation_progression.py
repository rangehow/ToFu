"""Synthetic preparation progression; no models, execution or manuscript content."""
from copy import deepcopy
import json

import pytest

from lib.research.action import apply_action_result, capture_research_result, _tool_receipts
from lib.research.program import readiness
from lib.research.workspace import empty_workspace
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


def test_aggregate_run_preparation_does_not_certify_exact_job_science(tmp_path):
    paper = tmp_path / 'synthetic-preparation-paper'
    paper.mkdir()
    artifact = paper / 'synthetic-job-B-observation.json'
    matching_artifact = paper / 'synthetic-job-A-observation.json'
    workspace = empty_workspace('Synthetic preparation paper', 'en')
    workspace['hypothesis'] = 'Synthetic scoped comparison fixture'
    workspace['protocol'].update(
        dataset='synthetic', baseline='control', primary_metric='accuracy',
        falsifier='Synthetic numeric rule', random_seeds=[1, 2, 3],
        decision_rule={'direction': 'higher', 'minimum_effect': 0,
                       'uncertainty_multiplier': 2})
    workspace['runs'] = [{'id': 'synthetic-experiment', 'status': 'planned'}]
    planned = deepcopy(workspace)
    assert not readiness(planned)['ready']
    assert not next(g['ok'] for g in readiness(planned)['gates'] if g['id'] == 'run_evidence')

    bindings = [{'tool': 'synthetic-status', 'capability': 'compute.status',
                 'provider': 'synthetic-local'}]

    def observe(current, task, job, state, scientific=None, reference=None):
        payload = {'job': {'id': job, 'status': state}, **(scientific or {})}
        row = {'toolName': 'synthetic-status', 'status': 'done', 'toolCallId': task}
        if scientific is not None:
            row['toolResultArtifactRef'] = str(reference or artifact)
        capture_research_result(row, json.dumps(payload))
        receipts = _tool_receipts([row])
        before = deepcopy((current, receipts))
        result = apply_action_result(
            action='experiment', workspace=current, task_id=task,
            receipts=receipts, bindings=bindings,
            payload={'run': {'id': 'synthetic-experiment', 'status': 'passed',
                             'remote_job_id': 'synthetic-job-A',
                             'artifact_refs': [str(reference or artifact)]}})
        assert (current, receipts) == before
        return result

    running = observe(planned, 'synthetic-start-receipt', 'synthetic-job-A', 'running')
    assert running['runs'][0]['execution_status'] == 'running'
    assert running['runs'][0]['status'] == 'planned'
    assert running['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert not running['runs'][0]['artifact_refs']

    science = observation_payload(workspace['protocol'], str(artifact))
    artifact.write_text(json.dumps(science), encoding='utf-8')
    retained_bytes = artifact.read_bytes()
    mismatched = observe(running, 'synthetic-other-result', 'synthetic-job-B',
                         'completed', science)
    run = mismatched['runs'][0]
    assert len(run['tool_receipts']) == 2
    assert run['remote_job_id'] == 'synthetic-job-A'
    assert run['execution_status'] == 'running'
    assert run['status'] == 'planned'
    assert run['scientific_outcome'] == 'inconclusive'
    assert not run['artifact_refs']
    assert mismatched['stage'] == 'experiment'
    gates = readiness(mismatched)
    assert not gates['ready']
    assert not next(g['ok'] for g in gates['gates'] if g['id'] == 'run_evidence')

    status_only = observe(mismatched, 'synthetic-status-only-A', 'synthetic-job-A',
                          'completed', reference=matching_artifact)
    run = status_only['runs'][0]
    assert run['execution_status'] == 'completed'
    assert run['remote_job_id'] == 'synthetic-job-A'
    assert run['scientific_outcome'] == 'supported'
    assert str(artifact) in run['artifact_refs']
    assert str(matching_artifact) not in run['artifact_refs']
    assert 'scientific_observations' not in json.loads(run['tool_receipts'][-1]['result_excerpt'])
    assert next(g['ok'] for g in readiness(status_only)['gates'] if g['id'] == 'run_evidence')
    assert not readiness(status_only)['ready']

    matching_science = observation_payload(workspace['protocol'], str(matching_artifact), 'refuted')
    matching_artifact.write_text(json.dumps(matching_science), encoding='utf-8')
    matching_bytes = matching_artifact.read_bytes()
    matched = observe(mismatched, 'synthetic-matched-result', 'synthetic-job-A',
                      'completed', matching_science, matching_artifact)
    run = matched['runs'][0]
    assert run['id'] == 'synthetic-experiment'
    assert run['execution_status'] == 'completed'
    assert run['scientific_outcome'] == 'inconclusive'
    assert str(artifact) in run['artifact_refs']
    assert str(matching_artifact) in run['artifact_refs']
    assert json.loads(run['tool_receipts'][1]['observation_excerpt'])['scientific_outcome'] == 'supported'
    assert json.loads(run['tool_receipts'][-1]['observation_excerpt'])['scientific_outcome'] == 'refuted'
    assert run['tool_receipts'][:2] == mismatched['runs'][0]['tool_receipts']
    assert matched['stage'] == 'evidence'
    gates = readiness(matched)
    assert next(g['ok'] for g in gates['gates'] if g['id'] == 'run_evidence')
    assert not gates['ready']
    assert {'claims', 'manuscript', 'compile', 'claim_traceability'} <= {
        g['id'] for g in gates['gates'] if not g['ok']}
    assert matched['claims'] == []
    assert not matched['source_files']
    assert artifact.read_bytes() == retained_bytes
    assert matching_artifact.read_bytes() == matching_bytes
    assert planned['runs'] == [{'id': 'synthetic-experiment', 'status': 'planned'}]
