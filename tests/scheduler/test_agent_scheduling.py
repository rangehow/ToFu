"""Agents schedule recurring work through the owner's scheduler (owner verdict 2026-11-13).

The Global Router, Project Coordinators and their Workers create, list, update,
pause and delete scheduled tasks with the ordinary scheduler tool names, by
default: no owner hand-off for a patrol or a probe. Every task an agent
creates records who created it (role, conversation, Turn, Attempt, assignment
and the request that delegated it) and a required purpose, and appears in the
owner's scheduled-task list with that attribution. It runs with no more
capability than its creator holds, keeps running after its creator ends (the
end is shown on it), stops in one owner action or by its creator's lead, and
every create, update and stop is in the owner's audit log. Owner:
docs/modules/scheduling_ops.md, "Agent-created schedules".
"""
from __future__ import annotations

import asyncio
import itertools
import json
import threading
from types import SimpleNamespace

import pytest

import lib.scheduler.manager as manager_module
from lib.scheduler.authority import SchedulerActor, read_grant, row_scope
from lib.scheduler.executor import execute_scheduler_tool
from lib.scheduler.executor._roles import execute_role_scheduler_tool
from lib.scheduler.manager import ScheduledTaskManager
from lib.tools.result_value import tool_result_failure_verdict

pytestmark = pytest.mark.unit

OWNER = 7
COORDINATOR = 'coordinator-project'
ROUTER = 'router-home-conversation'
FAMILY = {'schedule_create', 'schedule_list', 'schedule_manage'}
PROBE = {'name': 'Hope liveness probe', 'schedule': '13,43 * * * *', 'task_type': 'command',
         'command': 'echo hope-probe-ok',
         'purpose': 'Hope liveness patrol: probe the eval jobs every 30 minutes for request R-17.',
         'idempotency_key': 'hope-probe'}


def _actor(role, *, conversation, project, assignment=None, coordinator=COORDINATOR,
           code_execution=False, request_attempt=None):
    return SchedulerActor(role=role, owner=OWNER, tenant=None, conversation_id=conversation,
                          task_id=f'task-{conversation}', turn_id=f'turn-{conversation}',
                          attempt_id=f'attempt-{conversation}', project_path=project,
                          coordinator_id=coordinator, binding_revision=1, assignment_id=assignment,
                          code_execution=code_execution, request_attempt_id=request_attempt)


def _call(actor, tool, **arguments):
    return execute_role_scheduler_tool(actor, tool, dict(arguments), task={})


def _failed(result, code):
    assert tool_result_failure_verdict(result) == 'error', result
    assert result.startswith('Error:') and f'(code: {code})' in result, result


def _schema(tools, name):
    [tool] = [tool for tool in tools if (tool.get('function') or {}).get('name') == name]
    return tool['function']['parameters']


# ── Real storage, the owner's HTTP surface and the audit log ────────────────────


class _Client:
    def __init__(self, supervisor):
        self._client = supervisor.client

    def query(self, operation, payload, **_kwargs):
        return self._client.query(operation, payload)

    def command(self, operation, payload, key=None, **_kwargs):
        return self._client.command(operation, payload, key or operation)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    supervisor = StorageSupervisor(project_root=tmp_path / 'storage', backend='sqlite',
                                   startup_timeout=60)
    supervisor.start()
    client = _Client(supervisor)
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda *, write=False: client)
    monkeypatch.setattr(manager_module, '_manager', None)
    yield client
    supervisor.stop()


@pytest.fixture
def audit(monkeypatch):
    events = []

    def record(event, **fields):
        events.append((event, fields))
    for module in ('lib.scheduler.executor._roles', 'lib.scheduler.manager'):
        monkeypatch.setattr(f'{module}.audit_log', record)
    return events


