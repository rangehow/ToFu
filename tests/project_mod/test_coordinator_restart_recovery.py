"""Coordinator wake recovery after a restart: every proposal shape fails alone,
and no automated path resumes a Turn that a later input superseded.

Incident 2026-11-10 (signals 26b93632, d2529646, cc9fffc7): after the 04:37
restart the parallel Coordinator's restart-paused wake (Turn 060bc407) had been
moved past by the owner's queued input. Resuming it raised
LifecycleConflict(database_conflict): ``turn.attempt.create`` refuses to delete
a LIVE later Turn of the lane, and once those Turns settle it deletes them. The
recovery handler then read ``proposal['source']`` from the ``{'existing': row}``
shape, so ``KeyError: 'source'`` aborted the project's whole pass and hid the
conflict; a failure before the first proposal raised UnboundLocalError instead.

Incident 2026-11-11 18:28: the parallel Router's request Turn 8c1d64fe had posted
a progress report mid-turn. Recovery read the report as the lane tail and never
continued the Turn. A resume preserves every later Turn, so a Turn followed only by
its own reports continues in place, with its own authority.

Docs: docs/PROJECT_COORDINATION.md "Restart-paused wakes resume in place unless a
later input superseded them"; the shared rule is ``restart_resume.lane_tail_verdict``.
"""
from copy import deepcopy

import pytest

from tests.project_mod.test_coordinator_continuation import (
    _restart_pause, controls, delegate, end, ready, run as run)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch import continuation, pending_wakes, recovery
from lib.project_dispatch.repository import DispatchContention
from lib.turn_lifecycle import LifecycleConflict

pytestmark = pytest.mark.unit

__all__ = ('runtime', 'base_runtime', 'context', 'chats', 'storage')


# --- every proposal shape fails alone -----------------------------------------------------------

def _source_row(identity, coordinator):
    return {'id': 'row-' + identity, 'request': {
        'coordinatorId': coordinator, 'recipientId': coordinator,
        'delegation': {'attemptId': identity, 'policy': {'continuation': {'model': 'm', 'tools': []}}}}}


class _Pass:
    """The dispatch snapshot one recovery pass reads: one source row per request."""

    owner, project = 7, '/canonical/project'

    def __init__(self, sources, *, failing_loads=()):
        self.rows = [_source_row(identity, coordinator) for identity, coordinator in sources]
        self.failing_loads = set(failing_loads)
        self.authorized = []

    def clock(self):
        return 1_000.0

    def _selection(self, *, sources):
        return {'sources': sources}

    def _load(self, selection=None):
        if selection and selection['sources'][0] in self.failing_loads:
            raise DispatchContention('Concurrent/uncertain dispatch snapshot; retry reading')
        return {'assignments': deepcopy(self.rows), 'coordinatorSettlements': []}, None

    def discover(self):
        """Recovery's unwitnessed whole-project read: every row, no settled request."""
        return deepcopy(self.rows), set()

    def admission(self, assignment_id):
        return {'assignmentId': assignment_id}

    def command_identity(self, command):
        return command

    def _authorize_request(self, request, granter, command, *, check):
        self.authorized.append(command)
        if request.get('fail'):
            raise RuntimeError('authorize failed')
        return {'id': 'wake-' + command}


@pytest.fixture
def faults(monkeypatch):
    """Recorded Coordinator wake faults, without the unit fixture's progress-board read.

    The ``run`` fixture binds no task board, so every derived wake reports its
    ``commitments`` read; that seam is not under test here.
    """
    reported = []

    def record(owner, project, error, *, conversation_id=None, source_attempt_id=None, wake=None, **kwargs):
        if wake != 'commitments':
            reported.append({'owner': owner, 'project': project, 'error': type(error).__name__,
                             'conversation': conversation_id, 'source': source_attempt_id, 'wake': wake})
    monkeypatch.setattr('lib.router.watchdog.signals.report_coordinator_wake_failure', record)
    return reported


