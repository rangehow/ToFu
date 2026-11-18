"""A Coordinator wake's continuation context reads its outcome ownership on real storage.

Spec for docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task
board", on a real native TofuDB engine (isolated, this test's own data root): the
real dispatch repository, grants, Turns, task board and the real continuation birth
through recovery. The wake's own request row is unborn when its context is read, so
it has no Turn to read; the outcome-ownership section must still be populated.
Substituted: the model and the turn executor (inputs are persisted, never run).
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import uuid

import pytest

from tests.storage.test_native_project_dispatch import native_dispatch_authority as native_dispatch_authority

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)]

MCFG = {'model': 'fixture-model', 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
        'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
        'desktop_enabled': False, 'image_gen_enabled': False}


def _settle(factory, owner, attempt_id, task_id, content):
    factory(write=True).command('turn.event.record', dict(
        user_id=owner, attempt_id=attempt_id, task_id=task_id, terminal=True, status='completed',
        projection={'content': content}, settlement={'outcome': 'completed', 'cause': 'stop',
                                                     'resumeOptions': []}), uuid.uuid4().hex)


def test_a_wake_reads_outcome_ownership_through_real_storage(
        native_dispatch_authority, tmp_path, monkeypatch, caplog):
    from lib import integration_control as ic, turn_lifecycle as turns
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversations import coordinator_births, coordinator_workers
    from lib.conversations.coordinator_continuation import _UNOWNED_HEADING
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.identity import PrincipalContext
    from lib.project_dispatch import admission
    from lib.project_dispatch.continuation import recover_coordinator
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.request_budget import bind_source_account
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_mod import modifications
    from lib.router.binding import global_router
    from lib.tasks_pkg.coordinator_policy import pin_coordinator_request_policy
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools

    factory, owner, _engine = native_dispatch_authority
    caplog.set_level('WARNING', logger='lib.conversations.coordinator_continuation')
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **kw: None)
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())

    router = global_router(owner, create=True)
    conv, home = router['conversationId'], router['projectPath']
    mcfg = {**MCFG, 'project_path': home}
    Path(home).mkdir(parents=True, exist_ok=True)
    pair = turns.create_turn_pair(conv, user_id=owner, command_id='owner-request',
                                  input_projection={'content': 'Ship the paper and its appendix.'}, config={})
    root_id = 'native-file-request-' + uuid.uuid4().hex
    turns.bind_task(pair['attempt']['attemptId'], root_id, user_id=owner)
    turns.mark_task_started(pair['attempt']['attemptId'], root_id, user_id=owner)
    config = pin_coordinator_request_policy(conv, {'projectPath': home, 'codeExecEnabled': True,
                                                   'autoApply': True}, owner)
    config.update(_turnOwnerUserId=owner, memoryEnabled=True, mcpEnabled=False, plugins=[])
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id='native-test', scopes=[])
    task = {'id': root_id, 'convId': conv, '_userId': owner, '_attended': True,
            'messages': [{'role': 'user', 'content': 'Ship the paper and its appendix.'}],
            '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            '_principalContext': principal.to_payload(), 'config': config}
    assemble_round_tools(task['config'], task, mcfg)
    capture_worker_policy(task, task['config'], task['_executable_tool_catalog'], 'fixture-model',
                          worker_model='fixture-model')
    repo = ProjectDispatchRepository(owner, home)
    bind_source_account(task, repository=repo)
    lease = task['_executionBudget'].reserve_model(task, input_tokens=5, max_output_tokens=5)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 5, 'completion_tokens': 5})

    def transport(conversation, cfg, data, watermark, bind, **kwargs):
        identity = 'native-file-' + uuid.uuid4().hex
        bind(identity)
        granted = admission.principal_for(cfg[admission.KEY])
        run = {'id': identity, 'convId': conversation, 'config': cfg, 'status': 'running', 'messages': [],
               'usage': {}, 'toolRounds': [], 'events': [], '_userId': owner,
               '_principalContext': granted.to_payload(), '_tenant_id': granted.tenant_id,
               '_attended': False, '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
               'abort_event': threading.Event()}
        admission.bind_account(run)
        spent = model_budget_admission(run)(input_tokens=4, max_output_tokens=6)
        spent.mark_dispatched(); spent.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        if conversation != conv:
            _settle(factory, owner, cfg['_attemptId'], identity, 'Paper body drafted.')
        return identity, None

    commands = ConversationTurnCommandService(build_user_message=lambda message, *args: message,
                                              was_aborted_after=lambda *args: False, start_task=transport)
    service = ProjectDispatchService(repo, commands=commands)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda repository: service)
    delegated = json.loads(execute_coordinator_tool(task, 'delegate_project_work', {
        'title': 'Paper body', 'brief': 'Draft the paper body.', 'idempotency_key': 'paper-body'}))
    assert delegated.get('status') == 'completed', delegated
    board = json.loads(execute_coordinator_tool(task, 'update_task_progress', {
        'summary': 'The paper and its appendix', 'tasks': [
            {'title': 'Paper body', 'status': 'in_progress'},
            {'title': 'Appendix tables', 'status': 'pending'}]}))
    assert 'error' not in board, board
    _settle(factory, owner, task['_attemptId'], task['id'], 'Delegated the body; the appendix is next.')

    assert recover_coordinator(repo, service=service) == ['accepted']
    wake_input = [turn for turn in turns.list_turns(conv, user_id=owner)['turns']
                  if turn['actor'] != 'human' and turn['actor'] != 'assistant'][-1]
    content = turns.get_turn(conv, wake_input['turnId'], user_id=owner)['projection']['content']
    unreadable = [record.getMessage() for record in caplog.records if 'unreadable' in record.getMessage()]
    assert unreadable == []
    assert 'could not be read now' not in content
    assert _UNOWNED_HEADING in content
    assert '- [unowned] Appendix tables' in content
