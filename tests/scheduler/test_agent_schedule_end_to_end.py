"""The Hope liveness patrol probe, scheduled by agents end to end (SCHEDULE-AUTHORITY-1).

The schedule the owner had to paste by hand on 2026-11-13 is created through
the agents' own tool path: as the scheduler tool handler runs a Worker's
``schedule_create``, its actor is resolved from its grant, the row lands in
SQLite storage, the owner's routes list it with its creator and purpose, and
the scheduler tick runs it at its due minute and records the run. A run counts
as passing only when the probe exits 0, which the probe does only with
``authState=ready`` (HOPE-PATROL-1); a non-ready run is a failed run on the row
and in its history. A Coordinator holds no code execution, so its own attempt
to create the command task is refused with the remedy (delegate a Worker),
while its prompt and wake rows go through. Owner: docs/modules/scheduling_ops.md,
"Agent-created schedules".
"""
from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

import lib.scheduler.manager as manager_module
from tests.scheduler.test_agent_scheduling import (  # noqa: F401 (fixtures)
    COORDINATOR, OWNER, audit, creators, owner_http, storage, workspace,
)

pytestmark = pytest.mark.unit

PATROL_NAME = 'Hope liveness patrol: activity probe'
PATROL_SCHEDULE = '13,43 * * * *'
PATROL_PURPOSE = ('Hope liveness patrol: refresh observations.json every 30 minutes so the '
                  'patrol\u2019s hang rule reads a fresh 60-minute window.')
# Exit semantics of the probe as HOPE-PATROL-1 ships it: 0 only with authState=ready.
_STAND_IN_PROBE = '''
import argparse, json, pathlib, sys
parser = argparse.ArgumentParser()
for flag in ('--dir', '--owner', '--mis', '--project'):
    parser.add_argument(flag, required=True)
args = parser.parse_args()
directory = pathlib.Path(args.dir)
state = (directory / 'auth_state').read_text().strip()
(directory / 'observations.json').write_text(json.dumps({'authState': state, 'jobs': {}}))
print(json.dumps({'authState': state, 'jobs': 0, 'errors': []}))
sys.exit(0 if state == 'ready' else 3)
'''


@pytest.fixture
def patrol(tmp_path):
    directory = tmp_path / 'patrol'
    package = directory / 'bin' / 'hope_patrol'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'probe.py').write_text(_STAND_IN_PROBE)
    (directory / 'auth_state').write_text('login_required')
    command = (f'PYTHONPATH={directory}/bin {sys.executable} -m hope_patrol.probe --dir {directory} '
               '--owner 1500000196 --mis your-username --project your-user')
    return directory, command


def _grant_bound(task, *, workspace):
    """Resolve the actor with the real resolve_actor over a validated Worker grant."""
    from lib.scheduler import authority
    assignment = {'request': {'projectPath': workspace, 'coordinatorId': COORDINATOR,
                              'profile': {'mode': 'project',
                                          'tools': ['read_files', 'run_command', 'schedule_create',
                                                    'schedule_list', 'schedule_manage']},
                              'delegation': {'attemptId': 'coordinator-request-attempt'}},
                  'grant': {'bindingRevision': 1}}
    with mock.patch('lib.project_dispatch.admission.is_dispatch_task', lambda task: True), \
            mock.patch('lib.project_dispatch.admission.validated_dispatch',
                       lambda task: ({'assignmentId': 'assignment-patrol'}, assignment)), \
            mock.patch('lib.project_dispatch.admission.profile_for',
                       lambda row: row['request']['profile']):
        return _REAL_RESOLVE(task)


def _coordinator_bound(task, *, workspace):
    scope = {'project_path': workspace, 'binding_revision': 1, 'conversation_id': COORDINATOR,
             'user_id': OWNER}
    with mock.patch('lib.project_dispatch.admission.is_dispatch_task', lambda task: False), \
            mock.patch('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                       lambda task, project_path=None: dict(scope)), \
            mock.patch('lib.router.policy.is_router_scope', lambda scope: False), \
            mock.patch('lib.router.watchdog.binding.is_watchdog_scope', lambda scope: False):
        return _REAL_RESOLVE(task)


from lib.scheduler.authority import resolve_actor as _REAL_RESOLVE  # noqa: E402


def _tool_call(task, name, arguments, call_id):
    """One model tool call as the scheduler tool handler runs it: actor from the task, role executor."""
    from lib.scheduler import authority
    from lib.scheduler.executor._roles import execute_role_scheduler_tool
    from lib.tools.result_value import tool_result_failure_verdict
    content = execute_role_scheduler_tool(authority.resolve_actor(task), name, dict(arguments), task=task)
    return {'call': call_id, 'status': 'error' if tool_result_failure_verdict(content) == 'error' else 'done',
            'content': str(content)}


def _run_due(monkeypatch, minute):
    from datetime import datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 13, 22, minute, 5)
    monkeypatch.setattr(manager_module, 'datetime', Clock)
    manager = manager_module.get_scheduler()  # the instance the owner's routes read
    manager._check_and_run_due_tasks()
    for thread in list(manager._run_threads.values()):
        thread.join(timeout=60)
    return manager