def _proposals(monkeypatch, shapes):
    monkeypatch.setattr(continuation, '_proposal', lambda repository, value, identity: deepcopy(shapes[identity]))


class _Service:
    def __init__(self, failing=()):
        self.failing, self.dispatched = set(failing), []

    def dispatch_authorized(self, binding):
        self.dispatched.append(binding['assignmentId'])
        if binding['assignmentId'] in self.failing:
            raise LifecycleConflict('database_conflict', 'This turn already has a live attempt.')
        return {'dispatch': 'accepted'}


def _episode(fault):
    """A failed source's fault is its durable pending wake's episode (pending_wakes)."""
    return fault['wake'].startswith('pending:' + pending_wakes.source_entry(fault['source']) + ':')


def test_every_proposal_shape_fails_alone_and_names_its_coordinator(monkeypatch, faults):
    """One failing proposal of each shape: the real error is reported once per
    source episode with its own Coordinator and the canonical project, and the
    pass goes on."""
    from lib.conversations import restart_resume
    repository = _Pass([('paused', 'coordinator-a'), ('request', 'coordinator-b'),
                        ('timer', 'coordinator-c'), ('new', 'coordinator-d'), ('healthy', 'coordinator-e')])
    _proposals(monkeypatch, {
        'paused': {'existing': {'id': 'paused-wake'}},
        'request': {'resume_source': 'coordinator-b'},
        'timer': {'wait_until': 2_000.0},
        'new': {'request': {'fail': True}, 'granter': {}, 'command': 'coordinator-wake:new', 'stalled': False,
                'decision': None, 'source': repository.rows[3]},
        'healthy': {'existing': {'id': 'healthy-wake'}}})

    def resume_conversation(owner, conversation_id, *, turn_id=None, attempt_id=None):
        raise RuntimeError('resume failed')
    monkeypatch.setattr(restart_resume, 'resume_conversation', resume_conversation)

    def schedule(wake_at):
        raise RuntimeError('timer failed')
    service = _Service(failing={'paused-wake'})
    outcomes = continuation.recover_coordinator(repository, service=service, schedule=schedule)

    assert outcomes == ['deferred', 'deferred', 'deferred', 'deferred', 'accepted']
    assert service.dispatched == ['paused-wake', 'healthy-wake']
    assert [(fault['conversation'], fault['source'], fault['error']) for fault in faults] == [
        ('coordinator-a', 'paused', 'LifecycleConflict'), ('coordinator-b', 'request', 'RuntimeError'),
        ('coordinator-c', 'timer', 'RuntimeError'), ('coordinator-d', 'new', 'RuntimeError')]
    assert {fault['project'] for fault in faults} == {'/canonical/project'}
    assert all(_episode(fault) for fault in faults)


def test_a_source_failing_before_its_proposal_names_its_own_coordinator(monkeypatch, faults):
    """05:08:46 shape: the first source's reread lost the head CAS before any
    proposal existed (UnboundLocalError), and a later failure must never borrow
    an earlier source's proposal."""
    repository = _Pass([('first', 'coordinator-a'), ('second', 'coordinator-b'), ('third', 'coordinator-c')],
                       failing_loads={'first', 'third'})
    _proposals(monkeypatch, {'second': {'existing': {'id': 'second-wake'}}})
    service = _Service()
    outcomes = continuation.recover_coordinator(repository, service=service, schedule=lambda wake_at: None)

    assert outcomes == ['deferred', 'accepted', 'deferred']
    assert service.dispatched == ['second-wake']
    assert [(fault['conversation'], fault['source'], fault['error']) for fault in faults] == [
        ('coordinator-a', 'first', 'DispatchContention'),
        ('coordinator-c', 'third', 'DispatchContention')]
    assert all(_episode(fault) for fault in faults)


# --- a restart-paused wake that is no longer its lane's tail ------------------------------------

