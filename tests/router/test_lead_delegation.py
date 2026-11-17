"""The owner's delegation reaches every lead (owner decision 2026-11-18, docs/ROUTER.md R9).

The owner: 「我的权限要全部下放给负责人们，也就是说，负责人的能力边界要尽可能扩展开。」
Observed before: a Project Router's commitment wake, a relayed report or a
Global Router notice Turn could read Workers but every delegation, follow_up and
cancel was refused 'Synthetic authorization origin denied' (METRICS M3: 14 of 30
Worker-start refusals). Under the owner's full grant such a Turn now acts on
Workers on the lead's own judgment: one ``lead_authority`` audit record per lead
Attempt, the live grant re-checked on every call and at every Worker birth, so a
revocation refuses at once. Over a real storage authority (sqlite and native):
real bindings, Turns, Attempts, ``source_for_task``, Worker birth and admission.
"""
from __future__ import annotations

import pytest

from lib import lifecycle_approval as gate
from tests.router.test_proactive_instruction_dispatch import _birth, _commitment_wake, _worker_factory
from tests.router.test_proactive_instruction_dispatch import routed as routed
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

__all__ = ('routed', 'semantic_authority')


def _human(owner):
    return {'kind': 'human-ui', 'ownerUserId': owner, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def lead(routed, tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    routed.grant = lambda: gate.set_delegation(routed.owner, True, grantee={
        'kind': 'global_router', 'conversationId': routed.router['conversationId'],
        'bindingRevision': routed.router['revision'], 'tenant': None}, provenance=_human(routed.owner))
    routed.revoke = lambda: gate.set_delegation(routed.owner, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': routed.owner, 'credential': 'header'})
    from lib.turn_lifecycle import get_attempt, get_turn
    notice = _commitment_wake(routed)
    reply = get_turn(routed.target['conversationId'], notice['turn']['turnId'], user_id=routed.owner)
    routed.wake = routed.coordinator_task(reply, get_attempt(reply['currentAttemptId'], user_id=routed.owner))
    routed.reply = reply
    return routed


def test_a_wake_turn_dispatches_a_worker_under_the_owner_delegation(lead):
    from lib.conversations.coordinator_workers import delegate
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import NonhumanRequest, ProjectDispatchRepository
    repository = ProjectDispatchRepository(lead.owner, lead.project)
    # Without the grant the wake has no request of its own (the old refusal, unchanged).
    with pytest.raises(NonhumanRequest, match='Act for me'):
        source_for_task(repository, lead.wake, delegated=True)
    lead.grant()
    # Callers that need the owner's own words never receive the delegation.
    with pytest.raises(NonhumanRequest):
        source_for_task(repository, lead.wake)
    created = []
    outcome = delegate(lead.wake, lead.coordinator_scope,
                       {'title': 'Fix the regression', 'brief': 'Reproduce, fix and verify', 'idempotency_key': 'w1'},
                       create=_worker_factory(lead, created))
    birth = _birth(lead.wake, outcome)
    assert birth.failure is None and birth.result['dispatch'] == 'accepted', birth.failure or birth.result
    assert created, 'the wake dispatched a Worker'
    row = repository.read(outcome['assignment_id'])
    source = row['request']['authorizationSource']
    assert source['turnId'] == lead.reply['turnId'] and source['projectionRevision'] == 1
    repository.validate(repository.admission(row['id']))
    [record] = gate.delegated_actions(lead.owner)
    assert record['id'] == source['ownerDelegation']
    # The Worker's birth settled the record with its outcome (verification efb9d9dd: it stayed 'accepted').
    assert (record['scope'], record['status'], record['trigger']['kind']) == (
        'lead_authority', 'completed', 'owner_delegation')
    assert record['trigger']['triggerTurnId'] == lead.reply['parentTurnId']
    assert record['requester']['kind'] == 'project_lead'
    assert record['requester']['conversationId'] == lead.target['conversationId']
    assert record['target'] == {'kind': 'project_request', 'conversationId': lead.target['conversationId'],
                                'turnId': lead.reply['turnId'], 'attemptId': lead.wake['_attemptId'],
                                'path': lead.project}
    from lib.router.contract import validate
    validate(record, 'ownerDelegatedAction')
    # A second Worker of the same Turn reuses its one audit record.
    second = delegate(lead.wake, lead.coordinator_scope,
                      {'title': 'Docs', 'brief': 'Update the docs', 'idempotency_key': 'w2'},
                      create=_worker_factory(lead, created))
    assert _birth(lead.wake, second).failure is None
    assert len(gate.delegated_actions(lead.owner)) == 1


def test_a_revoked_delegation_refuses_the_next_dispatch_at_once(lead):
    from lib.conversations.coordinator_workers import delegate
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import NonhumanRequest, ProjectDispatchRepository
    repository = ProjectDispatchRepository(lead.owner, lead.project)
    lead.grant()
    _, source, _ = source_for_task(repository, lead.wake, delegated=True)
    assert 'ownerDelegation' in source
    lead.revoke()
    with pytest.raises(NonhumanRequest):
        source_for_task(repository, lead.wake, delegated=True)
    created = []
    outcome = delegate(lead.wake, lead.coordinator_scope,
                       {'title': 'Refused', 'brief': 'Must not start', 'idempotency_key': 'r1'},
                       create=_worker_factory(lead, created))
    birth = _birth(lead.wake, outcome)
    assert birth.failure and 'no request of its own' in birth.failure['error'], birth.failure
    assert not created and outcome['assignment_id'] not in {row['id'] for row in repository.list()}
    # A source recorded before the revocation no longer admits a birth either.
    with pytest.raises(Exception, match='no longer covers'):
        repository.validate_human_source(source)


def test_an_enumerated_grant_names_only_the_global_router(lead):
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import NonhumanRequest, ProjectDispatchRepository
    lead.grant()
    import json
    path = gate._APPROVALS_FILE
    document = json.loads(open(path).read())
    for record in document['delegations'].values():
        record.update(coverage='enumerated', scopes=['restart', 'lead_authority'])
    open(path, 'w').write(json.dumps(document))
    with pytest.raises(NonhumanRequest):
        source_for_task(ProjectDispatchRepository(lead.owner, lead.project), lead.wake, delegated=True)


def test_a_failed_lead_act_settles_its_record_failed_and_history_shows_the_final_state(lead, tmp_path,
                                                                                         monkeypatch):
    """Global Router verification efb9d9dd: lead_authority records stayed 'accepted' forever."""
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.coordinator_workers import delegate
    from lib.logging_pkg import lead_audit
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path / 'logs')
    lead.grant()

    def broken(_scope, _arguments):
        raise RuntimeError('worker conversation store unavailable')
    failed = delegate(lead.wake, lead.coordinator_scope,
                      {'title': 'Fails', 'brief': 'Cannot start', 'idempotency_key': 'f1'}, create=broken)
    assert _birth(lead.wake, failed).failure
    [record] = gate.delegated_action_history(lead.owner, scope='lead_authority')
    assert (record['status'], record['code']) == ('failed', 'RuntimeError'), record
    assert 'worker conversation store unavailable' in record['detail']
    # A failed act of the Attempt is no revocation: the next Worker of the same Turn is born.
    created = []
    born = delegate(lead.wake, lead.coordinator_scope,
                    {'title': 'Works', 'brief': 'Starts', 'idempotency_key': 'f2'},
                    create=_worker_factory(lead, created))
    assert _birth(lead.wake, born).failure is None and created
    assert gate.delegated_action(lead.owner, record['id'])['status'] == 'completed'
    # A failed tool call through the ordinary Coordinator tool path settles it failed again.
    result = execute_coordinator_tool(lead.wake, 'review_project_work', {
        'assignment_id': born['assignment_id'], 'decision': 'accept', 'note': 'no result yet',
        'result_revision': 99})
    from lib.tools.result_value import tool_result_failure_verdict
    assert tool_result_failure_verdict(result), result
    [final] = gate.delegated_action_history(lead.owner, scope='lead_authority')
    assert final['id'] == record['id'] and final['status'] == 'failed', final
    rows = [row for path in sorted(lead_audit.stream_directory(tmp_path / 'logs').glob('*.jsonl'))
            for row in map(__import__('json').loads, path.read_text().splitlines())
            if row['kind'] == 'owner_delegation_action']
    assert [(row['action'], row['outcome']) for row in rows] == [
        ('project_request', 'accepted'), ('delegate_project_work', 'failed'),
        ('delegate_project_work', 'completed'), ('review_project_work', 'failed')]
    assert {row['actor'] for row in rows} == {lead.target['conversationId']}
    assert all(row['grantRevision'] == record['grantRevision'] for row in rows)
