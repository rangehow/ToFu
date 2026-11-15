"""The Global Router pauses and resumes the nightly backup with its own tool, end to end.

Owner request 2026-11-10 (docs/modules/scheduling_ops.md, "Governed
scheduling"; docs/ROUTER.md R9). On 2026-11-09 a Worker paused the backup
through the owner's admin route over loopback, with no grant, reason or record.
Here the backup row is the one boot installs (``system-1-storage-backup``), the
Global Router binding is real, the owner's grant is the record the switch wrote
on 2026-11-06 (no ``coverage`` field, the scopes of its day), and the owner's
request is a real human Turn of the Router. Its admitted Attempt runs through
the ordinary task start with only the model replaced: the stand-in assembles
the Router's own tool surface and calls ``schedule_manage`` through the tool
pipeline. The storage authority holds the row, the lifecycle gate holds the
grant and its audit records, the tick honours the pause, and no HTTP request
is made.
"""
from __future__ import annotations

from datetime import datetime
import json
import time
import uuid

import pytest

from lib import lifecycle_approval as gate
from lib.identity import PrincipalContext
import lib.scheduler.manager as manager_module
from lib.scheduler.manager import ScheduledTaskManager
from tests.tools.test_tool_settle_all_lanes import _mk_tc

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

OWNER = 1
BACKUP = f'system-{OWNER}-storage-backup'
OWNER_WORDS = '今晚先把备份停掉，明早八点再恢复。'


class _Clock(datetime):
    current = datetime(2026, 11, 12, 2, 0, 5)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def world(tmp_path, monkeypatch):
    from lib.router.binding import global_router
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    router = global_router(OWNER, create=True)
    assert router['availability'] == 'available'
    # The owner's grant as the switch wrote it on 2026-11-06 (production shape).
    (tmp_path / 'lifecycle_approvals.json').write_text(json.dumps({'records': [], 'delegations': {str(OWNER): {
        'version': 1, 'owner': OWNER, 'state': 'granted', 'revision': 1,
        'scopes': ['restart', 'conversation_control'],
        'grantee': {'kind': 'global_router', 'conversationId': router['conversationId'],
                    'bindingRevision': router['revision'], 'tenant': None},
        'grantedAt': 1794350000.0, 'grantedBy': {
            'kind': 'human-ui', 'ownerUserId': OWNER, 'subject': 'open-mode', 'credential': 'open-mode',
            'browser': 'origin', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'},
        'assurance': 'advisory', 'revokedAt': None, 'revokedBy': None, 'updatedAt': 1794350000.0}}}))

    manager = ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='scheduler-e2e', owner_user_id=OWNER, scopes={'scheduler:run'})
    monkeypatch.setattr(manager_module, '_manager', manager)
    manager.delete_task(BACKUP, user_id=OWNER)  # the module's authority outlives one case
    manager._ensure_default_storage_backup_task()
    runs = []
    monkeypatch.setattr(manager, '_execute_task', lambda task: runs.append(task['id']) or (True, 'backed up'))

    audit = []
    for module in ('lib.lifecycle_approval', 'lib.scheduler.executor._roles'):
        monkeypatch.setattr(f'{module}.audit_log', lambda event, **fields: audit.append((event, fields)))

    def no_http(*args, **kwargs):
        raise AssertionError('the Router reached the scheduler over HTTP')
    monkeypatch.setattr('http.client.HTTPConnection.request', no_http)
    return {'router': router, 'manager': manager, 'runs': runs, 'audit': audit}


def _router_turn(world, monkeypatch, steps):
    """The owner's request as a real Router Turn; ``steps(call)`` stands in for the model."""
    from lib import turn_lifecycle
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from tests.support.conversation_workers import observe_admissions, run_admitted_attempt
    admissions = observe_admissions(monkeypatch)
    conversation_turn_commands.create_turn(world['router']['conversationId'], OWNER, {
        'commandId': 'owner-request-' + uuid.uuid4().hex, 'inputTurn': {'content': OWNER_WORDS},
        'config': {'model': 'claude-opus-5.5'}}, request_started_at=time.time())
    [admission] = admissions
    reply = turn_lifecycle.get_turn(world['router']['conversationId'], admission['config']['_turnId'],
                                    user_id=OWNER)
    owner_turn = reply['parentTurnId']

    def probe(task):
        tools = _router_surface(task, world['router']['projectPath'])

        def call(call_id, **arguments):
            parsed = _mk_tc(call_id, 'schedule_manage', 0, args=arguments)
            task['toolRounds'] = [parsed[5]]
            messages = []
            execute_tool_pipeline(task, [parsed], task['config'], None, False, tools, messages, [], 0,
                                  'claude-opus-5.5')
            [reply] = [message for message in messages if message.get('tool_call_id') == call_id]
            return {**parsed[5], 'modelText': json.dumps(reply.get('content'), ensure_ascii=False)}
        return steps(call, tools, owner_turn)

    _turn, [result] = run_admitted_attempt(OWNER, admission['config']['_attemptId'], monkeypatch, probe)
    return result, owner_turn