def _paused_wake(r):
    """The 04:37 shape: a restart-paused wake, then the owner's queued input and its reply."""
    ready(r)
    [row] = controls(r)
    turn_id, attempt_id = row['execution']['turnId'], row['execution']['attemptId']
    _restart_pause(r, turn_id, attempt_id)
    r.turns[turn_id].update(ordinal=304, laneId='main')
    r.attempts[attempt_id]['settledAt'] = 1_004_000
    r.turns['owner-input'] = {'turnId': 'owner-input', 'conversationId': 'coordinator', 'actor': 'virtual_user',
                              'kind': 'input', 'laneId': 'main', 'ordinal': 305, 'status': 'completed',
                              'createdAt': 1_003_500}
    r.turns['owner-reply'] = {'turnId': 'owner-reply', 'conversationId': 'coordinator', 'actor': 'assistant',
                              'kind': 'reply', 'laneId': 'main', 'ordinal': 306, 'status': 'running',
                              'currentAttemptId': 'owner-reply-attempt', 'createdAt': 1_005_000}
    r.attempts['owner-reply-attempt'] = {'attemptId': 'owner-reply-attempt', 'conversationId': 'coordinator',
                                         'turnId': 'owner-reply', 'taskId': 'owner-reply-task',
                                         'status': 'running', 'createdAt': 1_005_000}
    return row


def _never_resume(r, monkeypatch):
    def resume_turn(*args, **kwargs):
        pytest.fail('a Turn a later input superseded must never be resumed: the newer input owns the lane')
    monkeypatch.setattr(r.service.commands, 'resume_turn', resume_turn, raising=False)


def _settle_later_turn(r):
    r.attempts['owner-reply-attempt'].update(status='completed', settledAt=1_006_000)
    r.turns['owner-reply'].update(status='completed')


def _assert_superseded_once(r, paused):
    rows = controls(r)
    assert [row['id'] for row in rows[:1]] == [paused['id']] and len(rows) == 2
    fresh = rows[1]['request']
    assert fresh['coordinatorWake']['previousAttemptId'] == paused['execution']['attemptId']
    assert 'superseded by Turn owner-input' in fresh['brief']
    assert paused['execution']['turnId'] in fresh['brief']


def test_a_wake_a_live_later_turn_follows_is_never_resumed(run, monkeypatch, faults):
    r = run
    paused = _paused_wake(r)
    _never_resume(r, monkeypatch)
    # The continuation is a new Turn at the lane tail; the live reply owns the lane.
    assert r.wake() == ['deferred']
    _assert_superseded_once(r, paused)
    assert faults == []
    assert r.wake() == ['deferred'] and len(controls(r)) == 2


def test_after_the_later_turn_settles_the_wake_is_still_not_resumed(run, monkeypatch, faults):
    r = run
    paused = _paused_wake(r)
    _never_resume(r, monkeypatch)
    assert r.wake() == ['deferred']
    _settle_later_turn(r)
    assert r.wake() == ['accepted']
    _assert_superseded_once(r, paused)
    fresh = controls(r)[1]
    assert fresh['execution']['turnId'] != paused['execution']['turnId']
    assert faults == []
    assert r.wake() == [] and len(controls(r)) == 2


def test_a_superseded_wake_whose_later_turn_already_settled_continues_at_the_tail(run, monkeypatch, faults):
    r = run
    paused = _paused_wake(r)
    _settle_later_turn(r)
    _never_resume(r, monkeypatch)
    assert r.wake() == ['accepted']
    _assert_superseded_once(r, paused)
    assert faults == []


def test_boot_recovery_never_resumes_a_superseded_wake(run, monkeypatch, faults):
    r = run
    paused = _paused_wake(r)
    _settle_later_turn(r)
    _never_resume(r, monkeypatch)
    stats = recovery.recover_project_dispatch(
        7, '/project', repository=r.c.repo, service=r.service,
        was_aborted_after=lambda *args: False, clock=lambda: r.c.now[0])
    assert stats['accepted'] == 1 and stats['failed'] == 0
    _assert_superseded_once(r, paused)
    assert faults == []