@pytest.fixture
def workspace(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    return str(project)


@pytest.fixture
def creators(monkeypatch, workspace):
    """The creators' live state: assignment rows and Coordinator bindings, read at list time."""
    state = SimpleNamespace(review=None, revoked=False, coordinator=COORDINATOR)

    class Assignments:
        def __init__(self, owner, project):
            assert (owner, project) == (OWNER, workspace)

        def read(self, assignment_id):
            return {'id': assignment_id, 'grant': {'revoked': state.revoked},
                    'review': {'decision': state.review} if state.review else None,
                    'execution': {'taskId': 'worker-task'}, 'request': {}}

    def bindings(owner):
        return SimpleNamespace(read=lambda path: {
            'projectPath': path, 'conversationId': state.coordinator, 'revision': 1,
            'availability': 'available'})
    monkeypatch.setattr('lib.project_dispatch.repository.ProjectDispatchRepository', Assignments)
    monkeypatch.setattr('lib.conversations.project_coordination.ProjectCoordinationRepository', bindings)
    return state


def _worker(workspace, *, code_execution=True, conversation='worker-a', assignment='assignment-a'):
    return _actor('worker', conversation=conversation, project=workspace, assignment=assignment,
                  code_execution=code_execution, request_attempt='request-attempt-1')


def _lead(workspace):
    return _actor('coordinator', conversation=COORDINATOR, project=workspace)


@pytest.fixture
def owner_http(monkeypatch):
    """The owner's existing scheduled-task routes over the real blueprint."""
    from quart import Quart
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes.api_v1.auth import bearer_auth_before_request
    import routes.api_v1.scheduler as routes_module
    app = Quart(__name__, static_folder=None)
    register_http_error_handlers(app)
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(routes_module.api_v1_scheduler_bp)
    monkeypatch.setattr(routes_module, '_request_user_id', lambda: OWNER)
    transcript = []

    def call(method, path):
        async def go():
            response = await getattr(app.test_client(), method)(path)
            return response.status_code, await response.get_json()
        status, body = asyncio.run(go())
        transcript.append({'request': f'{method.upper()} {path}', 'status': status, 'body': body})
        return status, body
    call.transcript = transcript
    return call


def _only(storage, **match):
    rows = [row for row in ScheduledTaskManager().list_tasks(user_id=OWNER, include_disabled=True)
            if all(row.get(key) == value for key, value in match.items())]
    assert len(rows) == 1, rows
    return rows[0]


_TICKS = itertools.count()


def _due(monkeypatch):
    """Run one scheduler tick at the next minute; return the task ids it dispatched."""
    from datetime import datetime, timedelta
    dispatched = []
    minute = datetime(2026, 11, 13, 22, 13, 5) + timedelta(minutes=next(_TICKS))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return minute
    monkeypatch.setattr(manager_module, 'datetime', Clock)
    manager = ScheduledTaskManager()
    monkeypatch.setattr(manager, '_dispatch_claimed_task', lambda task: dispatched.append(task['id']))
    manager._check_and_run_due_tasks()
    return dispatched


# ── (a) A delegated Worker holds the scheduler family by default ────────────────


class _NoMcp:
    connected = False
    server_count = 0

    def get_openai_tool_defs(self):
        return []

    def get_tool_catalog_snapshot(self):
        return []

    def is_mcp_tool(self, name):
        return False


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """The Global Router's home and one Project Coordinator, bound for this owner."""
    import lib.mcp as mcp_module
    from lib.conversations import project_coordination
    from lib.router.home import router_home_location
    from lib.tools.routing import _reset_sticky_exposure_for_tests
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    project = tmp_path / 'bound-project'
    project.mkdir()
    bindings = {router_home_location(OWNER): ROUTER, str(project): COORDINATOR}

    def read(path):
        return {'projectPath': path, 'conversationId': bindings.get(path, ''),
                'availability': 'available' if path in bindings else 'unbound', 'revision': 1}
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=read))
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _NoMcp())
    monkeypatch.setattr('lib.channels.tools.visible_tools', lambda owner, conversation: [])
    _reset_sticky_exposure_for_tests()
    yield {conversation: path for path, conversation in bindings.items()}
    _reset_sticky_exposure_for_tests()


def _mcfg(path):
    return {'tid': 'agent-scheduling', 'model': 'model-a', 'project_path': path,
            'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False,
            'human_guidance_enabled': False, 'scheduler_enabled': False}


def _assemble(conversation, path, *, task_id=None, cfg=None):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    cfg = cfg or {'projectPath': path, '_turnOwnerUserId': OWNER, 'userId': OWNER,
                  'memoryEnabled': False, 'plugins': [], 'codeExecEnabled': True}
    task = {'id': task_id or f'{conversation}-task', '_userId': OWNER, 'config': cfg,
            'convId': conversation, 'messages': [{'role': 'user', 'content': '继续'}],
            'abort_event': threading.Event()}
    tools, available = assemble_round_tools(cfg, task, _mcfg(path))
    assert available
    return task, tools