def _router_surface(task, home):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    cfg = task['config']
    cfg.update(_turnOwnerUserId=OWNER, memoryEnabled=False, mcpEnabled=False, plugins=[])
    tools, available = assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': home,
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': 'claude-opus-5.5'})
    assert available
    return tools


def _enabled(world):
    return bool(world['manager'].get_task(BACKUP, user_id=OWNER)['enabled'])


def _tick(world, when):
    _Clock.current = when
    world['manager']._check_and_run_due_tasks()
    assert world['manager'].stop(timeout=10)


def _result_text(round_entry):
    """What the model reads back for the call."""
    return round_entry['modelText']


def test_the_router_pauses_and_resumes_the_nightly_backup_through_its_tool(world, monkeypatch):
    monkeypatch.setattr(manager_module, 'datetime', _Clock)
    assert _enabled(world)
    assert world['manager'].get_task(BACKUP, user_id=OWNER)['system_key'] == 'storage-backup'

    def steps(call, tools, owner_turn):
        [manage] = [tool for tool in tools if tool['function']['name'] == 'schedule_manage']
        assert 'owner_delegation' in manage['function']['parameters']['properties']
        authority = {'reason': 'The owner asked to stop tonight\u2019s backup', 'source_turn_id': owner_turn}
        seen = {'paused': call('pause-1', action='disable', task_id=BACKUP, idempotency_key='pause-backup',
                               owner_delegation=authority)}
        seen['enabled_after_pause'] = _enabled(world)
        # A paused backup is not claimed at 02:00; a replayed call never acts twice.
        _tick(world, datetime(2026, 11, 12, 2, 0, 5))
        seen['runs_while_paused'] = list(world['runs'])
        seen['replay'] = call('pause-2', action='disable', task_id=BACKUP, idempotency_key='pause-backup',
                              owner_delegation=authority)
        seen['resumed'] = call('resume-1', action='enable', task_id=BACKUP, idempotency_key='resume-backup',
                               owner_delegation={**authority, 'reason': 'The owner asked to resume it'})
        seen['enabled_after_resume'] = _enabled(world)
        return seen

    seen, owner_turn = _router_turn(world, monkeypatch, steps)

    assert seen['paused']['status'] == 'done', seen['paused']
    assert 'paused under the owner\u2019s delegation' in _result_text(seen['paused'])
    assert seen['enabled_after_pause'] is False and seen['runs_while_paused'] == []
    assert seen['replay']['status'] == 'done' and 'Already done' in _result_text(seen['replay'])
    assert seen['resumed']['status'] == 'done', seen['resumed']
    assert seen['enabled_after_resume'] is True
    _tick(world, datetime(2026, 11, 13, 2, 0, 5))
    assert world['runs'] == [BACKUP]

    # The owner's audit log: one record per use, with the owner's words, the
    # target and the grant it used, the way the Act-for-me disclosure reads it.
    from lib.router.owner_delegation import owner_delegation_state
    state = owner_delegation_state(OWNER)
    assert state['active'] and state['grant']['coverage'] == 'full' and 'scheduler' in state['grant']['scopes']
    resume_record, pause_record = state['actions']
    for record, action in ((pause_record, 'disable'), (resume_record, 'enable')):
        assert (record['scope'], record['status'], record['grantRevision']) == ('scheduler', 'completed', 1)
        assert record['target'] == {'kind': 'scheduled_task', 'taskId': BACKUP, 'taskScope': 'system',
                                    'action': action}
        assert record['trigger'] == {'kind': 'owner_message', 'turnId': owner_turn, 'text': OWNER_WORDS}
        assert record['requester']['conversationId'] == world['router']['conversationId']
    events = [event for event, _fields in world['audit']]
    assert events.count('owner_delegation_action') == 2 and events.count('owner_delegation_action_settled') == 2
    assert [fields['action'] for event, fields in world['audit'] if event == 'scheduled_task_controlled'] == [
        'disable', 'enable']


def test_without_the_grant_the_router_is_refused_and_the_backup_keeps_running(world, monkeypatch):
    monkeypatch.setattr(manager_module, 'datetime', _Clock)
    gate.set_delegation(OWNER, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'})

    def steps(call, tools, owner_turn):
        return call('pause-1', action='disable', task_id=BACKUP, idempotency_key='pause-backup',
                    owner_delegation={'reason': 'Pause it', 'source_turn_id': owner_turn})

    refused, _owner_turn = _router_turn(world, monkeypatch, steps)

    assert refused['status'] == 'error', refused
    text = _result_text(refused)
    assert 'owner_delegation_not_granted' in text and 'Act for me' in text and 'Nothing changed.' in text
    assert _enabled(world)
    _tick(world, datetime(2026, 11, 12, 2, 0, 5))
    assert world['runs'] == [BACKUP]
