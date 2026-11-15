"""A due commitment wakes its Coordinator: an idle one starts a Turn, a busy one is steered.

Liveness evidence of 2026-11-10: the Global Router's board items due 18:00 and
18:30 CST never started a turn, and a Coordinator's 18:00 promise never woke it
(COORD-LIVENESS root-causes those misses). This is the end-to-end contract the
platform owes for every Coordinator (docs/PROJECT_COORDINATION.md, "Commitments
on the task board"; docs/modules/scheduling_ops.md): the board write arms one
owner-visible ``coordinator_commitment_due`` row; the scheduler tick claims it
at its minute; its handler wakes the Coordinator once with a nonhuman notice
through the conversation turn command (``inject_mode='steer'``). An idle
Coordinator gets a new Turn whose executor starts; a busy one gets a durable
steer on its live Turn that its next round boundary hands to the model. Board,
binding, scheduler rows, Turns and steers are real storage; only model
execution is replaced.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid

import pytest

from lib.identity import PrincipalContext
import lib.scheduler.manager as manager_module
from lib.scheduler.manager import ScheduledTaskManager

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

OWNER = 1
PROMISE = 'Report the crash verdict'
NOTICE_HEADER = '[Commitment due: nonhuman notice'


class _Clock(datetime):
    current = datetime(2026, 11, 10, 17, 58)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture(params=['global_router', 'project_coordinator'])
def world(request, tmp_path, monkeypatch):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.binding import global_router
    from tests.support.conversation_workers import observe_admissions
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    if request.param == 'global_router':
        binding = global_router(OWNER, create=True)
    else:
        project = tmp_path / 'project'
        project.mkdir()
        binding = ProjectCoordinationRepository(OWNER).provision(
            str(project), title='Project Router', settings=None, command_id='liveness-' + uuid.uuid4().hex)
    assert binding['availability'] == 'available'
    manager = ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='scheduler-liveness', owner_user_id=OWNER, scopes={'scheduler:run'})
    monkeypatch.setattr(manager_module, '_manager', manager)
    monkeypatch.setattr(manager_module, 'datetime', _Clock)
    return {'binding': binding, 'manager': manager, 'admissions': observe_admissions(monkeypatch)}


def _scope(world):
    binding = world['binding']
    return {'owner': OWNER, 'projectPath': binding['projectPath'], 'coordinatorId': binding['conversationId'],
            'bindingRevision': binding['revision']}


def _promise_due_now(world):
    """The Coordinator promised a report on its board two minutes before its due time, which has passed.

    Returns the armed scheduler row.
    """
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    due = int(time.time()) - 20
    written = due - 120
    _Clock.current = datetime.fromtimestamp(written)
    CoordinatorTaskProgress(OWNER, world['binding']['projectPath'], clock=lambda: written).publish(_scope(world), {
        'summary': 'One promise to the owner.',
        'tasks': [{'title': PROMISE, 'status': 'in_progress',
                   'due': datetime.fromtimestamp(due, tz=timezone.utc).isoformat(timespec='seconds')}]})
    rows = [row for row in world['manager'].list_tasks(user_id=OWNER, include_disabled=True)
            if row['task_type'] == 'coordinator_commitment_due'
            and row['target_conv_id'] == world['binding']['conversationId'] and row['enabled']]
    assert len(rows) == 1, rows
    return rows[0]


def _tick_at(world, row, seconds_after=5):
    """One scheduler tick a few seconds after the row's minute; joins what it started."""
    minute = datetime.fromisoformat(row['schedule'][len('once:'):])
    _Clock.current = minute.replace(second=seconds_after)
    world['manager']._check_and_run_due_tasks()
    assert world['manager'].stop(timeout=30)


def _receipt(world):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    head = CoordinatorTaskProgress(OWNER, world['binding']['projectPath'])._head()['value']
    [receipt] = [row for row in head['commitments'] if row['title'] == PROMISE]
    return receipt.get('wake')


def test_a_due_commitment_starts_a_turn_in_an_idle_coordinator(world, monkeypatch):
    from lib import turn_lifecycle
    from tests.support.conversation_workers import run_admitted_attempt
    row = _promise_due_now(world)

    _tick_at(world, row)

    [admission] = world['admissions']
    assert admission['conversationId'] == world['binding']['conversationId']
    reply = turn_lifecycle.get_turn(admission['conversationId'], admission['config']['_turnId'], user_id=OWNER)
    notice = turn_lifecycle.get_turn(admission['conversationId'], reply['parentTurnId'], user_id=OWNER)
    assert notice['projection']['content'].startswith(NOTICE_HEADER)
    assert PROMISE in notice['projection']['content']
    from lib.turn_initiation import INITIATOR_PROACTIVE, resolve_turn_initiator
    assert resolve_turn_initiator(notice) == INITIATOR_PROACTIVE
    wake = _receipt(world)
    assert (wake['via'], wake['disposition']) == ('notice', 'started')
    assert wake['commandId'].startswith('commitment-due:')
    # The started Turn's executor runs (the model is the only stand-in).
    settled, probed = run_admitted_attempt(OWNER, admission['config']['_attemptId'], monkeypatch,
                                           lambda task: task['convId'])
    assert probed == [world['binding']['conversationId']] and settled['status'] == 'completed'
    # The due time woke once: the row is spent and a later tick wakes nothing.
    assert not world['manager'].get_task(row['id'], user_id=OWNER)['enabled']
    _tick_at(world, row, seconds_after=50)
    assert len(world['admissions']) == 1


def test_a_due_commitment_reaches_a_busy_coordinator_as_a_steer_its_next_round_reads(world, monkeypatch):
    from lib import turn_lifecycle
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    from tests.support.conversation_workers import run_admitted_attempt
    conversation = world['binding']['conversationId']
    conversation_turn_commands.create_turn(conversation, OWNER, {
        'commandId': 'owner-request-' + uuid.uuid4().hex, 'inputTurn': {'content': 'Keep the eval going.'},
        'config': {'model': 'claude-opus-5.5'}}, request_started_at=time.time())
    [request] = world['admissions']

    def busy_coordinator(task):
        # While this Turn runs, its promise falls due and the tick fires.
        row = _promise_due_now(world)
        _tick_at(world, row)
        live = turn_lifecycle.get_turn(conversation, task['_turnId'], user_id=OWNER)
        steers = [record for record in live['projection'].get('_userSteerInjects') or ()
                  if str(record.get('commandId') or '').startswith('commitment-due:')]
        # The next round boundary hands the steer to the model.
        messages = [{'role': 'user', 'content': 'Keep the eval going.'}]
        drain_and_inject_inbox(task=task, messages=messages, round_num=2, tid=task['id'][:8])
        return {'steers': steers, 'next_round_input': str(messages[-1].get('content')),
                'wake': _receipt(world), 'admissions': len(world['admissions'])}

    _settled, [seen] = run_admitted_attempt(OWNER, request['config']['_attemptId'], monkeypatch,
                                            busy_coordinator)

    [steer] = seen['steers']
    assert steer['deliveryState'] == 'pending' and steer['previews'][0]['text'].startswith(NOTICE_HEADER)
    assert (seen['wake']['via'], seen['wake']['disposition']) == ('notice', 'steered')
    assert seen['wake']['commandId'] == steer['commandId']
    assert NOTICE_HEADER in seen['next_round_input'] and PROMISE in seen['next_round_input']
    # A busy Coordinator gets no second Turn for the same promise.
    assert seen['admissions'] == 1
