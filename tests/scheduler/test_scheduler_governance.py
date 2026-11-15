"""Governed scheduling: one scheduler, scoped grants (docs/modules/scheduling_ops.md).

Project roles reach the scheduler with the ordinary tool names: a Worker
schedules one-shot wakes of its Coordinator and controls only its own, a
Coordinator controls every wake of its project, and the Global Router reaches
the owner's own, service and system rows only under the owner's delegation
(docs/ROUTER.md R9). A row's scope comes from the row, a project row carries
its grant, every refusal is a failed call that changed nothing, every outcome
is audited, and the claim fires each occurrence once across a restart and two
concurrent claimers on both storage authorities.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import threading

import pytest

import lib.scheduler.manager as manager_module
from lib.scheduler.authority import SchedulerActor, read_grant, resolve_actor
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.executor import execute_scheduler_tool
from lib.scheduler.executor._roles import execute_role_scheduler_tool
from lib.scheduler.manager import ScheduledTaskManager
from lib.tools.result_value import tool_result_failure_verdict
from tests.router.test_owner_delegation import OWNER, approvals, d as d, standing  # noqa: F401 (fixtures)

pytestmark = pytest.mark.unit

PROJECT = '/work/project'
OTHER_PROJECT = '/work/other'
COORDINATOR = 'coordinator-project'


class _Store:
    """Scheduler rows with the storage claim semantics (row lock, enabled, 55 s window)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.lock = threading.Lock()

    def query(self, operation, payload, **_kwargs):
        with self.lock:
            if operation == 'scheduler.task.get':
                row = self.rows.get(payload['task_id'])
                return dict(row) if row and row['user_id'] == payload['user_id'] else None
            if operation in ('scheduler.task.list', 'scheduler.task.list_all'):
                return [dict(row) for row in self.rows.values()
                        if (operation == 'scheduler.task.list_all' or row['user_id'] == payload['user_id'])
                        and (row['enabled'] or not payload.get('enabled_only'))]
        raise AssertionError(operation)

    def command(self, operation, payload, key=None, **_kwargs):
        with self.lock:
            if operation == 'scheduler.task.create':
                if payload['task_id'] in self.rows:
                    raise RuntimeError('duplicate scheduled task id')
                row = {'run_count': 0, 'fail_count': 0, 'last_run': '', 'last_poll_at': '',
                       'last_status': '', 'system_key': '', **payload, 'id': payload['task_id'],
                       'enabled': int(bool(payload.get('enabled', True)))}
                self.rows[row['id']] = row
                return {'applied': True, 'task': dict(row)}
            row = self.rows.get(payload.get('task_id'))
            owned = row is not None and row['user_id'] == payload['user_id']
            if operation == 'scheduler.task.update':
                if not owned:
                    return {'changed': False}
                row.update({k: (int(v) if k == 'enabled' else v) for k, v in payload.items()
                            if k not in ('task_id', 'user_id')})
                return {'changed': True}
            if operation == 'scheduler.task.delete':
                if not owned:
                    return {'deleted': False}
                del self.rows[row['id']]
                return {'deleted': True}
            if operation == 'scheduler.task.record_result':
                row.update(last_run=payload['now'], run_count=row['run_count'] + 1,
                           last_status='ok' if payload['success'] else 'failed')
                return {'changed': True}
        raise AssertionError(operation)


@pytest.fixture
def store(monkeypatch):
    store = _Store()
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda *, write=False: store)
    monkeypatch.setattr(manager_module, '_manager', None)
    return store


@pytest.fixture
def audit(monkeypatch):
    events = []
    monkeypatch.setattr('lib.scheduler.executor._roles.audit_log',
                        lambda event, **fields: events.append((event, fields)))
    return events


def _actor(role, *, conversation, project=PROJECT, assignment=None, coordinator=COORDINATOR, owner=OWNER,
           scope=None):
    return SchedulerActor(role=role, owner=owner, tenant=None, conversation_id=conversation,
                          task_id=f'task-{conversation}', turn_id=f'turn-{conversation}',
                          attempt_id=f'attempt-{conversation}', project_path=project,
                          coordinator_id=coordinator, binding_revision=1, assignment_id=assignment,
                          scope=scope)