def test_a_delegated_workers_grant_carries_the_scheduler_family_by_default(homes):
    task, _tools = _assemble(COORDINATOR, homes[COORDINATOR])
    policy = task['_coordinatorWorkerPolicy']

    assert FAMILY <= set(policy.tools)
    assert not {'timer_create', 'timer_manage'} & set(policy.tools)


@pytest.mark.parametrize('code_execution', [True, False], ids=['run_command', 'no-code'])
def test_a_workers_surface_offers_code_tasks_exactly_when_its_grant_runs_code(
        homes, monkeypatch, code_execution):
    from lib.project_dispatch import admission
    coordinator, _ = _assemble(COORDINATOR, homes[COORDINATOR])
    granted = [name for name in coordinator['_coordinatorWorkerPolicy'].tools
               if code_execution or name != 'run_command']
    profile = {'mode': 'project', 'tools': granted}
    monkeypatch.setattr(admission, 'is_dispatch_task', lambda task: task.get('id') == 'granted-worker')
    monkeypatch.setattr(admission, 'granted_coordinator_scope', lambda task: (True, None))
    monkeypatch.setattr(admission, 'validate_task', lambda task, **_: {'assignmentId': 'grant'})
    monkeypatch.setattr(admission, 'repository_for',
                        lambda binding: SimpleNamespace(read=lambda assignment: {}))
    monkeypatch.setattr(admission, 'profile_for', lambda assignment: profile)
    path = homes[COORDINATOR]
    cfg = {'userId': OWNER, 'projectPath': path, 'plugins': [], 'memoryEnabled': False,
           'codeExecEnabled': code_execution,
           **coordinator['_coordinatorWorkerPolicy'].config}

    task, _tools = _assemble('granted-worker-conv', path, task_id='granted-worker', cfg=cfg)

    catalog = task['_executable_tool_catalog']
    assert FAMILY <= {tool['function']['name'] for tool in catalog}
    # schedulerEnabled=false only defers the family behind Tool Search; it is found there.
    from lib.tools.gateway import search_executable_catalog
    found = search_executable_catalog(
        catalog, 'schedule a recurring cron probe',
        namespace_by_name=task.get('_executableToolNamespaceByName') or {},
        search_text_by_name=task.get('_executableToolSearchTextByName') or {},
        contract_documents_by_name=task.get('_toolContractDocumentsByName') or {},
        visible_names=set())
    assert 'schedule_create' in {item['name'] for item in found['items']}
    create = _schema(catalog, 'schedule_create')
    offered = set(create['properties']['task_type']['enum'])
    assert {'project_wake', 'prompt'} <= offered
    assert ({'command', 'python'} <= offered) is code_execution
    assert code_execution or not {'command', 'python'} & offered
    assert 'agent' not in offered
    assert {'purpose', 'idempotency_key'} <= set(create['required'])


@pytest.mark.parametrize('mode', ['coordinator', 'project'])
def test_a_grant_frozen_before_the_family_holds_it(mode):
    from lib.project_dispatch.repository import CONTRACT
    from lib.project_dispatch.scope import granted_tools
    frozen = list(CONTRACT['continuationTools']) if mode == 'coordinator' else ['read_files', 'run_command']
    assert not FAMILY & set(frozen)
    # A continuation holds its role's catalog, a Worker the derived family (docs/ROUTER.md R15).
    profile = {'mode': mode, 'tools': frozen}
    grant = {'request': {'projectPath': '/project', 'coordinatorId': COORDINATOR, 'profile': profile},
             'grant': {'owner': OWNER, 'bindingRevision': 1}}

    assert FAMILY <= granted_tools(profile, assignment=grant)
    assert not FAMILY & granted_tools({'mode': 'consult', 'tools': ['read_files']})


