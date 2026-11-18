"""Global Router broadcast reaches every lead and running Worker (docs/ROUTER.md R13).

Over a real storage authority (sqlite and native): a real Project Router binding
and a Worker it started under the owner's delegation. One
``broadcast_project_message`` lands in the Project Router's mailbox and the
running Worker's, tells the owning lead which of its Workers received it, is
recorded in audit.log, and a retry sends nothing twice.
"""
from __future__ import annotations

import pytest

from lib.conversations.project_messages import ProjectMailbox
from tests.router.test_lead_delegation import lead as lead
from tests.router.test_proactive_instruction_dispatch import _birth, _worker_factory
from tests.router.test_proactive_instruction_dispatch import routed as routed
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

__all__ = ('lead', 'routed', 'semantic_authority')


def _messages(owner, project, recipient):
    return ProjectMailbox(owner, project, recipient).page('')['messages']


def test_a_broadcast_reaches_every_lead_and_running_worker_once(lead, monkeypatch):
    from lib.conversations.coordinator_workers import delegate
    from lib.router.broadcast import broadcast_project_message
    from lib.router.tools import execute_router_tool
    audited = []
    monkeypatch.setattr('lib.router.broadcast.audit_log', lambda event, **fields: audited.append((event, fields)))
    lead.grant()
    outcome = delegate(lead.wake, lead.coordinator_scope,
                       {'title': 'Long run', 'brief': 'Keep running', 'idempotency_key': 'b1'},
                       create=_worker_factory(lead, []))
    birth = _birth(lead.wake, outcome)
    assert birth.failure is None, birth.failure
    worker = birth.result['conversation_id']
    arguments = {'targets': ['all'], 'content': 'Use `make test-deps`, not pip install.',
                 'reason': 'Three Workers hit the same missing dependency', 'idempotency_key': 'deps-1'}
    result = execute_router_tool(lead.task, lead.scope, 'broadcast_project_message', arguments)
    roles = sorted((item['role'], item['conversation_id']) for item in result['delivered'])
    coordinator = lead.target['conversationId']
    assert roles == [('lead', coordinator), ('lead_report', coordinator), ('worker', worker)], roles
    [to_worker] = _messages(lead.owner, lead.project, worker)
    assert to_worker['kind'] == 'steer' and 'make test-deps' in to_worker['content']
    assert to_worker['sender'] == lead.router['conversationId'] and to_worker['senderProject']
    to_lead = _messages(lead.owner, lead.project, coordinator)
    assert [message['kind'] for message in to_lead] == ['steer', 'report']
    assert worker in to_lead[1]['content'] and outcome['assignment_id'] in to_lead[1]['content']
    [(event, fields)] = audited
    assert event == 'router_broadcast' and (fields['leads'], fields['workers'], fields['projects']) == (1, 1, 1)
    # A retry with the same key sends nothing twice.
    broadcast_project_message(lead.task, lead.scope, arguments)
    assert len(_messages(lead.owner, lead.project, worker)) == 1
    assert len(_messages(lead.owner, lead.project, coordinator)) == 2
    # Leads only, and an unknown project is refused before anything is sent.
    leads_only = broadcast_project_message(lead.task, lead.scope, {
        **arguments, 'targets': [lead.project], 'include_workers': False, 'idempotency_key': 'deps-2'})
    assert [item['role'] for item in leads_only['delivered']] == ['lead']
    with pytest.raises(ValueError, match='not one of the owner'):
        broadcast_project_message(lead.task, lead.scope, {**arguments, 'targets': ['/nowhere'],
                                                          'idempotency_key': 'deps-3'})


def test_only_the_global_router_broadcasts(lead):
    from lib.router.tools import execute_router_tool
    with pytest.raises(PermissionError, match='Global Router'):
        execute_router_tool(lead.wake, lead.coordinator_scope, 'broadcast_project_message', {
            'targets': ['all'], 'content': 'x', 'reason': 'r', 'idempotency_key': 'k'})