def _report_after(r, turn_id, ordinal):
    """A progress report the interrupted Turn appended on its lane while it ran."""
    r.turns[turn_id] = {'turnId': turn_id, 'conversationId': 'coordinator',
                        'actor': 'assistant', 'kind': 'coordinator_report.progress',
                        'laneId': 'main', 'ordinal': ordinal, 'status': 'completed',
                        'settlement': {'cause': 'ingested'}, 'createdAt': 1_003_000}


def _record_resumes(r, monkeypatch):
    from types import SimpleNamespace
    sent = []

    def resume_turn(conversation, turn, owner, body, **kwargs):
        attempt_id = r.turns[turn]['currentAttemptId']
        successor = 'continued-' + attempt_id
        r.attempts[successor] = {**r.attempts[attempt_id], 'attemptId': successor, 'status': 'running',
                                 'createdAt': 1_003_500}
        r.turns[turn].update(status='running', currentAttemptId=successor)
        kwargs['trusted_dispatch'].continue_execution(turn, successor)
        sent.append((turn, deepcopy(body)))
        return SimpleNamespace(value={})
    monkeypatch.setattr(r.service.commands, 'resume_turn', resume_turn, raising=False)
    return sent


def test_a_wake_followed_only_by_its_own_reports_continues_in_place(run, monkeypatch, faults):
    """Reports a wake published do not stop it: a resume preserves every later Turn, so
    the wake continues in its own Turn under its own grant, with no second wake."""
    r = run
    paused = _paused_wake(r)
    for turn_id in ('owner-input', 'owner-reply'):
        del r.turns[turn_id]
    del r.attempts['owner-reply-attempt']
    _report_after(r, 'progress-report', 305)
    sent = _record_resumes(r, monkeypatch)
    assert r.wake() == ['accepted']
    assert [turn for turn, _ in sent] == [paused['execution']['turnId']]
    assert len(controls(r)) == 1 and 'progress-report' in r.turns
    assert faults == []


def test_a_coordinator_turn_with_a_call_cut_off_mid_effect_continues_in_a_wake_that_names_it(
        run, monkeypatch, faults):
    """A restart-paused Coordinator Turn whose settlement names a call the restart cut
    off mid-effect is never resumed in place: the request continues in a new wake whose
    brief names the call to check first, for a request Turn and a wake alike."""
    from lib.conversations import restart_resume
    effects = [{'toolName': 'run_command', 'toolCallId': 'call-write', 'operationId': 'op-1'}]
    r = run
    worker = delegate(r)
    end(r, worker)
    _restart_pause(r, 'coordinator-turn', 'coordinator-attempt')
    r.turns['coordinator-turn']['settlement']['error'] = {
        'code': 'worker_effect_outcome_unknown', 'unknownEffects': effects}
    monkeypatch.setattr(restart_resume, 'resume_conversation', lambda *a, **kw: pytest.fail(
        'a Turn with a call cut off mid-effect is never resumed automatically'))
    _never_resume(r, monkeypatch)
    assert r.wake() == ['accepted']
    [wake] = controls(r)
    assert 'coordinator-turn' in wake['request']['brief']
    assert 'run_command call-write with an unknown side effect' in wake['request']['brief']
    # The wake itself is interrupted the same way: the next wake names it again.
    turn_id, attempt_id = wake['execution']['turnId'], wake['execution']['attemptId']
    _restart_pause(r, turn_id, attempt_id)
    r.turns[turn_id]['settlement']['error'] = {
        'code': 'worker_effect_outcome_unknown', 'unknownEffects': effects}
    assert r.wake() == ['accepted']
    rows = controls(r)
    assert len(rows) == 2 and turn_id in rows[1]['request']['brief']
    assert 'run_command call-write with an unknown side effect' in rows[1]['request']['brief']
    assert faults == []


