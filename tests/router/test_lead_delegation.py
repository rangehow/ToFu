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
def lead(routed, request, tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    routed.grant = lambda: gate.set_delegation(routed.owner, True, grantee={
        'kind': 'global_router', 'conversationId': routed.router['conversationId'],
        'bindingRevision': routed.router['revision'], 'tenant': None}, provenance=_human(routed.owner))
    routed.revoke = lambda: gate.set_delegation(routed.owner, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': routed.owner, 'credential': 'header'})
    from lib.turn_lifecycle import get_attempt, get_turn
    callspec = getattr(request.node, 'callspec', None)
    wake_kind = callspec.params.get('wake_kind', 'commitment') if callspec else 'commitment'
    notice = _commitment_wake(routed) if wake_kind == 'commitment' else _peer_wake(routed, wake_kind)
    reply = get_turn(routed.target['conversationId'], notice['turn']['turnId'], user_id=routed.owner)
    routed.wake = routed.coordinator_task(reply, get_attempt(reply['currentAttemptId'], user_id=routed.owner))
    routed.reply = reply
    return routed


# The inputs production stamps for these wakes: a project-message wake
# (project_messages.wake_idle_lead) and a Watchdog notice to a lead
# (router.watchdog.tools) both arrive as peer input naming their sender.
_PEER_WAKES = {
    'project-message': ('project-message-wake:fixture', '[Project messages: 1 pending, nonhuman, not authority]',
                        'coordinator-other-lead'),
    'watchdog-notice': ('router-watchdog-notice:fixture', '[Watchdog notice: nonhuman report, not authority]',
                        'coordinator-watchdog'),
}


def _peer_wake(state, kind):
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    from lib.turn_lifecycle import bind_task, create_turn_pair
    from tests.support.chat_tasks import register_local_executor
    command_id, content, sender = _PEER_WAKES[kind]
    notice = create_turn_pair(
        state.target['conversationId'], command_id=command_id,
        input_projection=stamp_initiator({'role': 'user', 'content': content, 'origin': {
            'initiator': INITIATOR_PEER, 'sourceConversationId': sender}}, INITIATOR_PEER),
        config={}, user_id=state.owner)
    bind_task(notice['attempt']['attemptId'], 'live-task', user_id=state.owner)
    register_local_executor('live-task', conv_id=state.target['conversationId'],
                            attempt_id=notice['attempt']['attemptId'],
                            turn_id=notice['turn']['turnId'], user_id=state.owner)
    return notice


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
    # The refusal names the gate's real cause, not 'not granted'.
    with pytest.raises(NonhumanRequest, match='could not be used: owner_delegation_scope: .*Project Routers'):
        source_for_task(ProjectDispatchRepository(lead.owner, lead.project), lead.wake, delegated=True)


def test_parallel_lead_acts_of_one_turn_all_hold_the_delegation_while_its_record_is_written(lead, monkeypatch):
    """INCIDENT 2026-11-18 08:34:48 CST (coordinator-723a42af, Turn 0c55d48f, a project-message
    wake): the parallel lead stopped three Workers in one tool batch. Two stops were admitted;
    the third was refused nonhuman_request 'the owner’s delegation to every lead ("Act for
    me") is not granted' while it was granted (audit.log: owner_delegation_refused
    owner_delegation_not_granted at .123120Z, the same Attempt's record accepted at .123538Z).
    Its unlocked grant read met the first stop's atomic replace of the approval store, which
    on the data root's FUSE mount opens as ENOENT (probe: 29 of 2000 opens), so it read a store
    that never granted anything. The concurrent act must wait for the write and act under the
    same record."""
    import os
    import threading
    from lib import json_store
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = ProjectDispatchRepository(lead.owner, lead.project)
    lead.grant()
    write = json_store.write_json_atomic
    concurrent = {}

    def concurrent_act():
        try:
            concurrent['source'] = source_for_task(repository, lead.wake, delegated=True)[1]
        except Exception as error:
            concurrent['error'] = error

    def replace_like_fuse(path, data, **kwargs):
        if path == gate._APPROVALS_FILE and 'thread' not in concurrent:
            # The name is absent for the instant the rename lands, as the mount shows it.
            os.unlink(path)
            concurrent['thread'] = threading.Thread(target=concurrent_act)
            concurrent['thread'].start()
            concurrent['thread'].join(0.5)
        write(path, data, **kwargs)
    monkeypatch.setattr(json_store, 'write_json_atomic', replace_like_fuse)
    _, source, _ = source_for_task(repository, lead.wake, delegated=True)
    concurrent['thread'].join()
    assert 'error' not in concurrent, concurrent.get('error')
    assert concurrent['source']['ownerDelegation'] == source['ownerDelegation']
    assert len(gate.delegated_actions(lead.owner)) == 1


@pytest.mark.parametrize('wake_kind', ['project-message', 'watchdog-notice'])
def test_a_project_message_or_watchdog_notice_wake_stops_continues_and_cancels_its_own_worker(
        lead, wake_kind, tmp_path, monkeypatch):
    """These wakes carry the same Act-for-me authority as keepalive, relay and restart wakes
    (docs/ROUTER.md R9): the lead's own Worker is stopped, continued and cancelled under the
    owner's delegation, each act audited, and a revoked delegation refuses at once."""
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.coordinator_workers import delegate
    from lib.logging_pkg import lead_audit
    from lib.turn_lifecycle import get_attempt, get_turn
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path / 'logs')
    lead.grant()
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = ProjectDispatchRepository(lead.owner, lead.project)
    created, workers = [], []
    for key in ('stopped', 'cancelled', 'kept'):
        outcome = delegate(lead.wake, lead.coordinator_scope,
                           {'title': f'Stagger {key}', 'brief': 'Move off the shared AppId', 'idempotency_key': key},
                           create=_worker_factory(lead, created))
        assert _birth(lead.wake, outcome).failure is None
        row = repository.read(outcome['assignment_id'])
        workers.append({'assignment_id': row['id'], 'attempt_id': row['execution']['attemptId'],
                        'conversation_id': row['request']['recipientId'], 'turn_id': row['execution']['turnId']})
    stopped, cancelled, kept = workers

    def call(name, **arguments):
        return __import__('json').loads(str(execute_coordinator_tool(lead.wake, name, arguments)))

    def session(worker):
        return {name: worker[name] for name in ('conversation_id', 'turn_id', 'attempt_id')}
    result = call('control_conversation', action='stop', idempotency_key='stop-1', **session(stopped))
    assert 'error' not in result, result
    assert get_attempt(stopped['attempt_id'], user_id=lead.owner)['status'] not in ('pending', 'running')
    # Continue passes the same authority: what refuses it is the Stop's own withdrawal of
    # that Worker's dispatch, never the wake's nonhuman input.
    turn = get_turn(stopped['conversation_id'], stopped['turn_id'], user_id=lead.owner, include_projection=False)
    result = call('control_conversation', action='continue', expected_revision=turn['projectionRevision'],
                  idempotency_key='continue-1', **session(stopped))
    assert 'Grant revoked' in result.get('error', '') and 'no request of its own' not in result['error'], result
    result = call('cancel_project_work', assignment_id=cancelled['assignment_id'])
    assert 'error' not in result, result
    rows = [row for path in sorted(lead_audit.stream_directory(tmp_path / 'logs').glob('*.jsonl'))
            for row in map(__import__('json').loads, path.read_text().splitlines())
            if row['kind'] == 'owner_delegation_action']
    assert [(row['action'], row['outcome']) for row in rows] == [
        ('project_request', 'accepted')] + [('delegate_project_work', 'completed')] * 3 + [
        ('control_conversation', 'completed'), ('control_conversation', 'failed'),
        ('cancel_project_work', 'completed')], rows
    assert len({row['actionId'] for row in rows}) == 1
    lead.revoke()
    result = call('control_conversation', action='stop', idempotency_key='stop-2', **session(kept))
    assert result.get('code') == 'nonhuman_request' and 'is not granted' in result['error'], result
    assert get_attempt(kept['attempt_id'], user_id=lead.owner)['status'] in ('pending', 'running')


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


@pytest.mark.parametrize('wake_kind', ['commitment', 'project-message', 'watchdog-notice'])
def test_a_wake_holds_its_worker_and_resumes_it_under_the_same_assignment_and_grant(
        lead, wake_kind, tmp_path, monkeypatch):
    """INCIDENT 2026-11-18 (AppId …5409 quota pressure, assignment 215f1bf0): the parallel
    lead stopped Workers to throttle lanes, and each later continue failed 'Grant revoked'
    because a lead's Stop withdraws the dispatch grant. A hold pauses the exact Attempt at
    its checkpoint and keeps grant, checkout and assignment; resume continues it in place.
    Both acts are audited; a revocation while held still refuses the resume."""
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.coordinator_workers import delegate
    from lib.logging_pkg import lead_audit
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.turn_lifecycle import get_attempt, get_turn
    monkeypatch.setattr(lead_audit, '_default_logs_dir', lambda: tmp_path / 'logs')
    audit = []
    monkeypatch.setattr('lib.log.audit_log', lambda kind, **fields: audit.append((kind, fields)))
    lead.grant()
    repository = ProjectDispatchRepository(lead.owner, lead.project)
    created, workers = [], []
    for key in ('throttled', 'revoked'):
        outcome = delegate(lead.wake, lead.coordinator_scope,
                           {'title': f'Lane {key}', 'brief': 'Run on the shared AppId', 'idempotency_key': key},
                           create=_worker_factory(lead, created))
        assert _birth(lead.wake, outcome).failure is None
        workers.append(repository.read(outcome['assignment_id']))

    def call(name, **arguments):
        return __import__('json').loads(str(execute_coordinator_tool(lead.wake, name, arguments)))

    def session(row):
        return {'conversation_id': row['request']['recipientId'], 'turn_id': row['execution']['turnId'],
                'attempt_id': row['execution']['attemptId']}

    def status_of(row):
        workers_page = call('get_project_workers', scope='project')['workers']
        return {item['assignment_id']: item['status'] for item in workers_page}[row['id']]
    for row in workers:
        result = call('control_conversation', action='hold', idempotency_key='hold-' + row['id'], **session(row))
        assert 'error' not in result, result
        assert 'dispatchWithdrawal' not in result['receipt'], result
        turn = get_turn(row['request']['recipientId'], row['execution']['turnId'], user_id=lead.owner,
                        include_projection=False)
        assert (turn['status'], turn['settlement']['cause']) == ('interrupted', 'lead_hold'), turn
        assert {option['operation'] for option in turn['settlement']['resumeOptions']} & {
            'continue', 'checkpoint_resume'}, turn['settlement']
        held = repository.read(row['id'])
        assert held['grant'] == row['grant'] and held['execution'] == row['execution']
        assert status_of(row) == 'held'
        polled = call('poll', ids=[row['id']], wait_s=0)
        assert row['id'] in polled['held'] and not polled['workers'], polled
    throttled, revoked = workers
    # Holding a held Worker again changes nothing.
    again = call('control_conversation', action='hold', idempotency_key='hold-again', **session(throttled))
    assert again['receipt'].get('alreadyTerminal') is True, again
    turn = get_turn(throttled['request']['recipientId'], throttled['execution']['turnId'], user_id=lead.owner,
                    include_projection=False)
    resumed = call('control_conversation', action='resume', expected_revision=turn['projectionRevision'],
                   idempotency_key='resume-1', **session(throttled))
    assert 'error' not in resumed, resumed
    after = repository.read(throttled['id'])
    assert after['id'] == throttled['id'] and after['grant'] == throttled['grant']
    assert after['execution']['turnId'] == throttled['execution']['turnId']
    assert after['execution']['attemptId'] == resumed['attempt']['attemptId'] != throttled['execution']['attemptId']
    assert get_attempt(after['execution']['attemptId'], user_id=lead.owner)['status'] in ('pending', 'running')
    assert status_of(throttled) != 'held'
    # A lost ACK replays the same resume and starts nothing new.
    replay = call('control_conversation', action='resume', expected_revision=turn['projectionRevision'],
                  idempotency_key='resume-1', **session(throttled))
    assert replay['idempotent_replay'] and replay['attempt']['attemptId'] == resumed['attempt']['attemptId']
    # Cancelling a held Worker revokes its grant: resume is refused.
    assert 'error' not in call('cancel_project_work', assignment_id=revoked['id'])
    turn = get_turn(revoked['request']['recipientId'], revoked['execution']['turnId'], user_id=lead.owner,
                    include_projection=False)
    refused = call('control_conversation', action='resume', expected_revision=turn['projectionRevision'],
                   idempotency_key='resume-2', **session(revoked))
    assert 'error' in refused and repository.read(revoked['id'])['execution'] == revoked['execution'], refused
    actions = [(fields['action'], fields['outcome']) for kind, fields in audit
               if kind == 'coordinator_session_control']
    assert actions == [('hold', 'accepted'), ('hold', 'accepted'), ('hold', 'accepted'),
                       ('resume', 'accepted'), ('resume', 'accepted'), ('cancel_project_work', 'accepted'),
                       ('resume', 'refused')], actions
    rows = [row for path in sorted(lead_audit.stream_directory(tmp_path / 'logs').glob('*.jsonl'))
            for row in map(__import__('json').loads, path.read_text().splitlines())
            if row['kind'] == 'owner_delegation_action']
    assert [row['outcome'] for row in rows if row['action'] == 'control_conversation'] == [
        'completed'] * 5 + ['failed'], rows