def _live_router_task(lead):
    """The Global Router's own executing reply (a real Turn/Attempt of its conversation)."""
    from lib.identity import PrincipalContext
    from lib.turn_lifecycle import bind_task, get_attempt, list_turns, mark_task_started
    turns = list_turns(lead.router['conversationId'], user_id=lead.owner)['turns']
    reply = next(turn for turn in turns if turn.get('currentAttemptId')
                 and get_attempt(turn['currentAttemptId'], user_id=lead.owner)['status'] in ('pending', 'running'))
    attempt_id, task_id = reply['currentAttemptId'], 'router-live-task'
    bind_task(attempt_id, task_id, user_id=lead.owner)
    mark_task_started(attempt_id, task_id, user_id=lead.owner)
    return {'id': task_id, 'convId': lead.router['conversationId'], '_userId': lead.owner,
            '_turnId': reply['turnId'], '_attemptId': attempt_id,
            '_principalContext': PrincipalContext.user(subject_id=f'user:{lead.owner}',
                                                       owner_user_id=lead.owner, scopes=[]).to_payload(),
            'config': {'projectPath': lead.router['projectPath']}}


def test_lead_audit_4_exercise_router_controls_another_projects_worker_and_broadcasts(lead, record_property):
    """Lead audit stage 2 #4 ("live but not exercised"), run once on a scratch authority.

    A Project Router's wake starts a Worker in its own project under the owner's
    delegation. The Global Router, from its own executing reply, broadcasts to every
    lead and running Worker, then reads and stops that other project's Worker through
    control_conversation(owner_delegation). Every act is audited and nothing touches a
    real Worker or chat.
    """
    import json
    from lib import lifecycle_approval as gate
    from lib.conversations.coordinator_workers import delegate
    from lib.conversations.session_control import execute_session_tool
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.router.tools import execute_router_tool
    from lib.turn_lifecycle import get_attempt
    lead.grant()
    outcome = delegate(lead.wake, lead.coordinator_scope,
                       {'title': 'Scratch run', 'brief': 'Keep running', 'idempotency_key': 'x4'},
                       create=_worker_factory(lead, []))
    assert _birth(lead.wake, outcome).failure is None
    row = ProjectDispatchRepository(lead.owner, lead.project).read(outcome['assignment_id'])
    worker = {'conversation_id': row['request']['recipientId'], 'turn_id': row['execution']['turnId'],
              'attempt_id': row['execution']['attemptId']}
    router = _live_router_task(lead)

    broadcast = execute_router_tool(router, lead.scope, 'broadcast_project_message', {
        'targets': ['all'], 'content': 'Scratch exercise: pause before the shared AppId window.',
        'reason': 'Lead audit #4 exercise', 'idempotency_key': 'x4-broadcast'})
    roles = sorted(item['role'] for item in broadcast['delivered'])
    assert roles == ['lead', 'lead_report', 'worker'], broadcast
    [to_worker] = _messages(lead.owner, lead.project, worker['conversation_id'])
    assert to_worker['sender'] == lead.router['conversationId'] and to_worker['kind'] == 'steer'

    status = json.loads(execute_session_tool(router, lead.scope, 'get_conversation_status',
                                             {'conversation_id': worker['conversation_id']}))
    assert worker['turn_id'] in {turn['turnId'] for turn in status['turns']}, status
    stopped = json.loads(str(execute_session_tool(router, lead.scope, 'control_conversation', {
        **worker, 'action': 'stop', 'idempotency_key': 'x4-stop',
        'owner_delegation': {'reason': 'Lead audit #4 exercise', 'under_delegation': True}})))
    assert 'error' not in stopped, stopped
    assert get_attempt(worker['attempt_id'], user_id=lead.owner)['status'] not in ('pending', 'running')
    action = next(record for record in gate.delegated_actions(lead.owner)
                  if record['scope'] == 'conversation_control')
    assert (action['requester']['kind'], action['status'], action['target']['conversationId']) == (
        'global_router', 'completed', worker['conversation_id'])
    record_property('lead_audit_4', json.dumps({
        'broadcast_roles': roles, 'worker_project': lead.project,
        'router_home': lead.router['projectPath'], 'stop_action_id': stopped['owner_delegation_action_id'],
        'worker_attempt_status': get_attempt(worker['attempt_id'], user_id=lead.owner)['status']}))
