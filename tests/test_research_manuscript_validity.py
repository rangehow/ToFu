"""Manuscript currency depends on reviewed science, not compilation alone."""
import copy

import json

from lib.research.decisions import AUDIT_CHECKS

import pytest

from lib.research.action import apply_action_result
from lib.research.program import (
    claim_evidence_digest, dependency_digests, normalize_program_fields,
    readiness, scientific_dependency_digest, source_tree_digest,
)
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def _support(workspace):
    dependencies = dependency_digests(workspace)
    workspace['runs'] = [{
        'id': 'r1', 'status': 'passed', 'execution_status': 'completed',
        'scientific_outcome': 'supported', 'artifact_ref': 'evidence:1',
        'tool_receipts': [{'tool': 'run_command', 'status': 'done',
                           'result_digest': 'abc', 'result_excerpt': json.dumps({
                               'scientific_audit': {key: {
                                   'status': 'verified', 'evidence_refs': ['evidence:1']
                               } for key in AUDIT_CHECKS}})}], **dependencies,
    }]
    workspace.update(normalize_program_fields(workspace))
    claim = {'id': 'c1', 'text': workspace['hypothesis'], 'status': 'supported',
             'evidence_refs': ['r1'], **dependencies}
    claim['evidence_digest'] = claim_evidence_digest(claim, workspace['runs'])
    workspace['claims'] = [claim]
    workspace.update(normalize_program_fields(workspace))


def _generate(workspace):
    return apply_action_result(
        action='manuscript', workspace=workspace, task_id='write1', bindings=[],
        receipts=[], payload={'manuscript': {
            key: 'Reviewed scientific text' for key in
            ('title', 'abstract', 'method', 'results', 'limitations')},
            'source_updates': [{'path': 'main.tex', 'content': 'Reviewed source'}]})


def _compiled(workspace):
    digest = source_tree_digest(workspace['source_files'])
    workspace['compilation'].update(status='passing', source_digest=digest)
    workspace['publication'].update(status='published', source_digest=digest)


def _ready_workspace():
    workspace = empty_workspace('Science')
    workspace['hypothesis'] = 'Initial hypothesis'
    workspace['protocol'].update({key: 'Specified' for key in
        ('primary_metric', 'baseline', 'dataset', 'falsifier')})
    _support(workspace)
    workspace = _generate(workspace)
    _compiled(workspace)
    assert readiness(workspace)['ready']
    assert readiness(workspace)['published_current']
    return workspace


def test_new_supported_science_does_not_revalidate_old_manuscript():
    workspace = _ready_workspace()
    old_digest = workspace['manuscript']['scientific_dependency_digest']
    workspace['hypothesis'] = 'New hypothesis with newly supported claim'
    _support(workspace)
    assert workspace['claims'][0]['status'] == 'supported'
    assert workspace['manuscript']['scientific_dependency_digest'] == old_digest
    got = readiness(workspace)
    assert not got['ready']
    assert not got['published_current']
    gates = {row['id']: row['ok'] for row in got['gates']}
    assert gates['compile'] and gates['claims'] and gates['run_evidence']
    assert not gates['manuscript']
    workspace = _generate(workspace)
    _compiled(workspace)
    assert readiness(workspace)['ready']
    assert readiness(workspace)['published_current']


def test_legacy_manuscript_remains_unverified_until_review():
    workspace = _ready_workspace()
    del workspace['manuscript']['scientific_dependency_digest']
    workspace.update(normalize_program_fields(workspace))
    assert workspace['manuscript']['scientific_dependency_digest'] == ''
    assert not readiness(workspace)['ready']
    assert not readiness(workspace)['published_current']
    unchanged = apply_action_result(action='manuscript', workspace=workspace,
        task_id='empty', bindings=[], receipts=[], payload={})
    assert not readiness(unchanged)['ready']
    regenerated = _generate(workspace)
    _compiled(regenerated)
    assert readiness(regenerated)['ready']


def test_scientific_digest_is_stable_under_normalization():
    workspace = _ready_workspace()
    digest = scientific_dependency_digest(workspace)
    for _ in range(3):
        workspace.update(normalize_program_fields(workspace))
        assert scientific_dependency_digest(workspace) == digest
        assert readiness(workspace)['ready']
    workspace['hypothesis'] = '  ' + workspace['hypothesis'] + '  '
    workspace['protocol']['baseline'] = '  Specified  '
    assert scientific_dependency_digest(workspace) == digest


@pytest.mark.parametrize('change', ['protocol', 'claim', 'evidence'])
def test_snapshot_covers_protocol_claims_and_supporting_evidence(change):
    workspace = _ready_workspace()
    changed = copy.deepcopy(workspace)
    if change == 'protocol':
        changed['protocol']['evaluation_protocol'] = 'Revised evaluation'
    elif change == 'claim':
        changed['claims'][0]['text'] = 'Different assertion'
    else:
        changed['runs'][0]['tool_receipts'][0]['result_digest'] = 'different'
    assert scientific_dependency_digest(changed) != scientific_dependency_digest(workspace)
    assert not readiness(changed)['published_current']