@pytest.mark.parametrize('conversation', [ROUTER, COORDINATOR])
def test_a_continuation_turn_schedules_with_its_frozen_grant(homes, monkeypatch, conversation):
    """Tonight's gap: the Router's and a Coordinator's automatic continuations had no scheduler."""
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import CONTRACT
    path = homes[conversation]
    # The continuation grant as every open request froze it: no scheduler name.
    profile = {'mode': 'coordinator', 'tools': list(CONTRACT['continuationTools'])}
    scope = {'project_path': path, 'binding_revision': 1, 'conversation_id': conversation, 'user_id': OWNER}
    grant = {'request': {'projectPath': path, 'coordinatorId': conversation, 'profile': profile},
             'grant': {'owner': OWNER, 'bindingRevision': 1}}
    monkeypatch.setattr(admission, 'is_dispatch_task', lambda task: task.get('id') == 'continuation')
    monkeypatch.setattr(admission, 'granted_coordinator_scope', lambda task: (True, dict(scope)))
    monkeypatch.setattr(admission, 'validate_task', lambda task, **_: {'assignmentId': 'grant'})
    monkeypatch.setattr(admission, 'repository_for',
                        lambda binding: SimpleNamespace(read=lambda assignment: grant))
    monkeypatch.setattr(admission, 'profile_for', lambda assignment: profile)
    monkeypatch.setattr('lib.project_dispatch.delegation.capture_worker_policy', lambda *args, **kwargs: None)

    task, tools = _assemble(conversation, path, task_id='continuation')

    catalog = task['_executable_tool_catalog']
    assert FAMILY <= {tool['function']['name'] for tool in catalog}
    create = _schema(catalog, 'schedule_create')
    assert set(create['properties']['task_type']['enum']) == {'project_wake', 'prompt'}
    # Outside the role's direct core: a route's tool-name budget may defer them to
    # search_tools/execute_tools, which every continuation grant holds.
    assert not FAMILY & set(task.get('_requiredDirectToolNames') or ())
    assert {'search_tools', 'execute_tools'} <= set(profile['tools'])


# ── (b) The Global Router and a Coordinator schedule ────────────────────────────


@pytest.mark.parametrize('conversation', [ROUTER, COORDINATOR])
def test_the_router_and_a_coordinator_hold_the_scheduler_on_their_surface(homes, conversation):
    task, _tools = _assemble(conversation, homes[conversation])

    catalog = task['_executable_tool_catalog']
    assert FAMILY <= {tool['function']['name'] for tool in catalog}
    create = _schema(catalog, 'schedule_create')
    assert set(create['properties']['task_type']['enum']) == {'project_wake', 'prompt'}
    assert {'name', 'schedule', 'purpose', 'idempotency_key'} <= set(create['required'])


def test_the_router_and_a_coordinator_create_a_schedule(storage, audit, creators, workspace):
    router = _actor('router', conversation=ROUTER, project='/router/home', coordinator=ROUTER)
    lead = _lead(workspace)

    for actor, task_type in ((router, 'prompt'), (lead, 'prompt'), (lead, 'project_wake')):
        payload = {'command': 'Summarize yesterday\u2019s failed jobs.'} if task_type == 'prompt' else {}
        result = _call(actor, 'schedule_create', name=f'{actor.role} {task_type}',
                       schedule='7 9 * * 1-5', task_type=task_type, **payload,
                       purpose=f'{actor.role} morning check', idempotency_key=f'{actor.role}-{task_type}')
        assert tool_result_failure_verdict(result) is None, result

    rows = ScheduledTaskManager().list_tasks(user_id=OWNER)
    created = {(row['created_by']['role'], row['task_type']) for row in rows}
    assert created == {('router', 'prompt'), ('coordinator', 'prompt'), ('coordinator', 'project_wake')}
    assert all(row_scope(row) == 'project' for row in rows)


# ── (c) Creator and purpose on the row and in the owner's list ──────────────────


def test_an_agent_task_records_its_creator_and_purpose_in_the_owners_list(
        storage, audit, creators, workspace, owner_http):
    worker = _worker(workspace)
    _failed(_call(worker, 'schedule_create', **{**PROBE, 'purpose': ' '}), 'scheduler_invalid_argument')
    assert ScheduledTaskManager().list_tasks(user_id=OWNER, include_disabled=True) == []

    result = _call(worker, 'schedule_create', **PROBE)
    assert tool_result_failure_verdict(result) is None, result

    row = _only(storage, name=PROBE['name'])
    grant = read_grant(row)
    assert grant['reason'] == PROBE['purpose'] and grant['workspace'] == workspace
    assert grant['creator'] == {
        'role': 'worker', 'conversationId': 'worker-a', 'taskId': 'task-worker-a',
        'turnId': 'turn-worker-a', 'attemptId': 'attempt-worker-a',
        'assignmentId': 'assignment-a', 'requestAttemptId': 'request-attempt-1'}
    assert (row['task_type'], row['schedule'], row['command']) == ('command', '13,43 * * * *',
                                                                    PROBE['command'])

    status, body = owner_http('get', '/api/v1/scheduler/tasks')
    assert status == 200
    [listed] = body['tasks']
    assert listed['purpose'] == PROBE['purpose']
    assert listed['created_by'] == {
        'role': 'worker', 'conversationId': 'worker-a', 'turnId': 'turn-worker-a',
        'attemptId': 'attempt-worker-a', 'assignmentId': 'assignment-a',
        'requestAttemptId': 'request-attempt-1', 'projectPath': workspace,
        'leadConversationId': COORDINATOR}
    assert listed['creator_state'] == {'state': 'active'}
    status, body = owner_http('get', f'/api/v1/scheduler/tasks/{listed["id"]}')
    assert status == 200 and body['task']['purpose'] == PROBE['purpose']

    listing = execute_scheduler_tool('schedule_list', {'_user_id': OWNER})
    assert 'Created by: worker worker-a' in listing and 'assignment-a' in listing
    assert f'Purpose: {PROBE["purpose"]}' in listing


