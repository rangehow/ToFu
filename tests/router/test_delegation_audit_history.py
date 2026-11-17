"""Delegated-action audit records are never silently dropped (docs/ROUTER.md R9, Audit).

Observed 2026-11-18: the approval document keeps only the newest 50
``delegatedActions``; with one ``lead_authority`` record per lead Attempt, older
records fell off and only rotated ``audit.log`` lines remained. Now a record
leaves the document only after it is appended (fsync) to the durable history
beside it, and ``delegated_action_history`` answers queries over both.
"""
from __future__ import annotations

import json

import pytest

from lib import lifecycle_approval as gate
from lib.router.contract import validate

pytestmark = pytest.mark.unit

OWNER = 7
ROUTER = {'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None}
LEAD = {'kind': 'project_lead', 'conversationId': 'lead-a', 'bindingRevision': 3, 'tenant': None}
HUMAN = {'kind': 'human-ui', 'ownerUserId': OWNER, 'subject': 'session-key', 'credential': 'session',
         'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}
TRIGGER = {'kind': 'owner_delegation', 'turnId': 't', 'text': 'delegated to every lead'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    clock = iter(range(1_000, 10_000))
    monkeypatch.setattr(gate, '_now', lambda: float(next(clock)))
    gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=HUMAN)
    return tmp_path


def _act(requester, number, scope='lead_authority'):
    record, _ = gate.begin_delegated_action(
        OWNER, {**requester, 'turnId': f'turn-{number}', 'attemptId': f'attempt-{number}'}, scope=scope,
        key=f'k{number}', reason=f'action {number}', trigger=TRIGGER,
        target={'kind': 'project_request', 'conversationId': requester['conversationId']})
    return record


def test_records_beyond_the_document_bound_move_to_the_durable_history(approvals):
    created = [_act(LEAD if number % 2 else ROUTER, number) for number in range(120)]
    gate.settle_delegated_action(OWNER, created[-1]['id'], status='completed')
    assert len(gate.delegated_actions(OWNER)) == gate._KEEP_RECORDS
    history = gate.delegated_action_history(OWNER)
    assert [record['id'] for record in history] == [record['id'] for record in reversed(created)]
    assert history[0]['status'] == 'completed', 'the retained copy is the newer state'
    for record in history:
        validate(record, 'ownerDelegatedAction')
    archived = (approvals / 'lifecycle_approvals.delegated-history.jsonl').read_text().splitlines()
    assert len(archived) == 120 - gate._KEEP_RECORDS
    # Filters: the acting lead, scope, status and a requestedAt lower bound.
    by_lead = gate.delegated_action_history(OWNER, requester='lead-a')
    assert len(by_lead) == 60 and all(item['requester']['conversationId'] == 'lead-a' for item in by_lead)
    assert gate.delegated_action_history(OWNER, scope='restart') == []
    assert [item['id'] for item in gate.delegated_action_history(OWNER, status='completed')] == [
        created[-1]['id']]
    since = created[10]['requestedAt']
    assert len(gate.delegated_action_history(OWNER, since=since)) == 110
    assert gate.delegated_action_history(OWNER + 1) == []


def test_a_failed_archive_drops_nothing(approvals, monkeypatch):
    created = [_act(LEAD, number) for number in range(gate._KEEP_RECORDS)]

    def unwritable(_records):
        raise OSError('disk full')
    monkeypatch.setattr(gate, '_archive_actions', unwritable)
    with pytest.raises(OSError):
        _act(LEAD, 999)
    retained = gate.delegated_actions(OWNER)
    assert {record['id'] for record in retained} == {record['id'] for record in created}


def test_an_unreadable_history_line_fails_the_read_instead_of_hiding_records(approvals):
    for number in range(gate._KEEP_RECORDS + 1):
        _act(LEAD, number)
    history_file = approvals / 'lifecycle_approvals.delegated-history.jsonl'
    history_file.write_text(history_file.read_text() + '{"truncated":\n')
    with pytest.raises(gate.DelegationRefused) as refused:
        gate.delegated_action_history(OWNER)
    assert refused.value.code == 'owner_delegation_unavailable'


def test_history_is_read_fresh_from_disk(approvals):
    for number in range(gate._KEEP_RECORDS + 5):
        _act(LEAD, number)
    lines = [json.loads(line) for line in
             (approvals / 'lifecycle_approvals.delegated-history.jsonl').read_text().splitlines()]
    assert [line['reason'] for line in lines] == [f'action {number}' for number in range(5)]
    assert all(line['owner'] == OWNER for line in lines)


def _lead_audit_rows(logs_dir):
    from lib.logging_pkg import lead_audit
    rows = []
    for path in sorted(lead_audit.stream_directory(logs_dir).glob('lead-audit-*.jsonl')):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row['kind'] == 'owner_delegation_action']


def test_lead_authority_actions_join_the_durable_lead_audit_stream(approvals, monkeypatch):
    """Global Router verification efb9d9dd: audit.log alone rotates away within about a day."""
    from lib.logging_pkg import lead_audit
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: approvals / 'logs')
    record, _ = gate.begin_delegated_action(
        OWNER, {**LEAD, 'turnId': 'turn-1', 'attemptId': 'attempt-1'}, scope='lead_authority', key='k1',
        reason='a wake acts on Workers', trigger={**TRIGGER, 'triggerTurnId': 'notice-1'},
        target={'kind': 'project_request', 'conversationId': 'lead-a'}, trigger_source='notification')
    gate.begin_delegated_action(  # a replay records nothing again
        OWNER, {**LEAD, 'turnId': 'turn-1', 'attemptId': 'attempt-1'}, scope='lead_authority', key='k1',
        reason='a wake acts on Workers', trigger=TRIGGER, target={'kind': 'project_request'})
    gate.settle_delegated_action(OWNER, record['id'], status='failed', code='dispatch_refused',
                                 detail='no worker root', action='delegate_project_work')
    accepted, settled = _lead_audit_rows(approvals / 'logs')
    for row in (accepted, settled):
        assert (row['actionId'], row['scope'], row['actor'], row['actorTurn'], row['grantRevision']) == (
            record['id'], 'lead_authority', 'lead-a', 'turn-1', record['grantRevision'])
        assert row['trigger']['kind'] == 'owner_delegation' and row['trigger']['turnId'] == 't'
        assert 'text' not in row['trigger']
    assert accepted['trigger'] == {'kind': 'owner_delegation', 'turnId': 't', 'triggerTurnId': 'notice-1',
                                   'source': 'notification'}
    assert (accepted['outcome'], accepted['action']) == ('accepted', 'project_request')
    assert (settled['outcome'], settled['action'], settled['code'], settled['detail']) == (
        'failed', 'delegate_project_work', 'dispatch_refused', 'no worker root')
    validate(gate.delegated_action(OWNER, record['id']), 'ownerDelegatedAction')