def test_a_request_turn_that_posted_a_report_continues_in_place_not_in_a_scoped_wake(
        run, monkeypatch, faults):
    """2026-11-11 18:28, parallel Router: request Turn 8c1d64fe (owner-forwarded input
    ca86e183) had posted report 0047ec62 when the restart interrupted it. A new wake at
    the lane tail is a synthetic continuation that ``worker_control`` limits to its
    request's Workers. The request Turn continues in place instead, keeping the
    authority the owner's input gave it, and no wake is minted."""
    from lib.conversations import restart_resume
    r = run
    worker = delegate(r)
    end(r, worker)
    _restart_pause(r, 'coordinator-turn', 'coordinator-attempt')
    _report_after(r, 'request-report', 2)
    resumed = []

    def resume_conversation(owner, conversation_id, *, turn_id=None, attempt_id=None):
        resumed.append((conversation_id, turn_id, attempt_id))
        r.attempts['resumed-attempt'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'resumed-attempt',
                                         'status': 'running', 'createdAt': 1_003_000}
        r.turns['coordinator-turn'].update(status='running', currentAttemptId='resumed-attempt')
        return {'outcome': 'resumed'}
    monkeypatch.setattr(restart_resume, 'resume_conversation', resume_conversation)
    assert r.wake() == ['resumed'] and controls(r) == []
    assert resumed == [('coordinator', 'coordinator-turn', 'coordinator-attempt')]
    assert r.wake() == [] and controls(r) == [] and len(resumed) == 1
    assert faults == []


# --- the operation a restart continuation sends ------------------------------------------------

def _paused_tail(r, options):
    ready(r)
    [row] = controls(r)
    turn_id, attempt_id = row['execution']['turnId'], row['execution']['attemptId']
    _restart_pause(r, turn_id, attempt_id)
    r.turns[turn_id]['settlement']['resumeOptions'] = options
    return row


def test_a_tail_offering_checkpoint_resume_continues_with_it_and_its_anchor(run, monkeypatch):
    """Router 20be2960 shape: [checkpoint_resume, regenerate] was sent 'continue'
    at every boot and refused with database_conflict."""
    from types import SimpleNamespace
    r = run
    anchor = {'type': 'tool_checkpoint', 'keptToolRounds': 3}
    row = _paused_tail(r, [{'operation': 'checkpoint_resume', 'anchor': anchor},
                           {'operation': 'regenerate', 'anchor': {'type': 'turn_start'}}])
    attempt_id = row['execution']['attemptId']
    sent = []

    def resume_turn(conversation, turn, owner, body, **kwargs):
        successor = 'continued-' + attempt_id
        r.attempts[successor] = {'attemptId': successor, 'conversationId': conversation, 'turnId': turn,
                                 'taskId': r.attempts[attempt_id]['taskId'], 'status': 'running',
                                 'createdAt': 1_003_000}
        r.turns[turn].update(status='running', currentAttemptId=successor)
        kwargs['trusted_dispatch'].continue_execution(turn, successor)
        sent.append(deepcopy(body))
        return SimpleNamespace(value={})
    monkeypatch.setattr(r.service.commands, 'resume_turn', resume_turn, raising=False)
    assert r.wake() == ['accepted'] and len(controls(r)) == 1
    [body] = sent
    assert body['operation'] == 'checkpoint_resume' and body['resumeAnchor'] == anchor


def test_a_regenerate_only_restart_pause_is_never_regenerated(run, monkeypatch, faults):
    """Parallel 1be49eb0 shape: only Regenerate is offered. Regenerate would redo the
    work, so the Turn never continues in place; the request goes on in a new wake."""
    from lib.project_dispatch.repository import RestartContinuationRefused
    r = run
    row = _paused_tail(r, [{'operation': 'regenerate', 'anchor': {'type': 'turn_start'}}])
    _never_resume(r, monkeypatch)
    with pytest.raises(RestartContinuationRefused, match='offers no continue/checkpoint resume'):
        r.service.dispatch_authorized(r.c.repo.admission(row['id']))
    assert r.wake() == ['accepted']
    rows = controls(r)
    assert len(rows) == 2 and 'never regenerated' in rows[1]['request']['brief']
    assert faults == []


