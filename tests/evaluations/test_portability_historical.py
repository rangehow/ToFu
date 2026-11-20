"""Offline regressions for reading historical portability journals."""
import hashlib
import json

import pytest

from evaluations.portability import report
from evaluations.portability.journal import read_journal

pytestmark = pytest.mark.unit


_WRITER_FRAME = (
    '  File "/historical/evaluations/portability/run.py", line 158, in run_cell\n'
    '''    (artifacts / f'{task["id"]}.json').write_text(json.dumps(artifact, default=str)[:4_000_000])\n'''
)
_ARM_FRAME = (
    '  File "/historical/evaluations/portability/run.py", line 150, in run_cell\n'
    '    artifact = run_arm(task, subject, scratch)\n'
)
_LONG_ERROR = "OSError: [Errno 36] File name too long: '/historical/artifacts/" + 'é' * 2100 + ".json'"
_PATH_ONLY = {'artifact_path': 'artifacts/old-task.json'}
_PARTIAL_BINDING = {**_PATH_ONLY, 'attempt_id': 'historical-attempt'}


@pytest.fixture
def agent_zero():
    environment = {'platform': 'fixture'}
    return {
        'scenario': 'fixture', 'model': 'subject', 'arm': 'baseline', 'task': 'current',
        'version': '1', 'metric': 'exact', 'judge_model': None, 'tasks_digest': 'a' * 64,
        'comparison_version': '1', 'environment': 'inline', 'actual_environment': environment,
        'environment_fingerprint': hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
        'score': 0, 'error_kind': 'agent', 'error': 'ValueError: ordinary arm failure',
    }


def _historical(agent_zero, *, frame=_WRITER_FRAME, terminal=_LONG_ERROR, **fields):
    return {**agent_zero, 'task': 'historical', 'error': terminal[:2000],
            'traceback': f'Traceback (most recent call last):\n{frame}{terminal}\n', **fields}


def _write_journal(tmp_path, *rows):
    path = tmp_path / 'results.jsonl'
    path.write_bytes(''.join(json.dumps(row) + '\n' for row in rows).encode())
    return path


@pytest.mark.parametrize(('link', 'infrastructure'), [({}, 1), (_PARTIAL_BINDING, 1), (_PATH_ONLY, 0)])
def test_historical_truncation_preserves_subsequent_current_agent_zero(tmp_path, agent_zero, link, infrastructure):
    historical = _historical(agent_zero, **link)
    path = _write_journal(tmp_path, historical, agent_zero)
    original = path.read_bytes()

    assert read_journal(path) == ([historical, agent_zero], [])
    summary = report.summarise(report.load([tmp_path]))
    assert 'journal_issues' not in summary
    comparison = summary['fixture']
    assert comparison['infrastructure_failures'] == infrastructure
    model = comparison['models'][0]
    assert (model['baseline'], model['n_baseline'], model['errors']['baseline']) == (0, 1, 1)
    if link == _PATH_ONLY:
        assert summary['legacy_unverified']['records'] == [
            {**historical, 'score': None, 'error_kind': 'infrastructure'}]
    else:
        assert 'legacy_unverified' not in summary
    assert path.read_bytes() == original


@pytest.mark.parametrize('change', [
    {'error': _LONG_ERROR[:1999]},
    {'error': _LONG_ERROR[:2001]},
    {'error': _LONG_ERROR[:1999] + 'x'},
    {'terminal': _LONG_ERROR.replace('[Errno 36]', '[Errno 28]')},
    {'frame': _ARM_FRAME + _WRITER_FRAME},
    {'frame': _WRITER_FRAME.replace('/portability/', '/scenario/')},
    {'frame': _WRITER_FRAME.replace('write_text', 'read_text')},
    {'frame': ''},
    {'artifact_sha256': 'b' * 64},
], ids=['short', 'long', 'mismatch', 'other-errno', 'later-frame', 'wrong-file', 'wrong-source',
        'no-frame', 'complete-binding'])
def test_partial_binding_without_exact_historical_evidence_stops_prefix(tmp_path, agent_zero, change):
    candidate = _historical(agent_zero, **_PARTIAL_BINDING, **change)
    path = _write_journal(tmp_path, agent_zero, candidate, {**agent_zero, 'task': 'unread'})
    assert read_journal(path) == ([agent_zero], [{'path': str(path), 'line': 2, 'kind': 'malformed_row'}])


def test_current_unlinked_failures_still_count(tmp_path, agent_zero):
    arm_error = _historical(agent_zero, frame=_ARM_FRAME + _WRITER_FRAME)
    storage_error = {**agent_zero, 'task': 'storage', 'score': None, 'error_kind': 'infrastructure',
                     'error': 'Artifact persistence failed: OSError: [Errno 36] File name too long'}
    _write_journal(tmp_path, arm_error, storage_error)
    summary = report.summarise(report.load([tmp_path]))
    assert 'legacy_unverified' not in summary
    comparison = summary['fixture']
    assert comparison['infrastructure_failures'] == 1
    model = comparison['models'][0]
    assert (model['baseline'], model['n_baseline'], model['errors']['baseline']) == (0, 1, 1)


@pytest.mark.parametrize('kind', ['agent', 'infrastructure'])
def test_path_only_failure_without_writer_evidence_is_an_unchanged_diagnostic(tmp_path, agent_zero, kind):
    historical = _historical(agent_zero, frame=_ARM_FRAME, **_PATH_ONLY,
                             error_kind=kind, score=0 if kind == 'agent' else None)
    _write_journal(tmp_path, historical)
    assert report.summarise(report.load([tmp_path])) == {
        'legacy_unverified': {'status': 'legacy_unverified', 'records': [historical]}}