def test_a_long_target_stays_one_valid_audit_json_line_with_an_in_value_marker(approvals, monkeypatch):
    import lib.log as log_mod
    lines = []
    monkeypatch.setattr(log_mod, '_audit_write_line', lines.append)
    path = '/' + 'p' * 1999
    gate.begin_delegated_action(
        OWNER, {**LEAD, 'turnId': 'turn-1', 'attemptId': 'attempt-1'}, scope='lead_authority', key='long',
        reason='r' * 5000, trigger=TRIGGER,
        target={'kind': 'project_request', 'conversationId': 'lead-a', 'path': path})
    [line] = [line for line in lines if '"owner_delegation_action"' in line]
    assert line.endswith('\n') and line.count('\n') == 1
    entry = json.loads(line)
    assert entry['target']['kind'] == 'project_request' and entry['target']['conversationId'] == 'lead-a'
    # A 2,000-char target fits the value bound whole (it was cut at 300 serialized chars before).
    assert entry['target']['path'] == path
    assert 'log policy omitted' in entry['reason'] and entry['reason'].startswith('rrr')
    lines.clear()
    gate.begin_delegated_action(
        OWNER, {**LEAD, 'turnId': 'turn-2', 'attemptId': 'attempt-2'}, scope='lead_authority', key='longer',
        reason='r', trigger=TRIGGER, target={'kind': 'project_request', 'path': '/' + 'q' * 6000})
    [longer] = [json.loads(line) for line in lines if '"owner_delegation_action"' in line]
    assert longer['target']['kind'] == 'project_request'
    assert 'log policy omitted' in longer['target']['path'] and longer['target']['path'].startswith('/qqq')
    lines.clear()
    gate.settle_delegated_action(OWNER, gate.delegated_actions(OWNER)[0]['id'], status='failed', detail='d' * 9000)
    [settled] = [json.loads(line) for line in lines if '"owner_delegation_action_settled"' in line]
    assert 'log policy omitted' in settled['detail']