def test_a_revoked_latest_wake_never_resurrects_an_older_paused_one(run, monkeypatch, faults):
    """Revoking the request's latest wake ends its wakes: _proposal never falls
    back to an older wake row of the same request, paused or not."""
    r = run
    paused = _paused_wake(r)
    _settle_later_turn(r)
    _never_resume(r, monkeypatch)
    assert r.wake() == ['accepted']
    newer = controls(r)[1]
    r.attempts[newer['execution']['attemptId']].update(status='completed', settledAt=1_007_000)
    r.c.repo.revoke(newer['id'], expected_revision=newer['grant']['revision'])
    # The older wake is still restart-paused, and would be the lane tail again
    # if every later Turn were gone: nothing proposes it.
    for turn_id in ('owner-input', 'owner-reply', newer['execution']['turnId']):
        r.turns.pop(turn_id, None)
    assert continuation._proposal(r.c.repo, r.c.repo._load()[0], paused['request']['delegation']['attemptId']) is None
    assert r.wake() == [] and len(controls(r)) == 2


# --- real Turn storage: SQLite and native --------------------------------------------------------

from tests.conversation.test_restart_resume import OWNER, chats as chats  # noqa: E402
from tests.turn_lifecycle.test_task_event_delivery import storage as storage  # noqa: E402


def test_real_turns_decide_the_lane_tail_on_both_authorities(chats, tmp_path):
    """The verdict over real ``list_turns`` pages: a restart-paused Turn continues
    in place only while it is its lane's tail. Once the owner's queued input
    follows it, no automated continuation is issued, and no Turn is deleted."""
    from lib.project_dispatch.repository import ProjectDispatchRepository, RestartContinuationRefused
    from lib.project_dispatch.service import ProjectDispatchService
    c = chats
    task = c.running('coordinator-lane', content='half way through the review')
    c.settle(task, 'server_shutdown')
    assignment = {'id': 'wake', 'review': None, 'request': {'recipientId': 'coordinator-lane'},
                  'grant': {'owner': OWNER},
                  'execution': {'turnId': task['_turnId'], 'attemptId': task['_attemptId']}}
    repository = ProjectDispatchRepository(OWNER, str(tmp_path))
    resume = repository.restart_resume(assignment)
    assert resume['turn']['settlement']['cause'] == 'server_restart'
    assert resume['verdict'] == {'tail': True, 'later': 0, 'supersededBy': None, 'queued': False}
    assert resume['option']['operation'] in ('continue', 'checkpoint_resume')

    # Its own report after it does not stop the resume: a resume preserves it.
    report = c.report('coordinator-lane', 'Progress while the review ran')
    resume = repository.restart_resume(assignment)
    assert resume['verdict'] == {'tail': False, 'later': 1, 'supersededBy': None, 'queued': False}
    assert resume['option']['operation'] in ('continue', 'checkpoint_resume')
    assert repository.restart_paused(assignment) and report['turnId']

    queued = c.turns.create_turn_pair('coordinator-lane', command_id='coordinator-lane:queued-input',
                                      input_projection={'content': 'A newer instruction'},
                                      config=deepcopy(task['config']), user_id=OWNER)
    before = [turn['turnId'] for turn in c.turns.list_turns('coordinator-lane', user_id=OWNER)['turns']]
    resume = repository.restart_resume(assignment)
    assert resume['option'] is None and not resume['verdict']['tail']
    assert resume['verdict']['supersededBy'] == queued['turn']['parentTurnId']
    assert not repository.restart_paused(assignment)

    class Repository:
        def validate_own(self, binding):
            return assignment

        def get_attempt(self, attempt_id):
            return repository.get_attempt(attempt_id)

        def restart_resume(self, row):
            return repository.restart_resume(row)

    service = ProjectDispatchService(Repository(), commands=object(), turn_get=lambda *a, **k: None,
                                     attempt_get=lambda *a, **k: None)
    with pytest.raises(RestartContinuationRefused, match='superseded by Turn'):
        service.dispatch_authorized({'assignmentId': 'wake'})
    after = [turn['turnId'] for turn in c.turns.list_turns('coordinator-lane', user_id=OWNER)['turns']]
    assert after == before