def test_agents_schedule_the_patrol_probe_and_its_first_run_is_reported(
        storage, audit, creators, workspace, owner_http, patrol, monkeypatch):
    directory, command = patrol
    exact = {'name': PATROL_NAME, 'schedule': PATROL_SCHEDULE, 'task_type': 'command',
             'command': command, 'max_runtime': 300, 'purpose': PATROL_PURPOSE,
             'idempotency_key': 'hope-liveness-probe'}

    # The Coordinator holds no code execution: refused, with the remedy, nothing created.
    monkeypatch.setattr('lib.scheduler.authority.resolve_actor',
                        lambda task: _coordinator_bound(task, workspace=workspace))
    lead = {'id': 'coordinator-task', 'convId': COORDINATOR, '_userId': OWNER, '_turnId': 'lead-turn',
            '_attemptId': 'lead-attempt', 'config': {}}
    refused = _tool_call(lead, 'schedule_create', exact, 'call-lead-probe')
    assert refused['status'] == 'error', refused
    assert 'scheduler_capability_code_execution' in refused['content'] and 'Delegate a Worker' in refused['content']
    wake = _tool_call(lead, 'schedule_create', {
        'name': 'Read the patrol observations', 'schedule': '15,45 * * * *',
        'purpose': 'Read observations.json after each probe and report hung jobs.',
        'idempotency_key': 'patrol-readout'}, 'call-lead-wake')
    assert wake['status'] == 'done', wake

    # The Worker it delegates holds run_command and creates the exact schedule.
    monkeypatch.setattr('lib.scheduler.authority.resolve_actor',
                        lambda task: _grant_bound(task, workspace=workspace))
    worker = {'id': 'worker-task', 'convId': 'worker-patrol', '_userId': OWNER, '_turnId': 'worker-turn',
              '_attemptId': 'worker-attempt', 'config': {}}
    created = _tool_call(worker, 'schedule_create', exact, 'call-worker-probe')
    assert created['status'] == 'done', created
    replayed = _tool_call(worker, 'schedule_create', exact, 'call-worker-probe-retry')
    assert 'nothing changed' in replayed['content']

    status, body = owner_http('get', '/api/v1/scheduler/tasks')
    assert status == 200
    [probe] = [row for row in body['tasks'] if row['task_type'] == 'command']
    assert (probe['name'], probe['schedule'], probe['command'], probe['max_runtime']) == (
        PATROL_NAME, PATROL_SCHEDULE, command, 300)
    assert probe['purpose'] == PATROL_PURPOSE
    assert probe['created_by']['role'] == 'worker'
    assert probe['created_by']['conversationId'] == 'worker-patrol'
    assert probe['created_by']['assignmentId'] == 'assignment-patrol'
    assert probe['created_by']['requestAttemptId'] == 'coordinator-request-attempt'
    assert probe['created_by']['leadConversationId'] == COORDINATOR
    [readout] = [row for row in body['tasks'] if row['task_type'] == 'project_wake']
    assert readout['created_by']['role'] == 'coordinator'

    # 22:13 is due: the tick runs the probe. Without a ready session it fails, and says so.
    _run_due(monkeypatch, 13)
    status, body = owner_http('get', f'/api/v1/scheduler/tasks/{probe["id"]}')
    first = body['task']
    assert (first['run_count'], first['fail_count'], first['last_status']) == (1, 1, 'failed')
    assert '"authState": "login_required"' in first['last_result']
    assert json.loads((directory / 'observations.json').read_text())['authState'] == 'login_required'

    # With a ready session the 22:43 run passes.
    (directory / 'auth_state').write_text('ready')
    _run_due(monkeypatch, 43)
    status, body = owner_http('get', f'/api/v1/scheduler/tasks/{probe["id"]}')
    assert (body['task']['run_count'], body['task']['last_status']) == (2, 'ok')
    status, body = owner_http('get', f'/api/v1/scheduler/tasks/{probe["id"]}/history')
    assert status == 200 and [entry['success'] for entry in body['history']] == [False, True]

    created_events = [fields for event, fields in audit if event == 'scheduled_task_created']
    assert {(fields['created_by_role'], fields['task_type']) for fields in created_events} == {
        ('coordinator', 'project_wake'), ('worker', 'command')}
    print(json.dumps(owner_http.transcript, ensure_ascii=False, indent=1))