WORKER = _actor('worker', conversation='worker-a', assignment='assignment-a')
OTHER_WORKER = _actor('worker', conversation='worker-b', assignment='assignment-b')
LEAD = _actor('coordinator', conversation=COORDINATOR)
OTHER_LEAD = _actor('coordinator', conversation='coordinator-other', project=OTHER_PROJECT,
                    coordinator='coordinator-other')


def _call(actor, tool, task=None, **arguments):
    return execute_role_scheduler_tool(actor, tool, dict(arguments), task=task or {})


def _soon(minutes=30):
    return 'once:' + (datetime.now() + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M')


def _wake(actor, key='check-1', **extra):
    arguments = {'name': 'Check the eval job', 'schedule': _soon(),
                 'purpose': 'Job 42 should have finished; read its log.', 'idempotency_key': key, **extra}
    return _call(actor, 'schedule_create', **arguments)


def _only_wake(store):
    [row] = [row for row in store.rows.values() if row['task_type'] == 'project_wake']
    return row


def _failed(result, code):
    assert tool_result_failure_verdict(result) == 'error', result
    assert result.startswith('Error:') and f'(code: {code})' in result, result


def _owner_row(store, task_id, task_type, *, system_key=''):
    store.rows[task_id] = {'id': task_id, 'user_id': OWNER, 'name': task_id, 'schedule': '0 2 * * *',
                           'task_type': task_type, 'command': 'informational', 'enabled': 1,
                           'run_count': 0, 'last_run': '', 'last_poll_at': '', 'system_key': system_key}
    return store.rows[task_id]


# ── Create, list, pause, resume and cancel within scope ─────────────────────────


def test_a_worker_schedules_a_wake_of_its_coordinator_that_records_its_grant(store, audit):
    result = _wake(WORKER)

    assert tool_result_failure_verdict(result) is None and result.startswith('Wake scheduled')
    row = _only_wake(store)
    assert row['target_conv_id'] == COORDINATOR and row['source_conv_id'] == 'worker-a'
    grant = read_grant(row)
    assert grant['projectPath'] == PROJECT and grant['coordinatorId'] == COORDINATOR
    assert grant['creator'] == {'role': 'worker', 'conversationId': 'worker-a', 'taskId': 'task-worker-a',
                                'turnId': 'turn-worker-a', 'attemptId': 'attempt-worker-a',
                                'assignmentId': 'assignment-a', 'requestAttemptId': None}
    assert grant['reason'] == 'Job 42 should have finished; read its log.'
    assert [(event, fields['action'], fields['outcome']) for event, fields in audit] == [
        ('scheduled_task_controlled', 'create', 'created')]
    assert audit[0][1]['assignment'] == 'assignment-a' and audit[0][1]['project'] == PROJECT


def test_a_worker_lists_pauses_resumes_and_cancels_its_own_wake(store, audit):
    _wake(WORKER)
    task_id = _only_wake(store)['id']

    listing = _call(WORKER, 'schedule_list', include_disabled=True)
    assert f'[{task_id}]' in listing and 'project [yours]' in listing
    assert _call(WORKER, 'schedule_manage', action='disable', task_id=task_id) == f'Task {task_id} paused.'
    assert store.rows[task_id]['enabled'] == 0
    assert _call(WORKER, 'schedule_manage', action='enable', task_id=task_id) == f'Task {task_id} resumed.'
    assert store.rows[task_id]['enabled'] == 1
    assert _call(WORKER, 'schedule_manage', action='delete', task_id=task_id) == f'Task {task_id} cancelled.'
    assert task_id not in store.rows
    assert [fields['action'] for event, fields in audit if event == 'scheduled_task_controlled'] == [
        'create', 'disable', 'enable', 'delete']


def test_creation_is_idempotent_by_key_and_refuses_a_different_payload(store):
    first = _wake(WORKER)
    replay = _wake(WORKER)
    conflict = _wake(WORKER, purpose='Something else entirely.')

    assert first.startswith('Wake scheduled') and replay.startswith('Already scheduled')
    assert len(store.rows) == 1
    _failed(conflict, 'scheduler_idempotency_conflict')


def test_a_worker_wake_is_one_shot(store):
    _failed(_wake(WORKER, schedule='*/30 * * * *'), 'scheduler_worker_one_shot')
    assert store.rows == {}


def test_a_coordinator_schedules_recurring_check_ins_and_controls_every_wake_of_its_project(store):
    _wake(WORKER)
    worker_wake = _only_wake(store)['id']
    created = _call(LEAD, 'schedule_create', name='Morning review', schedule='7 9 * * 1-5',
                    purpose='Review open Workers.', idempotency_key='morning')

    assert created.startswith('Wake scheduled')
    assert _call(LEAD, 'schedule_manage', action='disable', task_id=worker_wake) == f'Task {worker_wake} paused.'
    updated = _call(LEAD, 'schedule_manage', action='update', task_id=worker_wake,
                    updates={'schedule': _soon(90), 'name': 'Later check'})
    assert updated.startswith(f'Task {worker_wake} updated') and store.rows[worker_wake]['name'] == 'Later check'


# ── Out-of-scope calls are refused, fail, and change nothing ────────────────────


def test_a_worker_cannot_control_another_workers_wake(store, audit):
    _wake(WORKER)
    task_id = _only_wake(store)['id']
    before = dict(store.rows[task_id])

    for action in ('disable', 'delete', 'run'):
        _failed(_call(OTHER_WORKER, 'schedule_manage', action=action, task_id=task_id), 'scheduler_out_of_grant')
    assert store.rows[task_id] == before
    assert 'project [yours]' not in _call(OTHER_WORKER, 'schedule_list')
    refused = [fields for event, fields in audit if event == 'scheduled_task_refused']
    assert [fields['code'] for fields in refused] == ['scheduler_out_of_grant'] * 3
    assert refused[0]['assignment'] == 'assignment-b' and refused[0]['task_id'] == task_id


def test_a_coordinator_cannot_control_another_projects_wake(store):
    _wake(WORKER)
    task_id = _only_wake(store)['id']

    _failed(_call(OTHER_LEAD, 'schedule_manage', action='disable', task_id=task_id), 'scheduler_out_of_project')
    assert store.rows[task_id]['enabled'] == 1


@pytest.mark.parametrize('task_type,scope', [
    ('storage_backup', 'system'), ('optimizer', 'system'), ('router_watchdog_review', 'service'),
    ('coordinator_commitment_due', 'service'), ('prompt', 'owner'), ('agent', 'owner'),
    ('unknown_future_type', 'system'),
])
def test_owner_service_and_system_rows_are_refused_to_project_roles(store, task_type, scope):
    row = _owner_row(store, f'row-{task_type}', task_type)
    before = dict(row)

    for actor in (WORKER, LEAD):
        for action in ('disable', 'enable', 'delete', 'run', 'update'):
            _failed(_call(actor, 'schedule_manage', action=action, task_id=row['id'], updates={'name': 'x'}),
                    f'scheduler_{scope}_scope')
    assert store.rows[row['id']] == before


def test_an_update_never_changes_a_wakes_grant(store):
    _wake(WORKER)
    task_id = _only_wake(store)['id']
    command = store.rows[task_id]['command']

    for field in ('command', 'task_type', 'target_conv_id', 'enabled'):
        _failed(_call(LEAD, 'schedule_manage', action='update', task_id=task_id, updates={field: 'x'}),
                'scheduler_update_field')
    _failed(_call(LEAD, 'schedule_manage', action='update', task_id=task_id,
                  updates={'schedule': '*/5 * * * *'}), 'scheduler_worker_one_shot')
    assert store.rows[task_id]['command'] == command


def test_a_role_cannot_reach_readiness_watchers(store):
    for name in ('timer_create', 'timer_manage', 'await_task'):
        _failed(_call(WORKER, name, action='list'), 'scheduler_tool_unavailable')


ROUTER_ACTOR = _actor('router', conversation='router', coordinator='router')


def test_every_refusal_is_a_failed_call_that_names_its_remedy_and_changes_nothing(store, audit):
    _wake(WORKER)
    wake = _only_wake(store)['id']
    backup = 'system-7-storage-backup'
    _owner_row(store, backup, 'storage_backup', system_key='storage-backup')
    delegation = {'reason': 'The owner asked', 'source_turn_id': 'owner-turn'}
    cases = [
        (OTHER_WORKER, 'schedule_manage', {'action': 'disable', 'task_id': wake},
         'scheduler_out_of_grant', 'Ask your Coordinator'),
        (OTHER_LEAD, 'schedule_manage', {'action': 'disable', 'task_id': wake},
         'scheduler_out_of_project', 'only that project\u2019s Coordinator, the Global Router or the owner'),
        (WORKER, 'schedule_manage', {'action': 'disable', 'task_id': backup},
         'scheduler_system_scope', 'Ask your Coordinator'),
        (LEAD, 'schedule_manage', {'action': 'disable', 'task_id': backup},
         'scheduler_system_scope', 'Ask the owner, or the Global Router'),
        (ROUTER_ACTOR, 'schedule_manage', {'action': 'disable', 'task_id': backup},
         'scheduler_system_scope', 'pass owner_delegation'),
        (WORKER, 'schedule_create', {'name': 'Check the eval job', 'schedule': _soon(), 'purpose': 'Another note.',
                                     'idempotency_key': 'check-1'},
         'scheduler_idempotency_conflict', 'Use a new idempotency_key'),
        (WORKER, 'schedule_manage', {'action': 'disable', 'task_id': 'missing'},
         'scheduler_task_not_found', 'schedule_list shows the tasks'),
        (WORKER, 'schedule_create', {'name': 'n', 'schedule': '*/5 * * * *', 'purpose': 'x', 'idempotency_key': 'cron'},
         'scheduler_worker_one_shot', 'once:YYYY-MM-DD HH:MM'),
        (WORKER, 'schedule_manage', {'action': 'explode', 'task_id': wake},
         'scheduler_invalid_action', 'action must be one of'),
        (LEAD, 'schedule_manage', {'action': 'update', 'task_id': wake, 'updates': {'command': 'x'}},
         'scheduler_update_field', 'Updatable: name, schedule, description'),
        (LEAD, 'schedule_manage', {'action': 'disable', 'task_id': backup, 'idempotency_key': 'k',
                                   'owner_delegation': delegation},
         'scheduler_delegation_router_only', 'Ask the owner, or the Global Router'),
        (ROUTER_ACTOR, 'schedule_manage', {'action': 'disable', 'task_id': wake, 'idempotency_key': 'k',
                                           'owner_delegation': delegation},
         'scheduler_delegation_project_row', 'without owner_delegation'),
        (ROUTER_ACTOR, 'schedule_manage', {'action': 'delete', 'task_id': backup, 'idempotency_key': 'k',
                                           'owner_delegation': delegation},
         'scheduler_delegation_action', 'delete needs the owner'),
        (WORKER, 'timer_create', {'action': 'list'}, 'scheduler_tool_unavailable', 'with schedule_create'),
    ]
    before = {key: dict(row) for key, row in store.rows.items()}
    audit.clear()

    for actor, tool, arguments, code, remedy in cases:
        result = _call(actor, tool, **arguments)
        _failed(result, code)
        assert remedy in result and 'Nothing changed.' in result, (code, result)
    assert store.rows == before
    assert [fields['code'] for event, fields in audit] == [case[3] for case in cases]
    assert {event for event, _fields in audit} == {'scheduled_task_refused'}


# ── System rows: only the owner, or the Global Router under R9 delegation ──────


def _router(d):
    return _actor('router', conversation='router', project=d.scope['project_path'], coordinator='router',
                  scope=d.scope)


def _delegated(d, action='disable', key='pause-backup', **authority):
    return _call(_router(d), 'schedule_manage', task=d.task, action=action,
                 task_id='system-7-storage-backup', idempotency_key=key,
                 owner_delegation={'reason': 'The owner asked to pause the backup tonight',
                                   'source_turn_id': 'owner-turn', **authority})


def test_a_system_task_is_refused_without_delegation_and_paused_under_it(d, store, audit):
    backup = _owner_row(store, 'system-7-storage-backup', 'storage_backup', system_key='storage-backup')

    _failed(_call(_router(d), 'schedule_manage', action='disable', task_id=backup['id']), 'scheduler_system_scope')
    _failed(_delegated(d), 'owner_delegation_not_granted')
    assert backup['enabled'] == 1 and d.audit() == []

    d.grant()
    paused = _delegated(d)
    assert tool_result_failure_verdict(paused) is None and paused.startswith(
        'Task system-7-storage-backup paused under the owner\u2019s delegation')
    assert backup['enabled'] == 0
    [record] = d.audit()
    assert (record['scope'], record['status'], record['target']) == ('scheduler', 'completed', {
        'kind': 'scheduled_task', 'taskId': 'system-7-storage-backup', 'taskScope': 'system',
        'action': 'disable'})
    assert record['trigger']['kind'] == 'owner_message' and record['reason'].startswith('The owner asked')
    # A retry with the same key never acts twice; another key resumes.
    assert _delegated(d).startswith('Already done') and len(d.audit()) == 1
    assert _delegated(d, action='enable', key='resume-backup').startswith('Task system-7-storage-backup resumed')
    assert backup['enabled'] == 1
    from lib.router.contract import validate
    for record in d.audit():
        validate(record, 'ownerDelegatedAction')
    assert ('scheduled_task_controlled', 'disable') in [(event, fields['action']) for event, fields in audit]


def _rewrite_grant(**fields):
    from lib import lifecycle_approval as gate
    with open(gate._APPROVALS_FILE) as handle:
        document = json.load(handle)
    record = document['delegations'][str(OWNER)]
    for name, value in fields.items():
        if value is None:
            record.pop(name, None)
        else:
            record[name] = value
    with open(gate._APPROVALS_FILE, 'w') as handle:
        json.dump(document, handle)


def test_a_full_grant_from_before_the_scheduler_scope_covers_it_with_no_owner_action(d, store):
    """The owner's 11-06 grant (the switch's whole list of its day) covers the new scope."""
    backup = _owner_row(store, 'system-7-storage-backup', 'storage_backup', system_key='storage-backup')
    d.grant()
    _rewrite_grant(coverage=None, scopes=['restart', 'conversation_control'])

    paused = _delegated(d)
    assert tool_result_failure_verdict(paused) is None and backup['enabled'] == 0
    [record] = d.audit()
    assert (record['scope'], record['status']) == ('scheduler', 'completed')


def test_an_enumerated_grant_without_the_scheduler_scope_stays_narrow(d, store):
    _owner_row(store, 'system-7-storage-backup', 'storage_backup', system_key='storage-backup')
    d.grant()
    _rewrite_grant(coverage='enumerated', scopes=['restart', 'conversation_control'])

    refused = _delegated(d)
    _failed(refused, 'owner_delegation_scope')
    assert 'covers only restart, conversation_control' in refused and 'Act for me' in refused
    assert store.rows['system-7-storage-backup']['enabled'] == 1


def test_delegation_never_reaches_beyond_pause_and_resume_or_project_wakes(d, store):
    _owner_row(store, 'system-7-storage-backup', 'storage_backup', system_key='storage-backup')
    d.grant()

    for action in ('delete', 'run', 'update'):
        _failed(_delegated(d, action=action, key=action), 'scheduler_delegation_action')
    _failed(_call(LEAD, 'schedule_manage', action='disable', task_id='system-7-storage-backup',
                  idempotency_key='lead', owner_delegation={'reason': 'x', 'source_turn_id': 'owner-turn'}),
            'scheduler_delegation_router_only')
    assert store.rows['system-7-storage-backup']['enabled'] == 1 and d.audit() == []


# ── A refusal is a failed call on every surface ────────────────────────────────


def test_the_owner_surface_reports_its_errors_as_failed_calls(store):
    missing = execute_scheduler_tool('schedule_manage', {'action': 'delete', 'task_id': 'missing',
                                                         '_user_id': OWNER})
    listed = execute_scheduler_tool('schedule_list', {'_user_id': OWNER})

    assert tool_result_failure_verdict(missing) == 'error' and missing == 'Error: Task not found'
    assert tool_result_failure_verdict(listed) is None


def test_a_refused_role_call_reads_failed_in_the_round_transcript(store, monkeypatch):
    from lib.conv_ref import _format_tool_rounds
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    _owner_row(store, 'system-7-storage-backup', 'storage_backup')
    monkeypatch.setattr('lib.scheduler.authority.resolve_actor', lambda task: WORKER)
    task = _mk_task(_userId=OWNER, config={'autoApply': True})

    def run(arguments):
        parsed = _mk_tc('call-manage', 'schedule_manage', 0, args=arguments)
        task['toolRounds'] = [parsed[5]]
        messages = []
        execute_tool_pipeline(task, [parsed], task['config'], None, False, list(SCHEDULER_TOOLS),
                              messages, [], 0, 'test-model')
        return parsed[5]

    refused = run({'action': 'disable', 'task_id': 'system-7-storage-backup'})
    assert refused['status'] == 'error', refused
    assert '[error]' in _format_tool_rounds([refused]) and '[done]' not in _format_tool_rounds([refused])
    assert store.rows['system-7-storage-backup']['enabled'] == 1


# ── Actor resolution reads the grant, never an argument ────────────────────────


def test_the_actor_comes_from_the_live_grant(monkeypatch):
    import lib.project_dispatch.admission as admission
    task = {'id': 'worker-task', 'convId': 'worker-a', '_userId': OWNER, '_turnId': 't', '_attemptId': 'a',
            'config': {admission.KEY: {'owner': OWNER}}}
    assignment = {'request': {'projectPath': PROJECT, 'coordinatorId': COORDINATOR,
                              'profile': {'mode': 'project', 'tools': ['read_files', 'run_command']},
                              'delegation': {'attemptId': 'request-attempt'}},
                  'grant': {'bindingRevision': 3}}
    monkeypatch.setattr(admission, 'validated_dispatch',
                        lambda task: ({'assignmentId': 'assignment-a'}, assignment))
    monkeypatch.setattr(admission, 'profile_for', lambda row: row['request']['profile'])

    worker = resolve_actor(task)
    assert (worker.role, worker.project_path, worker.coordinator_id, worker.assignment_id,
            worker.binding_revision) == ('worker', PROJECT, COORDINATOR, 'assignment-a', 3)
    # Code execution and the delegating request come from the grant, never an argument.
    assert worker.code_execution is True and worker.request_attempt_id == 'request-attempt'

    monkeypatch.setattr(admission, 'profile_for', lambda row: {'mode': 'files'})
    with pytest.raises(PermissionError):
        resolve_actor(task)

    import lib.tasks_pkg.coordinator_policy as policy
    scope = {'project_path': PROJECT, 'binding_revision': 2, 'conversation_id': COORDINATOR, 'user_id': OWNER}
    monkeypatch.setattr(policy, 'coordinator_scope', lambda task, project_path=None: dict(scope))
    monkeypatch.setattr('lib.router.policy.is_router_scope', lambda scope: False)
    monkeypatch.setattr('lib.router.watchdog.binding.is_watchdog_scope', lambda scope: False)
    lead = resolve_actor({'id': 'lead-task', 'convId': COORDINATOR, '_userId': OWNER})
    assert (lead.role, lead.project_path, lead.coordinator_id) == ('coordinator', PROJECT, COORDINATOR)
    monkeypatch.setattr(policy, 'coordinator_scope', lambda task, project_path=None: None)
    assert resolve_actor({'id': 'chat', 'convId': 'chat', '_userId': OWNER}).role == 'owner'


# ── A due wake delivers once and re-reads its premise ──────────────────────────


class _Bindings:
    def __init__(self, conversation=COORDINATOR, revision=1):
        self.value = {'projectPath': PROJECT, 'conversationId': conversation, 'revision': revision,
                      'availability': 'available'}

    def read(self, path):
        return dict(self.value)


class _Assignments:
    def __init__(self, revoked=False):
        self.revoked = revoked

    def read(self, assignment_id):
        return {'id': assignment_id, 'grant': {'revoked': self.revoked}}


def _dispatcher(calls, disposition='started'):
    def dispatch(conversation, message, tools, **kwargs):
        calls.append({'conversation': conversation, 'message': message, **kwargs})
        return ScheduledTurnDispatch(disposition, 'task')
    return dispatch


def test_a_due_wake_delivers_one_nonhuman_notice_to_its_coordinator(store):
    from lib.scheduler.project_wake import run_project_wake
    _wake(WORKER)
    row = _only_wake(store)
    calls = []

    for _ in range(2):
        ok, result = run_project_wake(row, bindings=_Bindings(), assignments=_Assignments(),
                                      dispatch=_dispatcher(calls))
        assert ok and result == f'Woke {COORDINATOR} (started).'
    first, second = calls
    assert first['conversation'] == COORDINATOR and first['inject_mode'] == 'steer'
    # One occurrence, one command identity: the conversation command service runs it once.
    assert first['command_id'] == second['command_id']
    content = first['message']['content']
    assert content.startswith('[Scheduled wake: nonhuman notice') and 'Job 42 should have finished' in content
    assert 'assignment-a' in content and first['message']['_initiator'] == 'proactive'


@pytest.mark.parametrize('premise', ['rebound', 'revoked', 'conversation_gone'])
def test_a_wake_whose_premise_changed_is_inert_and_disabled(store, premise):
    from lib.scheduler.project_wake import run_project_wake
    _wake(WORKER)
    row = _only_wake(store)
    calls = []
    bindings = _Bindings(conversation='coordinator-new') if premise == 'rebound' else _Bindings()
    assignments = _Assignments(revoked=premise == 'revoked')
    disposition = 'target_missing' if premise == 'conversation_gone' else 'started'

    ok, result = run_project_wake(row, bindings=bindings, assignments=assignments,
                                  dispatch=_dispatcher(calls, disposition))

    assert ok and result.startswith('Inert')
    assert len(calls) == (1 if premise == 'conversation_gone' else 0)
    assert store.rows[row['id']]['enabled'] == 0


def test_the_tick_hands_a_due_project_wake_to_its_handler(store, monkeypatch):
    _wake(WORKER)
    row = _only_wake(store)
    seen = []
    monkeypatch.setattr('lib.scheduler.project_wake.run_project_wake',
                        lambda task: seen.append(task['id']) or (True, 'woke'))

    assert ScheduledTaskManager()._execute_task(row) == (True, 'woke') and seen == [row['id']]


# ── Surfaces and policy ─────────────────────────────────────────────────────────


def test_project_roles_are_issued_the_scheduler_family():
    from lib.project_dispatch.repository import CONTRACT
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    family = {'schedule_create', 'schedule_list', 'schedule_manage'}

    assert not family & set(CONTRACT['workerDeniedTools'])
    assert {'timer_create', 'timer_manage'} <= set(CONTRACT['workerDeniedTools'])
    assert family <= COORDINATOR_TOOLS
    # A continuation's record lists no scheduler name; the continuation holds its
    # role's catalog, family included, whenever its record was frozen (docs/ROUTER.md R15).
    assert not family & set(CONTRACT['continuationTools'])
    from lib.project_dispatch.scope import granted_tools
    profile = {'mode': 'coordinator', 'tools': CONTRACT['continuationTools']}
    grant = {'request': {'projectPath': '/project', 'coordinatorId': 'coordinator', 'profile': profile},
             'grant': {'owner': 7, 'bindingRevision': 1}}
    assert family <= granted_tools(profile, assignment=grant)


@pytest.mark.parametrize('role', ['worker', 'coordinator', 'router'])
def test_a_role_sees_the_projected_scheduler_schemas(role):
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS, project_role_scheduler_surface
    from lib.tools.contracts import compile_execution_contract_documents
    catalog = [dict(tool) for tool in SCHEDULER_TOOLS]
    cfg = {'_executableToolCatalog': list(catalog),
           '_toolContractDocumentsByName': compile_execution_contract_documents(catalog, namespace='scheduler'),
           '_requiredDirectToolNames': ['timer_create', 'schedule_list']}

    tools = project_role_scheduler_surface(cfg, catalog, role=role)

    names = [tool['function']['name'] for tool in tools]
    assert names == ['schedule_create', 'schedule_list', 'schedule_manage']
    assert [tool['function']['name'] for tool in cfg['_executableToolCatalog']] == names
    create = tools[0]['function']['parameters']
    assert create['required'] == ['name', 'schedule', 'purpose', 'idempotency_key']
    manage = cfg['_toolContractDocumentsByName']['schedule_manage']['arguments_schema']
    assert manage['properties']['action']['enum'] == ['disable', 'enable', 'delete', 'run', 'update', 'log']
    assert ('owner_delegation' in manage['properties']) is (role == 'router')
    assert '$ref' not in json.dumps([tool['function'] for tool in tools])  # tool schemas carry no $defs
    assert 'timer_create' not in cfg['_toolContractDocumentsByName']
    assert cfg['_requiredDirectToolNames'] == ['schedule_list']
