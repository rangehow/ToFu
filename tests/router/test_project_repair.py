"""The Global Router repairs a project whose Project Router cannot run (ROUTER.md R9 ``project_repair``).

Owner decision 2026-11-12: the tofu Project Router failed every Turn for hours and only the owner could
open a conversation in the project to fix it. Under the owner's delegation the Router now opens that
conversation itself, with the owner's own words as its request and its brief as nonhuman evidence, but
only while the project's Router cannot run; it hears the repair's outcome once.
"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib import lifecycle_approval as gate
from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.identity import PrincipalContext
from lib.lifecycle_approval import DelegationRefused
from lib.router import project_repair
from lib.router.owner_delegation import _live_router_attempt as LIVE_ATTEMPT
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.storage.errors import StorageError
from tests.router.test_delegated_action import approvals, delegation as delegation  # noqa: F401  (fixtures)
from tests.router.test_standing_instructions import OWNER, r as standing  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

OWNER_WORDS = '以后这个负责人挂了，你自己不能修复吗？'
NOTE = 'coordinator-project fails every Turn: archive create failed (database_protocol_error). Fix and verify.'


def _turn(turn_id, status, *, actor='assistant', cause=None, kind=None):
    return {'turnId': turn_id, 'actor': actor, 'status': status, 'kind': kind,
            'settlement': {'cause': cause} if cause else {}}


@pytest.fixture
def repair(delegation, monkeypatch):
    """The owner's Router in a genuine owner request; the project's lead lane, storage and conversations."""
    s = delegation
    s.turns['owner-turn']['projection'] = {'content': OWNER_WORDS, 'originalContent': OWNER_WORDS}
    # The lead's main lane, newest first: (Turn, its current Attempt).
    s.lane = [(_turn('failed-reply', 'failed', cause='generation_error'), {})]
    lanes = []

    def lane_turns_newest_first(conversation_id, lane_id='main', *, user_id):
        lanes.append((conversation_id, lane_id, user_id))
        yield from deepcopy(s.lane)
    monkeypatch.setattr('lib.turn_lifecycle.lane_turns_newest_first', lane_turns_newest_first)
    s.lanes = lanes
    s.conversations = {'coordinator-project': {'settings': {'folderId': 'folder-tofu', 'workerModel': 'worker-model',
                                                            'model': 'lead-model'}}}
    s.created = []
    records_command = s.records.command

    def command(operation, payload, command_id=None, **kwargs):
        if operation != 'conversation.create':
            return records_command(operation, payload, command_id, **kwargs)
        s.created.append({'payload': deepcopy(payload), 'command_id': command_id})
        if payload['conv_id'] in s.conversations:
            raise StorageError('database_conflict', 'conversation exists')
        s.conversations[payload['conv_id']] = {'settings': deepcopy(payload['settings'])}
    s.records.command = command

    class Conversations:
        def __init__(self, client=None):
            pass

        def get(self, conversation_id, *, user_id, include_messages=False):
            assert user_id == OWNER and include_messages is False
            row = s.conversations.get(conversation_id)
            return None if row is None else SimpleNamespace(metadata=deepcopy(row))
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository', Conversations)
    s.task['config'] = {'codeExecEnabled': True, 'searchMode': 'multi', 'chatMode': 'chat'}
    def call(**arguments):
        payload = {'project_path': s.project, 'title': 'Repair the stuck Project Router', 'note': NOTE,
                   'reason': 'The owner asked me to fix a stuck Project Router myself',
                   'source_turn_id': 'owner-turn', 'idempotency_key': 'repair-1', **arguments}
        return project_repair.repair_project(s.task, s.scope, {name: value for name, value in payload.items()
                                                               if value is not None})
    s.repair = call
    return s


def test_a_project_whose_lead_failed_gets_one_repair_conversation_with_the_owner_words(repair):
    repair.grant()
    # Input Turns, an appended report and a queued pair after the failed reply never ran on the lane.
    repair.lane = [(_turn('report', 'completed', cause='ingested', kind='ingested'), {}),
                   (_turn('queued-input', 'completed', actor='human'), {}),
                   (_turn('queued-reply', 'pending'), {'queueBinding': {'queueId': 'q'}}),
                   (_turn('owner-input', 'completed', actor='human'), {}),
                   *repair.lane]
    result = repair.repair()

    conversation_id = result['conversation_id']
    assert conversation_id.startswith('repair-') and result['execution_started'] is True
    assert result['lead']['can_run'] is False and result['lead']['turn_id'] == 'failed-reply'
    assert result['lead']['coordinator_conversation_id'] == 'coordinator-project'
    [created] = repair.created
    assert created['payload']['conv_id'] == conversation_id
    assert created['payload']['title'] == 'Repair the stuck Project Router'
    settings = created['payload']['settings']
    # The project's folder, root and Worker model; the Router request's capability switches only.
    assert settings['projectPath'] == repair.project and settings['folderId'] == 'folder-tofu'
    assert settings['model'] == 'worker-model'
    assert settings['codeExecEnabled'] is True and settings['searchMode'] == 'multi'
    assert 'chatMode' not in settings and '_coordinatorSource' not in settings

    [call] = repair.calls
    assert call['conversation'] == conversation_id and call['input_actor'] == 'human'
    message = call['message']
    assert message['originalContent'] == OWNER_WORDS
    assert message['content'].startswith(OWNER_WORDS + '\n\n[Global Router context: nonhuman')
    assert NOTE in message['content']
    assert message['origin'] == {'initiator': 'human',
                                 'forwardedFrom': {'conversationId': 'router', 'turnId': 'owner-turn'}}
    assert call['config_overrides'] == {project_repair.REPAIR_MARKER: {
        'actionId': result['action_id'], 'routerConversationId': 'router', 'projectPath': repair.project}}

    [record] = repair.audit()
    assert (record['scope'], record['status'], record['id']) == ('project_repair', 'completed', result['action_id'])
    assert record['target'] == {'kind': 'project', 'path': repair.project, 'action': 'repair',
                                'conversationId': conversation_id}
    assert record['trigger']['kind'] == 'owner_message' and record['trigger']['text'] == OWNER_WORDS

    # A retry with the same key opens nothing again.
    again = repair.repair()
    assert again['replayed'] is True and again['conversation_id'] == conversation_id
    assert len(repair.calls) == 1 and len(repair.created) == 1


@pytest.mark.parametrize('newest', [
    pytest.param(None, id='never-worked'),
    pytest.param(_turn('reply', 'running'), id='running'),
    pytest.param(_turn('reply', 'completed', cause='provider_finished'), id='completed'),
    pytest.param(_turn('reply', 'truncated', cause='length'), id='truncated'),
    pytest.param(_turn('reply', 'interrupted', cause='user_abort'), id='stopped'),
    pytest.param(_turn('reply', 'interrupted', cause='server_restart'), id='restarted'),
    pytest.param(_turn('reply', 'failed', cause='dispatch_grant_revoked'), id='grant-withdrawn'),
])
def test_a_project_router_that_can_run_is_not_repaired(repair, newest):
    repair.grant()
    repair.lane = [] if newest is None else [(newest, {})]
    with pytest.raises(DelegationRefused) as refused:
        repair.repair()
    assert refused.value.code == 'project_lead_can_run'
    assert 'route_to_project' in str(refused.value)
    assert repair.created == [] and repair.calls == []
    [record] = repair.audit()
    assert (record['status'], record['code'], record['scope']) == ('refused', 'project_lead_can_run',
                                                                   'project_repair')


def test_a_project_without_an_available_router_cannot_run_one(repair):
    repair.unbound.add(repair.project)
    state = project_repair.lead_state(OWNER, repair.project)
    assert state['can_run'] is False and state['availability'] == 'unbound'
    assert repair.lanes == []


def test_without_the_grant_nothing_is_opened_or_recorded(repair):
    with pytest.raises(DelegationRefused) as refused:
        repair.repair()
    assert refused.value.code == 'owner_delegation_not_granted'
    assert repair.created == [] and repair.calls == [] and repair.audit() == []


def test_without_an_owner_trigger_the_refusal_is_recorded_and_nothing_is_opened(repair):
    repair.grant()
    with pytest.raises(DelegationRefused) as refused:
        project_repair.repair_project(repair.task, repair.scope, {
            'project_path': repair.project, 'title': 'Repair', 'note': NOTE, 'reason': 'Lead is stuck',
            'idempotency_key': 'no-trigger'})
    assert refused.value.code == 'owner_delegation_trigger_missing'
    assert repair.created == [] and repair.calls == []
    assert [record['code'] for record in repair.audit()] == ['owner_delegation_trigger_missing']


def test_a_standing_instruction_repairs_only_its_own_project(repair, tmp_path):
    repair.grant()
    instruction = repair.create()  # scoped to repair.project
    other = tmp_path / 'other-project'
    other.mkdir()
    with pytest.raises(DelegationRefused) as refused:
        project_repair.repair_project(repair.task, repair.scope, {
            'project_path': str(other), 'title': 'Repair', 'note': NOTE, 'reason': 'Lead is stuck',
            'instruction_id': instruction['instructionId'], 'event_id': 'lead-failed',
            'idempotency_key': 'other'})
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    assert repair.calls == []

    result = repair.repair(source_turn_id=None, instruction_id=instruction['instructionId'],
                           event_id='lead-failed', idempotency_key='instructed')
    [call] = repair.calls
    assert call['message']['originalContent'] == instruction['text']
    assert call['message']['origin']['forwardedFrom'] == {
        'conversationId': 'router', 'turnId': 'owner-turn', 'instructionId': instruction['instructionId']}
    assert 'Triggering channel event: lead-failed.' in call['message']['content']
    assert result['status'] == 'completed'


def test_a_repair_turn_that_does_not_start_settles_failed(repair, monkeypatch):
    repair.grant()
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn',
                        lambda *args, **kwargs: ScheduledTurnDispatch('start_failed'))
    with pytest.raises(project_repair.RepairNotStarted):
        repair.repair()
    [record] = repair.audit()
    assert (record['status'], record['code']) == ('failed', 'project_repair_not_started')


def test_only_the_global_router_repairs(repair):
    repair.grant()
    scope = {**repair.scope, 'project_path': repair.project}
    with pytest.raises(PermissionError):
        project_repair.repair_project(repair.task, scope, {
            'project_path': repair.project, 'title': 'Repair', 'note': NOTE, 'reason': 'x',
            'source_turn_id': 'owner-turn', 'idempotency_key': 'k'})
    assert repair.calls == []


@pytest.fixture
def replacement(repair, monkeypatch):
    """Real coordination repository and live-attempt checks over isolated storage ports."""
    from lib import turn_lifecycle
    from lib.router import owner_delegation
    from lib.conversations import project_coordination, repository
    # The base repair fixture mocks binding reads; this lane exercises the actual CAS.
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', ProjectCoordinationRepository)
    bindings = ProjectCoordinationRepository(OWNER, conversations=repository.ConversationRepository())
    # The binding repository normally uses its imported repository class.
    monkeypatch.setattr(project_coordination, 'ConversationRepository', repository.ConversationRepository)
    monkeypatch.setattr(owner_delegation, '_live_router_attempt', LIVE_ATTEMPT)
    project = repair.project
    repair.conversations['coordinator-project']['settings']['projectPath'] = project
    repair.conversations['router'] = {'settings': {'projectPath': repair.scope['project_path']}}
    def seed(path, conversation_id):
        path, key = bindings._identity(path)
        repair.records.rows[(project_coordination.NAMESPACE, key)] = {
            'version': 1, 'value': {'user_id': OWNER, 'projectPath': path, 'conversationId': conversation_id}}
    seed(project, 'coordinator-project')
    seed(repair.scope['project_path'], 'router')
    repair.task.update(convId='coordinator-project', _turnId='lead-turn', _attemptId='lead-attempt')
    repair.scope = {**repair.scope, 'conversation_id': 'coordinator-project', 'project_path': project}
    repair.task['_coordinatorDispatchScope'] = repair.scope
    repair.attempt = {'status': 'running', 'taskId': repair.task['id'], 'attemptId': 'lead-attempt',
                      'turnId': 'lead-turn', 'conversationId': 'coordinator-project', 'createdAt': 1000}
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', lambda attempt_id, *, user_id: repair.attempt)
    monkeypatch.setattr(turn_lifecycle, 'get_turn', lambda conv, turn, **kw: {'conversationId': conv, 'turnId': turn})
    repair.folder = {'id': 'folder-tofu', 'projectPaths': [project]}
    monkeypatch.setattr('lib.conversations.folders.owner_folders', lambda: SimpleNamespace(
        get=lambda user, folder: repair.folder if user == OWNER and folder == 'folder-tofu' else None))
    repair.bindings = bindings
    repair.arguments = {'action': 'replace_own_binding', 'under_delegation': True,
                        'reason': 'The current lead binding needs replacement', 'idempotency_key': 'replace'}
    return repair


def test_running_project_lead_replaces_only_its_own_binding(replacement):
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.router.contract import validate
    s = replacement
    s.grant()
    result = json.loads(execute_coordinator_tool(s.task, 'repair_project', s.arguments))
    assert result['status'] == 'completed' and result['execution_started'] is False
    new = s.bindings.read(s.project)
    assert result['binding'] == new and new['revision'] == 2
    assert new['availability'] == 'available' and new['conversationId'] != s.task['convId']
    assert s.task['convId'] in s.conversations and 'router' in s.conversations
    assert len(s.created) == 1 and not s.calls
    [record] = s.audit()
    validate(record, 'ownerDelegatedAction')
    assert (record['status'], record['scope'], record['requester']['kind'], record['target']['path']) == (
        'completed', 'project_repair', 'project_lead', s.project)
    assert record['trigger']['kind'] == 'owner_delegation'
    # Replacing yourself loses the old binding authority, including on a retry.
    replay = json.loads(execute_coordinator_tool(s.task, 'repair_project', s.arguments))
    assert replay['execution_started'] is False and len(s.created) == 1
    assert s.bindings.read(s.project) == new


@pytest.mark.parametrize('fault', ['no-grant', 'revoked', 'narrow', 'tenant', 'owner', 'foreign-conversation',
                                  'peer-target', 'stale', 'stopped', 'failed-attempt', 'folder', 'global-changed'])
def test_own_binding_repair_fails_closed(replacement, fault):
    s = replacement
    if fault != 'no-grant':
        s.grant()
    if fault == 'revoked':
        s.revoke()
    if fault == 'narrow':
        with open(gate._APPROVALS_FILE) as handle:
            document = json.load(handle)
        document['delegations'][str(OWNER)].update(coverage='enumerated', scopes=['project_repair'])
        with open(gate._APPROVALS_FILE, 'w') as handle:
            json.dump(document, handle)
    if fault in {'owner', 'tenant'}:
        s.task['_principalContext'] = PrincipalContext.user(
            subject_id='test', owner_user_id=OWNER + (fault == 'owner'),
            tenant_id='foreign' if fault == 'tenant' else None).to_payload()
    if fault == 'foreign-conversation':
        s.task['convId'] = 'not-the-lead'
    if fault == 'peer-target':
        s.arguments['project_path'] = '/another/project'
    if fault == 'stale':
        s.scope['binding_revision'] = 2
    if fault == 'stopped':
        from lib.project_dispatch.repository import record_stop
        record_stop(OWNER, s.task['convId'], 2, client=s.records)
    if fault == 'failed-attempt':
        s.attempt['status'] = 'failed'
    if fault == 'folder':
        s.folder['projectPaths'] = ['/another/project']
    if fault == 'global-changed':
        s.conversations['router']['settings']['archived'] = True
    with pytest.raises((PermissionError, ValueError)):
        project_repair.repair_project(s.task, s.scope, s.arguments)
    assert s.bindings.read(s.project)['revision'] == 1
    assert not s.created and not s.calls


def test_revocation_after_audit_acceptance_still_blocks_replacement(replacement, monkeypatch):
    s = replacement
    s.grant()
    begin = gate.begin_delegated_action

    def accept_then_revoke(*args, **kwargs):
        receipt = begin(*args, **kwargs)
        s.revoke()
        return receipt

    monkeypatch.setattr(gate, 'begin_delegated_action', accept_then_revoke)
    with pytest.raises(DelegationRefused) as refused:
        project_repair.repair_project(s.task, s.scope, s.arguments)
    assert refused.value.code == 'owner_delegation_not_granted'
    assert s.bindings.read(s.project)['revision'] == 1 and not s.created
    assert s.audit()[0]['status'] == 'failed'


def test_replacement_never_overwrites_a_concurrent_owner_binding(replacement, monkeypatch):
    s = replacement
    s.grant()
    command = s.records.command

    def concurrent_owner_change(operation, payload, command_id=None, **kwargs):
        result = command(operation, payload, command_id, **kwargs)
        if operation == 'conversation.create':
            # The preceding read passed, but an owner clears the binding before the CAS.
            s.bindings.bind(s.project, '', expected_revision=1, command_id='owner-clear')
        return result

    monkeypatch.setattr(s.records, 'command', concurrent_owner_change)
    with pytest.raises(StorageError, match='CAS conflict'):
        project_repair.repair_project(s.task, s.scope, s.arguments)
    binding = s.bindings.read(s.project)
    assert binding['availability'] == 'unbound' and binding['revision'] == 2
    assert not s.calls and s.audit()[0]['status'] == 'failed'


# ── the Router hears each repair Turn's outcome once ────────────────────────

MARKER = {'actionId': 'od-repair', 'routerConversationId': 'router', 'projectPath': '/srv/tofu'}


@pytest.fixture
def outcome(monkeypatch):
    sent = []

    def dispatch(conversation, message, tools, **kwargs):
        sent.append({'conversation': conversation, 'message': deepcopy(message), **kwargs})
        return ScheduledTurnDispatch('started', 'task')
    router = {'availability': 'available', 'conversationId': 'router', 'revision': 1}
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create: dict(router))
    return SimpleNamespace(sent=sent, dispatch=dispatch, router=router)


def _repair_task(status='done', **extra):
    return {'id': 'repair-task', 'convId': 'repair-abc', '_turnId': 'repair-turn', 'status': status,
            'finishReason': 'stop', 'config': {project_repair.REPAIR_MARKER: dict(MARKER)}, **extra}


def test_the_router_hears_a_settled_repair_once_per_turn(outcome):
    task = _repair_task()
    assert project_repair.deliver_repair_outcome(task, MARKER, user_id=OWNER,
                                                 dispatch=outcome.dispatch) == 'started'
    project_repair.deliver_repair_outcome(task, MARKER, user_id=OWNER, dispatch=outcome.dispatch)
    first, second = outcome.sent
    assert first['conversation'] == 'router' and first['inject_mode'] == 'steer'
    assert first['command_id'] == second['command_id']  # the turn command delivers it once
    assert first['message']['_initiator'] == 'proactive'
    text = first['message']['content']
    assert text.startswith(project_repair.OUTCOME_HEADER)
    assert 'repair-abc' in text and 'repair-turn' in text and 'od-repair' in text
    later = _repair_task(_turnId='next-turn')
    project_repair.deliver_repair_outcome(later, MARKER, user_id=OWNER, dispatch=outcome.dispatch)
    assert outcome.sent[-1]['command_id'] != first['command_id']


def test_a_replaced_router_gets_no_repair_notice(outcome):
    outcome.router['conversationId'] = 'newer-router'
    assert project_repair.deliver_repair_outcome(_repair_task(), MARKER, user_id=OWNER,
                                                 dispatch=outcome.dispatch) is None
    assert outcome.sent == []


def test_project_settlement_delivers_the_repair_outcome(monkeypatch):
    settled, notices = [], []
    monkeypatch.setattr('lib.conversations.project_settlement.on_project_task_settled',
                        lambda task, path, *, user_id: settled.append((task['convId'], path, user_id)))
    monkeypatch.setattr('lib.router.project_repair.deliver_repair_outcome',
                        lambda task, marker, *, user_id: notices.append((task['convId'], dict(marker), user_id)))
    from lib.tasks_pkg.project_settlement_sink import settle_project_task
    settle_project_task(_repair_task(), '/srv/tofu', user_id=OWNER)
    settle_project_task({'id': 'plain', 'convId': 'plain-chat', 'config': {}}, '/srv/tofu', user_id=OWNER)
    assert settled == [('repair-abc', '/srv/tofu', OWNER), ('plain-chat', '/srv/tofu', OWNER)]
    assert notices == [('repair-abc', MARKER, OWNER)]
