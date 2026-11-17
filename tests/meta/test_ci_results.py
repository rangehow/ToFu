"""The merge gate must reject absent, failed, cancelled and unexpected skips."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.github/scripts/check_ci_results.py'
spec = importlib.util.spec_from_file_location('check_ci_results', SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize('evidence', [None, [], {}, {'unit': {}}, {'unit': None}])
def test_missing_results_fail(evidence):
    assert ci.dependency_failures(evidence, set())


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped', 'pending', 'unknown'])
def test_only_explicit_success_passes(result):
    assert ci.dependency_failures({'unit': {'result': result}}, set())


def test_nightly_skip_is_event_specific_and_never_hides_failure():
    passed = {'unit': {'result': 'success'}, 'nightly': {'result': 'skipped'}}
    assert not ci.dependency_failures(passed, {'nightly'})
    assert ci.dependency_failures(passed, set())
    assert ci.dependency_failures({'nightly': {'result': 'failure'}}, {'nightly'})
    assert ci.dependency_failures({'unit': {'result': 'success'}}, {'nightly'})


@pytest.mark.parametrize('payload,code', [('{', 1), ('{}', 1),
    (json.dumps({'unit': {'result': 'success'}}), 0),
    (json.dumps({'unit': {'result': 'cancelled'}}), 1)])
def test_cli_exit_status(payload, code):
    result = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == code, result.stderr


def test_workflow_aggregates_every_lane_and_keeps_merge_candidate_checkout():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    required = jobs['required']
    assert set(required['needs']) == set(jobs) - {'required'}
    assert required['if'] == '${{ always() }}'
    for job in jobs.values():
        assert not job.get('continue-on-error', False)
        for step in job.get('steps', []):
            if str(step.get('uses', '')).startswith('actions/checkout@'):
                assert 'ref' not in step.get('with', {})  # GitHub's merge candidate
    gate = next(step for step in required['steps'] if 'run' in step)
    assert gate['env']['NEEDS'] == '${{ toJSON(needs) }}'
    assert gate['env']['EVENT_NAME'] == '${{ github.event_name }}'
    assert gate['env']['VERIFICATION_PROFILE'] == '${{ needs.verification-scope.outputs.profile }}'
    # Execute the actual workflow shell with realistic dependency evidence.
    for event, profile in (('pull_request', 'incremental'), ('push', 'incremental'),
                           ('schedule', 'full'), ('workflow_dispatch', 'full'), ('push', 'full')):
        outcomes = {name: {'result': 'success'} for name in required['needs']}
        inactive = ({'test-affected'} if profile == 'full' else
                    {'test-unit', 'test-api', 'coverage', 'test-slow', 'visual-nightly', 'perf-nightly'})
        for name in inactive:
            outcomes[name]['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode == 0
        assert run_workflow_gate(gate['run'], event, outcomes, '').returncode != 0
        active = 'test-unit' if profile == 'full' else 'test-affected'
        outcomes[active]['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0
        outcomes[active]['result'] = 'success'
        outcomes['frontend']['result'] = 'failure'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0
        outcomes['frontend']['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0


def run_workflow_gate(command, event, outcomes, profile):
    import os
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command], cwd=ROOT,
        env={**os.environ, 'NEEDS': json.dumps(outcomes), 'EVENT_NAME': event,
             'PATH': os.path.dirname(sys.executable) + os.pathsep + os.environ.get('PATH', ''),
             'VERIFICATION_PROFILE': profile},
        text=True, capture_output=True, timeout=10)


def test_dotfile_artifacts_opt_into_hidden_file_upload():
    # upload-artifact@v4 (>= 4.4) skips hidden files by default and then reports
    # "No files were found" for a path such as .coverage.
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    uploads = [step['with'] for job in jobs.values() for step in job.get('steps', [])
               if str(step.get('uses', '')).startswith('actions/upload-artifact@')]
    hidden = [upload for upload in uploads
              if any(part.startswith('.') and part not in {'.', '..'}
                     for line in str(upload['path']).splitlines()
                     for part in Path(line.strip()).parts)]
    assert {upload['name'] for upload in hidden} >= {'coverage-unit-${{ matrix.shard }}', 'coverage-api'}
    for upload in hidden:
        assert upload.get('include-hidden-files') is True, upload['name']


def test_browser_lane_uses_the_same_journeys_as_local_development():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    commands = [step.get('run', '') for step in jobs['e2e']['steps']]
    assert any('make test-e2e' in command for command in commands)


def test_incremental_and_qualification_jobs_have_explicit_scope_and_receipts():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    scope = jobs['verification-scope']
    assert any(step.get('with', {}).get('fetch-depth') == 0 for step in scope['steps'])
    affected = jobs['test-affected']
    assert "'incremental'" in affected['if']
    run = next(step for step in affected['steps'] if 'landing_verification check' in step.get('run', ''))
    assert '--json' in run['run'] and '--base' in run['run'] and '--expected-revision' in run['run']
    assert run['env']['BASE_REVISION'] == '${{ needs.verification-scope.outputs.base }}'
    assert run['env']['EXPECTED_REVISION'] == '${{ needs.verification-scope.outputs.head }}'
    artifacts = [step for step in affected['steps'] if str(step.get('uses', '')).startswith('actions/upload-artifact@')]
    assert any('${{ github.sha }}' in step['with']['name'] and
               step['with'].get('if-no-files-found') == 'error' for step in artifacts)
    for name in ('test-unit', 'test-api', 'test-slow', 'coverage', 'visual-nightly', 'perf-nightly'):
        assert "'full'" in jobs[name]['if']


def test_unit_tier_shards_cover_every_file_once_and_gate_all_shard_coverage():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    unit = jobs['test-unit']
    shards = unit['strategy']['matrix']['shard']
    assert unit['strategy']['fail-fast'] is False
    assert shards == list(range(1, len(shards) + 1))
    assert unit['env']['TOFU_TEST_SHARD'] == f'${{{{ matrix.shard }}}}/{len(shards)}'
    gate = next(step['run'] for step in jobs['coverage']['steps'] if 'check_coverage.py' in step.get('run', ''))
    assert 'coverage-unit-*/.coverage' in gate


def test_shard_selection_partitions_whole_files(monkeypatch):
    from tests.conftest import _shard_selection

    class Item:
        def __init__(self, nodeid):
            self.nodeid = nodeid

    class Hook:
        def __init__(self):
            self.deselected = []

        def pytest_deselected(self, items):
            self.deselected.extend(items)

    class Config:
        def __init__(self):
            self.hook = Hook()

    every = [Item(f'tests/area{n % 7}/test_{n}.py::test_{case}') for n in range(60) for case in 'ab']
    owner = {}
    for index in (1, 2, 3):
        monkeypatch.setenv('TOFU_TEST_SHARD', f'{index}/3')
        items, config = list(every), Config()
        _shard_selection(config, items)
        assert len(items) + len(config.hook.deselected) == len(every)
        for item in items:
            path = item.nodeid.split('::', 1)[0]
            assert owner.setdefault(path, index) == index
    assert len(owner) == 60
    monkeypatch.delenv('TOFU_TEST_SHARD')
    items = list(every)
    _shard_selection(Config(), items)
    assert items == every
    monkeypatch.setenv('TOFU_TEST_SHARD', '4/3')
    with pytest.raises(pytest.UsageError):
        _shard_selection(Config(), list(every))
