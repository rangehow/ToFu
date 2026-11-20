"""Offline specifications for immutable portability artifact namespaces."""
import json
from dataclasses import replace

import pytest

from evaluations.portability import run
from evaluations.portability.scenario import Scenario

pytestmark = pytest.mark.unit


@pytest.fixture
def scenario():
    def arm(task, subject, scratch):
        return {'answer': task['prompt']}

    return Scenario('fixture', '1', 'metric-v1', ({'id': 'a', 'prompt': 'first'},),
                    arm, arm, lambda task, artifact, judge: 1.0)


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'host-a'})


def artifact_snapshot(directory):
    return {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob('*.json')}


@pytest.mark.parametrize('change', [dict(version='2'), dict(metric='metric-v2'),
                                   dict(judge_model='judge-v2'),
                                   dict(comparison_version='next'),
                                   dict(tasks=({'id': 'a', 'prompt': 'second'},))])
def test_comparison_changes_preserve_existing_artifacts(tmp_path, scenario, change):
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    before = artifact_snapshot(tmp_path)
    assert len(before) == 1
    run.run_cell(replace(scenario, **change), 'subject', 'baseline', tmp_path)
    after = artifact_snapshot(tmp_path)
    assert len(after) == 2
    assert len({path.parent for path in after}) == 2
    assert all(after[path] == content for path, content in before.items())


def test_actual_environment_changes_preserve_existing_artifacts(monkeypatch, tmp_path, scenario):
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    before = artifact_snapshot(tmp_path)
    monkeypatch.setattr(run, 'actual_environment', lambda: {'host': 'host-b'})
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    after = artifact_snapshot(tmp_path)
    assert len(after) == 2
    assert len({path.parent for path in after}) == 2
    assert all(after[path] == content for path, content in before.items())


def test_reruns_preserve_prior_artifact(tmp_path, scenario):
    path = run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    before = artifact_snapshot(tmp_path)
    run.run_cell(scenario, 'subject', 'baseline', tmp_path)
    after = artifact_snapshot(tmp_path)
    assert len(after) == 2
    assert len({path.parent for path in after}) == 1
    assert all(after[path] == content for path, content in before.items())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len({row['artifact_path'] for row in rows}) == 2
    assert all((tmp_path / row['artifact_path']).is_file() for row in rows)


def test_legacy_files_are_not_rewritten(tmp_path, scenario):
    legacy_artifact = tmp_path / 'artifacts' / 'fixture' / 'subject' / 'baseline' / 'a.json'
    legacy_artifact.parent.mkdir(parents=True)
    legacy_artifact.write_bytes(b'{"legacy": true}\n')
    results = tmp_path / 'results.jsonl'
    previous = b'{"scenario":"legacy","model":"subject","arm":"baseline","task":"a","score":1}\n'
    results.write_bytes(previous)
    assert run.run_cell(scenario, 'subject', 'baseline', tmp_path) == results
    assert legacy_artifact.read_bytes() == b'{"legacy": true}\n'
    assert results.read_bytes().startswith(previous)