def test_a_task_outlives_its_creator_and_shows_the_creators_end(
        storage, audit, creators, workspace, monkeypatch):
    worker = _worker(workspace)
    _call(worker, 'schedule_create', **{**PROBE, 'schedule': '* * * * *'})
    creators.review = 'accept'

    row = _only(storage, name=PROBE['name'])
    assert row['creator_state'] == {'state': 'ended', 'reason': 'reviewed_accept'}
    assert row['enabled'] and _due(monkeypatch) == [row['id']]

    creators.revoked = True
    assert _only(storage, name=PROBE['name'])['creator_state'] == {
        'state': 'ended', 'reason': 'grant_revoked'}


# ── (d) The owner pauses and stops it in one action; the stop is honored ────────


def test_the_owner_pauses_and_stops_an_agent_task_in_one_action(
        storage, audit, creators, workspace, owner_http, monkeypatch):
    worker = _worker(workspace)
    _call(worker, 'schedule_create', **{**PROBE, 'schedule': '* * * * *'})
    task_id = _only(storage, name=PROBE['name'])['id']
    assert _due(monkeypatch) == [task_id]

    assert owner_http('post', f'/api/v1/scheduler/tasks/{task_id}/pause')[0] == 200
    assert _due(monkeypatch) == []
    # An agent never undoes the owner's pause.
    _failed(_call(worker, 'schedule_manage', action='enable', task_id=task_id), 'scheduler_owner_paused')
    _failed(_call(_lead(workspace), 'schedule_manage', action='enable', task_id=task_id),
            'scheduler_owner_paused')
    assert _due(monkeypatch) == []

    assert owner_http('post', f'/api/v1/scheduler/tasks/{task_id}/resume')[0] == 200
    assert _due(monkeypatch) == [task_id]

    assert owner_http('delete', f'/api/v1/scheduler/tasks/{task_id}')[0] == 200
    assert _due(monkeypatch) == []
    assert owner_http('get', '/api/v1/scheduler/tasks?include_disabled=true')[1]['tasks'] == []


def test_the_creators_lead_stops_it_and_a_creator_controls_only_its_own(
        storage, audit, creators, workspace):
    worker, other = _worker(workspace), _worker(workspace, conversation='worker-b', assignment='assignment-b')
    _call(worker, 'schedule_create', **PROBE)
    task_id = _only(storage, name=PROBE['name'])['id']

    _failed(_call(other, 'schedule_manage', action='disable', task_id=task_id), 'scheduler_out_of_grant')
    assert _call(_lead(workspace), 'schedule_manage', action='disable', task_id=task_id).endswith('paused.')
    assert _call(worker, 'schedule_manage', action='enable', task_id=task_id).endswith('resumed.')

    # The Global Router leads every Coordinator: it stops a task of any project.
    lead_row = _call(_lead(workspace), 'schedule_create', name='Morning review', schedule='7 9 * * *',
                     task_type='prompt', command='List stale Workers.', purpose='Morning review.',
                     idempotency_key='morning')
    assert tool_result_failure_verdict(lead_row) is None, lead_row
    router = _actor('router', conversation=ROUTER, project='/router/home', coordinator=ROUTER)
    for row in ScheduledTaskManager().list_tasks(user_id=OWNER):
        assert _call(router, 'schedule_manage', action='delete', task_id=row['id']).endswith('cancelled.')
    assert ScheduledTaskManager().list_tasks(user_id=OWNER, include_disabled=True) == []


