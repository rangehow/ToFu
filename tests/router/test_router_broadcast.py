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
