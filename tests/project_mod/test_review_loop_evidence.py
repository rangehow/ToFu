"""Review certification rechecks durable file facts, not just stored verdict text."""
from copy import deepcopy

import pytest

from lib.conversations import review_loops
from tests.memory.test_working_file_sources import read_source, save
from tests.memory.test_working_facts import (
    artifact as artifact, facts as facts, storage as storage, turn_service as turn_service,
    PRINCIPAL, owner, query, record, dependency,
)
from tests.project_mod.test_review_loops import Ports, _loop

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('case', ['current', 'missing_file', 'changed_file', 'replaced_root',
                                  'derived', 'owner_asserted', 'wrong_revision', 'conflicting',
                                  'empty', 'no_certificate'])
def test_plain_report_rechecks_durable_observation_without_weakening_gates(
        facts, tmp_path, storage, case):
    root = tmp_path / 'reviewer'
    root.mkdir()
    task, _, source = read_source(facts, root)
    fact = save(facts, task, source)
    assert fact['status'] == 'observed'
    task.pop('_working_file_sources')
    storage.stop()
    storage.start()
    assert query(facts)['facts'][0]['status'] == 'observed'
    certificate = f'REVIEW_FACT: {fact["id"]}@{fact["revision"]}'
    if case == 'missing_file':
        (root / 'basis.txt').unlink()
    elif case == 'changed_file':
        (root / 'basis.txt').write_text('Budget: 200\n')
    elif case == 'replaced_root':
        root.rename(tmp_path / 'old-reviewer')
        root.mkdir()
        (root / 'basis.txt').write_text('Budget: 100\nRegion: east\n')
    elif case == 'derived':
        fact = record(facts, text='Budget agrees', kind='derived', evidence=[dependency(fact)])['facts'][0]
        certificate = f'REVIEW_FACT: {fact["id"]}@{fact["revision"]}'
    elif case == 'owner_asserted':
        fact = owner(facts, fact, 'confirm')['facts'][0]
        certificate = f'REVIEW_FACT: {fact["id"]}@{fact["revision"]}'
    elif case == 'wrong_revision':
        certificate = f'REVIEW_FACT: {fact["id"]}@2'
    elif case == 'conflicting':
        certificate += f'\nREVIEW_FACT: {fact["id"]}@2'
    elif case == 'no_certificate':
        certificate = ''
    report = f'Inspected the source.\n{certificate}\nREVIEW_VERDICT: pass'
    if case == 'empty':
        report = ''
    stored_report = deepcopy(report)
    loop, ports = _loop(('references',)), Ports()
    real = review_loops._Ports({}, {'user_id': PRINCIPAL.owner_user_id,
        'project_path': str(root), 'conversation_id': 'lead'})
    ports.evidence_current = lambda view, bindings: real.evidence_current(
        {'request': {'recipientId': facts['conv_id']}}, bindings)
    ports.settle('author', 'done')
    review_loops.advance(loop, ports)
    ports.settle('reviewer-1', report)
    review_loops.advance(loop, ports)
    assert ports.views['reviewer-1']['execution']['result']['content'] == stored_report
    assert not ports.follow_ups
    assert ports.accepted == ['reviewer-1']
    if case == 'current':
        assert loop['outcome']['status'] == 'passed'
        assert loop['rounds'][0]['reviewers'][0]['verdict'] == 'pass'
    else:
        assert loop['outcome']['status'] == 'impasse'
        assert loop['rounds'][0]['reviewers'][0]['verdict'] == 'missing'
        if case == 'empty':
            assert 'without a REVIEW_VERDICT line' in loop['outcome']['reason']
        elif case == 'no_certificate':
            assert 'pass lacks a REVIEW_FACT binding' in loop['outcome']['reason']
        else:
            assert 'pass lacks verified current source evidence' in loop['outcome']['reason']
            assert 'without a REVIEW_VERDICT line' not in loop['outcome']['reason']