# ── (e) No agent schedules more capability than it holds ────────────────────────


@pytest.mark.parametrize('case', [
    ('worker-without-code', 'command'), ('worker-without-code', 'python'),
    ('coordinator', 'command'), ('router', 'python'),
    ('worker', 'agent'), ('coordinator', 'agent'),
])
def test_no_agent_schedules_more_capability_than_it_holds(storage, audit, creators, workspace, case):
    role, task_type = case
    actor = {'worker-without-code': _worker(workspace, code_execution=False),
             'worker': _worker(workspace),
             'coordinator': _lead(workspace),
             'router': _actor('router', conversation=ROUTER, project='/router/home', coordinator=ROUTER),
             }[role]
    code = 'scheduler_task_type' if task_type == 'agent' else 'scheduler_capability_code_execution'

    result = _call(actor, 'schedule_create', **{**PROBE, 'task_type': task_type})

    _failed(result, code)
    assert 'Nothing changed' in result
    assert ScheduledTaskManager().list_tasks(user_id=OWNER, include_disabled=True) == []
    assert [event for event, _fields in audit] == ['scheduled_task_refused']


def test_a_lead_without_code_execution_cannot_fire_a_code_task_now(storage, audit, creators, workspace):
    _call(_worker(workspace), 'schedule_create', **PROBE)
    task_id = _only(storage, name=PROBE['name'])['id']

    _failed(_call(_lead(workspace), 'schedule_manage', action='run', task_id=task_id),
            'scheduler_capability_code_execution')
    assert _only(storage, name=PROBE['name'])['run_count'] == 0


def test_no_caller_forges_an_agents_attribution(storage, audit):
    forged = execute_scheduler_tool('schedule_create', {
        '_user_id': OWNER, 'name': 'forged', 'schedule': '7 9 * * *', 'task_type': 'agent',
        'command': 'do it', 'target_conv_id': 'owner-chat',
        'tools_config': {'creatorGrant': {'creator': {'role': 'worker'}}}})

    assert tool_result_failure_verdict(forged) == 'error' and 'creatorGrant' in forged
    assert ScheduledTaskManager().list_tasks(user_id=OWNER, include_disabled=True) == []


def test_a_code_task_runs_in_its_creators_project_workspace(storage, audit, creators, workspace):
    import os
    _call(_worker(workspace), 'schedule_create', **{**PROBE, 'command': 'pwd'})
    row = _only(storage, name=PROBE['name'])

    ok, output = ScheduledTaskManager()._execute_task(row)
    assert ok and output.strip() == os.path.realpath(workspace)

    os.rmdir(workspace)
    ok, output = ScheduledTaskManager()._execute_task(row)
    assert not ok and 'nothing ran' in output and workspace in output


# ── (f) Create, update and stop are in the owner's audit log ────────────────────


def test_create_update_and_stop_are_in_the_owners_audit_log(
        storage, audit, creators, workspace, owner_http):
    worker = _worker(workspace)
    _call(worker, 'schedule_create', **PROBE)
    task_id = _only(storage, name=PROBE['name'])['id']
    _call(worker, 'schedule_manage', action='update', task_id=task_id, updates={'schedule': '17,47 * * * *'})
    owner_http('post', f'/api/v1/scheduler/tasks/{task_id}/pause')
    owner_http('delete', f'/api/v1/scheduler/tasks/{task_id}')

    created = [fields for event, fields in audit if event == 'scheduled_task_created']
    assert created and created[0]['task_id'] == task_id
    assert created[0]['created_by_role'] == 'worker' and created[0]['created_by_conversation'] == 'worker-a'
    assert created[0]['purpose'] == PROBE['purpose']
    controls = [(fields['role'], fields['action'], fields.get('outcome')) for event, fields in audit
                if event == 'scheduled_task_controlled' and fields['task_id'] == task_id]
    assert controls == [('worker', 'create', 'created'), ('worker', 'update', 'applied'),
                        ('owner', 'disable', 'applied'), ('owner', 'delete', 'applied')]
    owner_stop = [fields for event, fields in audit
                  if event == 'scheduled_task_controlled' and fields['role'] == 'owner']
    assert all(fields['created_by_conversation'] == 'worker-a' and fields['via'] == 'http'
               for fields in owner_stop)
    print(json.dumps(owner_http.transcript, ensure_ascii=False, indent=1))
