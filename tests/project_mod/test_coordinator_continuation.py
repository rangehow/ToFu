"""Worker outcomes, due decisions and backoff retries resume the request through ordinary admission."""
from copy import deepcopy
from types import SimpleNamespace
import json
import threading

import pytest

from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch import admission, continuation
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.repository import CONTRACT, ProjectDispatchError
from lib.project_dispatch.service import ProjectDispatchService
from lib.conversations.coordinator_tools import execute_coordinator_tool

pytestmark = pytest.mark.unit


@pytest.fixture
def run(runtime, monkeypatch):
    r = runtime
    r.human = r.task
    r.queued_human = False
    r.workspaces = {}
    r.reports = []
    r.c.repo.turn_lister = _lane_lister(r)
    r.c.repo.workspace_get = lambda project, task, **kw: deepcopy(r.workspaces.get(task))
    r.command_lock = threading.Lock()

    class PairClient:
        def command(self, operation, payload, command_id):
            assert operation == 'turn.create_pair'
            return {'submittedTurn': {'turnId': 'input-' + command_id,
                'conversationId': payload['conversation_id'], 'actor': payload['input_actor'],
                'projection': deepcopy(payload['input_projection'])}}

    monkeypatch.setattr('lib.turn_lifecycle._turn_client', lambda **kw: PairClient())

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            assert kwargs['principal'].kind == 'system' and request['inputActor'] == 'virtual_user'
            assert request['inputTurn']['_initiator'] == 'brain'
            from lib.turn_lifecycle import create_turn_pair
            accepted = trusted.capture(request['config'], owner, kwargs['principal'], conv)
            recorded = create_turn_pair(
                conv, command_id=request['commandId'],
                input_projection=request['inputTurn'],
                config=accepted,
                user_id=owner, input_actor=request['inputActor'],
                input_kind=request.get('inputKind', 'input'),
                require_lane_idle=True, reject_if_human_queued=True)
            request['inputTurn'] = recorded['submittedTurn']['projection']
            with r.command_lock:
                row = r.c.repo.read(trusted.binding['assignmentId'])
                if row['execution']:
                    return SimpleNamespace(value={})
                from lib.turn_lifecycle import LifecycleConflict
                if conv == 'coordinator' and r.queued_human:
                    raise LifecycleConflict('superseded_by_human', 'human first')
                if any(a['conversationId'] == conv and a['status'] in ('pending', 'running') for a in r.attempts.values()):
                    raise LifecycleConflict('lane_busy', 'busy')
                key = row['id']; turn, attempt, task = 'turn-' + key, 'attempt-' + key, 'task-' + key
                if row['request']['profile']['mode'] == 'coordinator':
                    snapshot = request['inputTurn']['contextSnapshot']['snapshot']
                    assert snapshot['model'] == accepted['model']
                    assert snapshot['tools'] and snapshot['roots']
                    assert request['inputTurn']['origin']['initiator'] == 'brain'
                trusted.bind_execution(turn, attempt)
                r.turns[turn] = {'turnId': turn, 'conversationId': conv, 'actor': 'assistant',
                    'projectionRevision': 1, 'projection': {'content': 'verified evidence'}, 'createdAt': 1_001_000}
                r.attempts[attempt] = {'attemptId': attempt, 'conversationId': conv, 'turnId': turn,
                    'taskId': task, 'status': 'running', 'createdAt': 1_001_000, 'config': accepted}
                r.calls.append(conv)
            return SimpleNamespace(value={})

        def abort_project_attempt(self, attempt, owner, *, provenance):
            assert owner == 7
            r.cancel_provenance = provenance
            if r.attempts[attempt]['status'] in ('pending', 'running'):
                r.attempts[attempt]['status'] = 'interrupted'

    r.service = ProjectDispatchService(r.c.repo, commands=Commands(),
        turn_get=r.c.repo.turn_get, attempt_get=r.c.repo.attempt_get)
    monkeypatch.setattr('lib.conversations.coordinator_workers.ProjectDispatchService', lambda repo: r.service)
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: r.service)
    names = CONTRACT['continuationTools'] + ['run_command', 'read_files', 'write_file']
    capture_worker_policy(r.task, r.task['config'], [{'function': {'name': name}} for name in names], 'model-a',
                          worker_model='model-a')

    class Reports:
        def publish(self, scope, args):
            if args not in r.reports:
                r.reports.append(args)
            return {'turnId': 'report-limit'}

    r.scheduled = []
    r.wake = lambda: continuation.recover_coordinator(r.c.repo, service=r.service, reports=Reports(),
                                                      schedule=r.scheduled.append)
    r.call = lambda name, **args: json.loads(execute_coordinator_tool(r.task, name, args))
    return r


def delegate(r, key='one'):
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': key})
    assert result.get('dispatch') == 'accepted', result
    return r.c.repo.read(result['assignment_id'])


def controls(r):
    return [row for row in r.c.repo.list() if row['request']['profile']['mode'] == 'coordinator']


def end(r, row, status='completed', delivered=True):
    attempt = r.attempts[row['execution']['attemptId']]
    attempt['status'] = status
    attempt['settledAt'] = int(r.c.now[0] * 1000)
    if delivered:
        # A Worker checkout is registered under its assignment's work id.
        work_id = row['execution']['taskId']
        r.workspaces[work_id] = {'task_id': work_id, 'user_id': 7, 'project_root': '/project',
            'origin': {admission.KEY: r.c.repo.admission(row['id']),
                       'delivery': {'state': 'delivered', 'taskOutcome': 'completed'}}}


def activate(r):
    row = controls(r)[-1]
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    config = trusted.capture({}, 7, trusted.principal, 'coordinator')
    config['_turnOwnerUserId'] = 7
    r.task = {'id': r.attempts[row['execution']['attemptId']]['taskId'], 'convId': 'coordinator',
        '_userId': 7, 'config': config, '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': row['execution']['turnId'], '_attemptId': row['execution']['attemptId'],
        'status': 'running', 'abort_event': threading.Event(), 'usage': {}}
    admission.bind_account(r.task)
    capture_worker_policy(r.task, config, [{'function': {'name': 'get_project_workers'}}], 'forged-model',
                          worker_model='forged-model')
    return row


def report(r, kind='delivery'):
    r.turns['human-report'] = {'turnId': 'human-report', 'conversationId': 'coordinator', 'actor': 'assistant',
        'kind': 'coordinator_report.' + kind, 'createdAt': 1_002_000,
        'projection': {'content': 'Verified results', 'origin': {'initiator': 'brain'}}}
    return {'report_turn_id': 'human-report', 'outcome': 'complete' if kind == 'delivery' else 'needs_user'}


def ready(r, status='completed'):
    worker = delegate(r)
    end(r, worker, status)
    assert r.wake() == []  # A live original human task owns follow-through.
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    return worker


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted', 'truncated'])
def test_one_outcome_wakes_exactly_one_ordinary_system_turn(run, status):
    r = run; worker = ready(r, status)
    assert r.wake() == [] and len(controls(r)) == 1
    row = controls(r)[0]
    assert row['request']['delegation'] == worker['request']['delegation']
    assert row['grant']['subject'] == 'user:7'
    assert row['request']['coordinatorId'] == row['request']['recipientId'] == 'coordinator'
    assert row['request']['profile']['tools'] == CONTRACT['continuationTools']


def test_continuation_can_delegate_and_review_without_becoming_human(run):
    r = run; first = ready(r); control = activate(r)
    assert not admission.requires_isolated_workspace(r.task)
    assert r.task['_principalContext']['kind'] == 'system' and not r.task['_attended']
    assert r.task['_coordinatorWorkerPolicy'].model == 'model-a'
    assert 'write_file' in r.task['_coordinatorWorkerPolicy'].tools
    replay = delegate(r)
    assert replay['id'] == first['id'] and len(r.created) == 1
    r.c.conversations.rows['coordinator']['settings']['model'] = 'model-current'
    second = delegate(r, 'second')
    assert second['request']['model'] == 'model-current'
    assert second['request']['delegation'] == first['request']['delegation']
    review = r.call('review_project_work', assignment_id=first['id'], result_revision=1, decision='accept', note='Verified')
    assert review['decision'] == 'accept', review
    page = r.call('get_project_workers', limit=1)
    assert len(page['workers']) == 1 and page['next_cursor'] == first['id']
    assert r.call('get_project_workers', after_id=page['next_cursor'])['workers'][0]['assignment_id'] == second['id']
    from lib.project_dispatch.scope import admit_tool
    from lib.llm.llm_errors import AbortedError
    assert admit_tool(r.task, 'get_project_workers', {}, '/project') is None
    with pytest.raises(AbortedError):
        admit_tool(r.task, 'write_file', {'path': 'escape', 'content': 'x'}, '/project')
    assert r.task['_principalContext']['kind'] == 'system'


def test_pending_birth_reuses_grant_and_respects_human_lane(run):
    r = run; worker = delegate(r); end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'; r.queued_human = True
    assert r.wake() == ['deferred']
    first = controls(r)[0]
    assert first['execution'] is None and r.wake() == ['deferred'] and len(controls(r)) == 1
    r.queued_human = False
    assert r.wake() == ['accepted'] and controls(r)[0]['id'] == first['id']


@pytest.mark.parametrize('violation', ['stop', 'rebind', 'source', 'owner'])
def test_original_authority_withdrawal_cannot_wake(run, violation):
    r = run; worker = delegate(r); end(r, worker); r.attempts['coordinator-attempt']['status'] = 'completed'
    if violation == 'stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(7, 'coordinator', 1001, client=r.c.client)
    if violation == 'rebind': r.c.binding.value['revision'] = 2
    if violation == 'source': r.turns['human-turn']['actor'] = 'virtual_user'
    if violation == 'owner': r.c.conversations.rows['coordinator']['user_id'] = 8
    assert r.wake() == ['retired'] and not controls(r)
    assert r.wake() == [] and not controls(r)


def test_deleted_first_worker_does_not_disable_request_continuation(run):
    r = run; worker = delegate(r); end(r, worker)
    r.c.conversations.rows.pop(worker['request']['recipientId'])
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']


@pytest.mark.parametrize('failed_with_held', [False, True])
def test_empty_turns_back_off_then_stop_once_with_unread_report(run, failed_with_held):
    r = run; ready(r)
    if failed_with_held:
        from lib.turn_verdict import LEAD_HOLD
        activate(r)
        held = delegate(r, 'held')
        end(r, held, 'interrupted', delivered=False)
        r.turns[held['execution']['turnId']]['settlement'] = {'cause': LEAD_HOLD}
        # Give the first failure a frontier that already includes the held Worker.
        end(r, controls(r)[-1], 'failed', delivered=False)
        assert r.wake() == ['accepted']
    status = 'failed' if failed_with_held else 'completed'
    baseline = len(controls(r)) - 1
    limit = CONTRACT['delegationDefaults']['maxStalledTurns']
    delays = CONTRACT['delegationDefaults']['stallRetrySeconds']
    for stalled in range(1, limit):
        end(r, controls(r)[-1], status, delivered=False)
        assert r.wake() == [] and r.scheduled, 'a stalled turn retries later instead of stopping'
        assert r.scheduled[-1] >= r.c.now[0] + delays[min(stalled - 1, len(delays) - 1)] - 1
        r.c.now[0] = r.scheduled[-1] + 1
        assert r.wake() == ['accepted']
    end(r, controls(r)[-1], status, delivered=False)
    assert r.wake() == ['stopped'] and len(controls(r)) == baseline + limit and len(r.reports) == 1
    assert r.reports[0]['kind'] == 'blocked' and r.wake() == []
    value, _ = r.c.repo._load()
    assert value['coordinatorSettlements'][0]['reason'] == 'no_progress'


def test_waiting_for_active_worker_is_not_no_progress(run):
    r = run; first = delegate(r); second = delegate(r, 'second')
    end(r, first); r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']; end(r, controls(r)[-1], delivered=False)
    assert r.wake() == [] and not r.reports
    end(r, second)
    assert r.wake() == ['accepted'] and len(controls(r)) == 2


@pytest.mark.parametrize('other_status,withdraw', [('running', None), ('held', None),
                                                   ('held', 'stop'), ('held', 'revoke')])
def test_failed_results_wake_retries_without_waiting_for_another_worker(run, monkeypatch, other_status, withdraw):
    from lib.project_dispatch import recovery
    from lib.turn_verdict import LEAD_HOLD

    r = run
    done, other = delegate(r), delegate(r, 'other')
    end(r, done)
    if other_status == 'held':
        end(r, other, 'interrupted', delivered=False)
        r.turns[other['execution']['turnId']]['settlement'] = {'cause': LEAD_HOLD}
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    [failed] = controls(r)
    end(r, failed, 'failed', delivered=False)
    r.turns[failed['execution']['turnId']].update(status='failed', settlement={
        'cause': 'failed', 'error': {'code': 'context_length_exceeded'}})
    # Replayed observation from an older completed lead Turn is not proof
    # this failed wake handled the results.
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', {
        done['id']: ('needs_review', None, done['execution']['attemptId'], 'delivered')})
    expected_due = r.c.now[0] + CONTRACT['delegationDefaults']['stallRetrySeconds'][0]
    assert r.wake() == []
    assert r.scheduled == [expected_due], 'a failed lead has not processed this result frontier'

    query = r.c.client.query

    def list_records(op, payload, **kw):
        if op != 'record.list':
            return query(op, payload)
        return [{'key': key, 'value': deepcopy(row['value'])}
                for (namespace, key), row in sorted(r.c.client.rows.items())
                if namespace == payload['namespace'] and key > payload['after_key']
                and key.startswith(payload['prefix'])][:payload['limit']]

    monkeypatch.setattr(r.c.client, 'query', list_records)
    monkeypatch.setattr(continuation, 'schedule_wake', lambda owner, project, due, **kw: r.scheduled.append(due))

    def boot():
        r.scheduled.clear()
        continuation._LANE_OBSERVATIONS.clear()
        outcomes = []

        def reconcile(owner, project, *, repository):
            outcomes.append(recovery.recover_project_dispatch(owner, project, repository=repository,
                service=r.service, was_aborted_after=lambda *a: False, clock=repository.clock))

        assert recovery.recover_persisted_dispatch(client=r.c.client,
            repository_factory=lambda owner, project: r.c.repo, recovery=reconcile) == 1
        return outcomes[0]

    # A first restart re-derives the lost timer. When it becomes due, human
    # lane priority leaves a persisted, unbound wake (not a running Attempt).
    assert boot()['accepted'] == 0 and r.scheduled == [expected_due]
    r.c.now[0] = expected_due + 1
    r.queued_human = True
    assert r.wake() == ['deferred']
    _, owed = controls(r)
    assert owed['execution'] is None
    continuation.observe_turn_end(7, 'coordinator', 'coordinator-attempt', {
        done['id']: ('needs_review', None, done['execution']['attemptId'], 'delivered')})
    assert r.wake() == ['deferred'] and controls(r)[-1] == owed
    assert r.attempts[other['execution']['attemptId']]['status'] == (
        'interrupted' if other_status == 'held' else 'running')

    # The second restart reuses the persisted grant or respects withdrawal.
    r.queued_human = False
    if withdraw == 'stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(7, 'coordinator', r.c.now[0], client=r.c.client)
    elif withdraw == 'revoke':
        r.c.repo.revoke(owed['id'], expected_revision=owed['grant']['revision'])
    if withdraw:
        assert boot()['accepted'] == 0 and r.wake() == []
        assert controls(r)[-1]['execution'] is None and r.calls.count('coordinator') == 1
        return
    assert boot()['accepted'] == 1
    old, successor = controls(r)
    assert successor['id'] == owed['id'] and successor['grant'] == owed['grant']
    assert old == failed
    assert successor['request']['coordinatorWake']['previousAttemptId'] == old['execution']['attemptId']
    assert successor['execution']['turnId'] != old['execution']['turnId']
    assert r.attempts[successor['execution']['attemptId']]['status'] == 'running'
    # A duplicate settlement/recovery event cannot start a third lead or touch the Worker.
    assert r.wake() == [] and boot()['accepted'] == 0 and len(controls(r)) == 2
    assert r.c.repo.read(other['id']) == other
    assert r.calls.count(other['request']['recipientId']) == 1


@pytest.mark.parametrize('new_evidence', [False, True])
def test_failed_results_wake_respects_automatic_retry_verdict(run, new_evidence):
    r = run
    done, other = delegate(r), delegate(r, 'other')
    end(r, done)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    [failed] = controls(r)
    end(r, failed, 'failed', delivered=False)
    execution = failed['execution']
    r.turns[execution['turnId']].update(status='failed', currentAttemptId=execution['attemptId'], settlement={
        'cause': 'generation_error', 'error': {'code': 'context_length_exceeded',
            'retryPolicy': {'mode': 'manual', 'automatic': False}}})
    if new_evidence:
        end(r, other)
        assert r.wake() == ['accepted'] and len(controls(r)) == 2 and not r.reports
    else:
        assert r.wake() == ['stopped'] and not r.scheduled and controls(r) == [failed]
        assert r.reports[0]['kind'] == 'blocked'
        assert 'unsafe to retry automatically' in r.reports[0]['message']
        value, _ = r.c.repo._load()
        assert value['coordinatorSettlements'][0]['reason'] == 'no_progress'
        assert r.wake() == [] and len(r.reports) == 1


def _failed_oversized_wake(r):
    done, other = delegate(r), delegate(r, 'other')
    end(r, done)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    [failed] = controls(r)
    end(r, failed, 'failed', delivered=False)
    execution = failed['execution']
    r.turns[execution['turnId']].update(status='failed', laneId='main', ordinal=20,
        currentAttemptId=execution['attemptId'], settlement={'cause': 'generation_error', 'error': {
            'code': 'context_length_exceeded', 'retryPolicy': {'automatic': False}}})
    return done, other, failed


@pytest.mark.parametrize('owner_replied', [False, True])
def test_blocked_result_retry_does_not_discard_owner_decision(run, monkeypatch, owner_replied):
    r = run
    _bound_request(r)
    _turn_lister(r)
    monkeypatch.setattr(continuation, 'schedule_wake', lambda *a, **kw: None)
    _decision_report(r)
    assert 'error' not in r.call('request_user_decision', report_turn_id='decision-report',
                                recommendation='Use A', wait_seconds=600)
    _, _, failed = _failed_oversized_wake(r)
    due = _pending(r)['dueAt']
    assert r.wake() == [] and r.scheduled == [due] and not r.reports
    assert not _pending(r).get('resolved')
    r.c.now[0] = due + 1
    if owner_replied:
        _turn_lister(r, human_after=[11])
        assert r.wake() == [] and controls(r) == [failed] and not r.reports
        assert not _pending(r).get('resolved')  # The owner's new Turn owns the response.
    else:
        assert r.wake() == ['accepted']
        assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'decision_default'
        assert _pending(r)['resolved'] == 'default_applied'


def test_later_completed_observer_discharges_unborn_retry_only_on_same_lane(run, monkeypatch):
    r = run
    done, other, failed = _failed_oversized_wake(r)
    r.turns[failed['execution']['turnId']]['settlement']['error'].pop('retryPolicy')
    r.c.now[0] += 60
    r.queued_human = True
    assert r.wake() == ['deferred']
    owed = controls(r)[-1]
    assert owed['execution'] is None
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})
    r.turns['observer-turn'] = {'turnId': 'observer-turn', 'conversationId': 'coordinator',
        'currentAttemptId': 'observer', 'status': 'completed', 'ordinal': 21, 'laneId': 'branch'}
    r.attempts['observer'] = {'attemptId': 'observer', 'turnId': 'observer-turn',
        'conversationId': 'coordinator', 'status': 'completed'}
    continuation.observe_turn_end(7, 'coordinator', 'observer', {
        done['id']: ('needs_review', None, done['execution']['attemptId'], 'delivered')})
    assert r.wake() == ['deferred'] and controls(r)[-1] == owed
    r.turns['observer-turn']['laneId'] = 'main'
    _record_durable_observation(r, done, 'observer-turn')
    r.queued_human = False
    assert r.wake() == [] and controls(r)[-1] == owed
    end(r, other)
    assert r.wake() == ['accepted'] and controls(r)[-1]['id'] == owed['id']


def _record_durable_observation(r, worker, observer_turn):
    execution = worker['execution']
    r.turns[execution['turnId']]['currentAttemptId'] = execution['attemptId']
    r.turns[observer_turn]['conversationId'] = 'coordinator'
    r.turns[observer_turn]['settlement'] = {'coordinatorWorkerObservations': {
        worker['id']: {'attemptId': execution['attemptId'], 'resultRevision': 1,
                       'deliveryStatus': 'delivered'}}}


@pytest.mark.parametrize('replied', [False, True])
def test_observed_unborn_retry_does_not_own_decision_deadline(run, monkeypatch, replied):
    r = run
    _bound_request(r)
    _turn_lister(r)
    monkeypatch.setattr(continuation, 'schedule_wake', lambda *a, **kw: None)
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})
    _decision_report(r)
    assert 'error' not in r.call('request_user_decision', report_turn_id='decision-report',
                                recommendation='Use A', wait_seconds=600)
    done, _, failed = _failed_oversized_wake(r)
    r.turns[failed['execution']['turnId']]['settlement']['error'].pop('retryPolicy')
    r.c.now[0] += 60
    r.queued_human = True
    assert r.wake() == ['deferred']
    owed = controls(r)[-1]
    r.queued_human = False
    r.turns['later'] = {'turnId': 'later', 'currentAttemptId': 'later-attempt',
                        'status': 'completed', 'ordinal': 21, 'laneId': 'main'}
    r.attempts['later-attempt'] = {'attemptId': 'later-attempt', 'turnId': 'later',
                                   'conversationId': 'coordinator', 'status': 'completed'}
    _record_durable_observation(r, done, 'later')
    ordinary_lister = _lane_lister(r)
    r.c.repo.turn_lister = lambda *a, **kw: (ordinary_lister(*a, **kw)
        if kw.get('after_ordinal') == 20 else {'turns': []})
    due = _pending(r)['dueAt']
    assert r.wake() == [] and r.scheduled[-1] == due
    r.c.now[0] = due + 1
    if replied:
        _turn_lister(r, human_after=[11])
        assert r.wake() == [] and not _pending(r).get('resolved')
    else:
        assert r.wake() == ['accepted']
        assert controls(r)[-1]['request']['coordinatorWake']['reason'] == 'decision_default'
        assert _pending(r)['resolved'] == 'default_applied'
    assert r.c.repo.read(owed['id'])['execution'] is None and r.wake() == []


def test_successful_result_observation_after_proposal_defers_birth_without_local_hint(run, monkeypatch):
    r = run
    done, _, failed = _failed_oversized_wake(r)
    failed_turn = r.turns[failed['execution']['turnId']]
    failed_turn['settlement']['error'].pop('retryPolicy')
    done_turn = r.turns[done['execution']['turnId']]
    done_turn['currentAttemptId'] = done['execution']['attemptId']
    r.c.repo.turn_lister = lambda *a, **kw: {'turns': [r.turns['later']] if 'later' in r.turns else []}
    monkeypatch.setattr(continuation, '_LANE_OBSERVATIONS', {})
    original = r.service.dispatch_authorized

    def observer_commits(binding):
        r.turns['later'] = {'turnId': 'later', 'currentAttemptId': 'observer',
            'status': 'completed', 'ordinal': 21, 'laneId': 'main', 'settlement': {
                'coordinatorWorkerObservations': {done['id']: {'attemptId': done['execution']['attemptId'],
                    'resultRevision': 1, 'deliveryStatus': 'delivered'}}}}
        return original(binding)

    monkeypatch.setattr(r.service, 'dispatch_authorized', observer_commits)
    r.c.now[0] += 60
    assert r.wake() == ['deferred'] and controls(r)[-1]['execution'] is None
    assert r.calls.count('coordinator') == 1 and r.wake() == []
    # A different result revision is not the result that the observer saw.
    done_turn['modelContextRevision'] = 2
    assert r.wake() == ['accepted'] and r.calls.count('coordinator') == 2


def test_authorization_rechecks_runnable_disposition(run, monkeypatch):
    r = run
    _, _, failed = _failed_oversized_wake(r)
    error = r.turns[failed['execution']['turnId']]['settlement']['error']
    policy = error.pop('retryPolicy')
    r.c.now[0] += 60
    authorize = r.c.repo._authorize_request

    def blocked_before_authorization(*a, **kw):
        error['retryPolicy'] = policy
        return authorize(*a, **kw)

    monkeypatch.setattr(r.c.repo, '_authorize_request', blocked_before_authorization)
    assert r.wake() == ['deferred'] and controls(r) == [failed] and not r.reports
    assert r.wake() == ['stopped'] and len(r.reports) == 1


def test_deferred_retry_cannot_birth_after_predecessor_advances(run):
    r = run
    _, _, failed = _failed_oversized_wake(r)
    old = failed['execution']
    r.turns[old['turnId']]['settlement']['error'].pop('retryPolicy')
    r.c.now[0] += 60
    r.queued_human = True
    assert r.wake() == ['deferred']
    owed = controls(r)[-1]
    r.attempts['advanced'] = {'attemptId': 'advanced', 'turnId': old['turnId'],
        'conversationId': 'coordinator', 'status': 'pending', 'taskId': 'conversation-attempt:advanced'}
    r.c.repo.continue_execution(failed['id'], old, old['turnId'], 'advanced')
    r.queued_human = False
    with pytest.raises(ProjectDispatchError, match='predecessor was withdrawn or advanced'):
        r.service.dispatch_authorized(r.c.repo.admission(owed['id']))
    assert r.wake() == ['deferred'] and controls(r)[-1] == owed
    assert r.calls.count('coordinator') == 1


def test_blocked_report_requires_committed_stop_evidence(run, monkeypatch):
    r = run
    _, other, failed = _failed_oversized_wake(r)
    settle = continuation._settle

    def complete_before_commit(*a, **kw):
        end(r, other)
        return settle(*a, **kw)

    monkeypatch.setattr(continuation, '_settle', complete_before_commit)
    assert r.wake() == ['deferred'] and not r.reports
    assert not r.c.repo._load()[0].get('coordinatorSettlements')
    assert controls(r) == [failed] and r.wake() == ['accepted']


def test_worker_point_change_at_stop_commit_rederives_before_publishing(run, monkeypatch):
    r = run
    _, other, failed = _failed_oversized_wake(r)
    original = r.c.repo._commit_value
    races = []

    def race(original_value, value, version, *, fence_documents=()):
        if value.get('coordinatorSettlements') and not races:
            assert ('assignments', other['id']) in fence_documents
            races.append(True)
            r.c.repo.revoke(other['id'], expected_revision=1)
        return original(original_value, value, version, fence_documents=fence_documents)

    monkeypatch.setattr(r.c.repo, '_commit_value', race)
    assert r.wake() == ['deferred'] and races == [True]
    assert not r.reports and not r.c.repo._load()[0].get('coordinatorSettlements')
    assert controls(r) == [failed] and r.wake() == ['accepted']


def test_committed_stop_recovers_report_after_lost_ack_without_starting_turn(run, monkeypatch):
    r = run
    _, _, failed = _failed_oversized_wake(r)
    settle = continuation._settle
    from lib.storage.errors import StorageError

    def lose_ack(*a, **kw):
        settle(*a, **kw)
        raise StorageError('database_timeout', 'Lost stop acknowledgement', retryable=True)

    monkeypatch.setattr(continuation, '_settle', lose_ack)
    assert r.wake() == ['deferred'] and not r.reports
    [receipt] = r.c.repo._load()[0]['coordinatorSettlements']
    assert not receipt.get('reportTurnId') and receipt['stopReport'] == 'automatic_retry_blocked'
    assert r.wake() == ['stopped'] and controls(r) == [failed]
    assert r.wake() == [] and len(r.reports) == 1


def test_finish_requires_exact_review_delivery_and_published_report(run):
    r = run; first = ready(r); activate(r); args = report(r)
    assert 'Unfinished/unreviewed' in r.call('finish_project_request', **args)['error']
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1, decision='accept', note='Verified')
    assert reviewed['decision'] == 'accept'
    task = first['execution']['taskId']
    r.workspaces[task]['origin']['delivery']['state'] = 'blocked'
    assert 'delivery' in r.call('finish_project_request', **args)['error']
    r.workspaces[task]['origin']['delivery']['state'] = 'delivered'
    r.turns[first['execution']['turnId']]['projectionRevision'] = 2
    assert 'exact result' in r.call('finish_project_request', **args)['error']
    # A display-only update (a translation overlay) leaves the result revision.
    r.turns[first['execution']['turnId']]['modelContextRevision'] = 1
    done = r.call('finish_project_request', **args)
    assert done['reason'] == 'complete' and r.call('finish_project_request', **args) == done
    assert 'settled' in r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'late'})['error']
    end(r, controls(r)[-1]); assert r.wake() == []


def test_closed_request_refuses_queued_work_and_late_settlement_but_admits_new_owner_request(run, monkeypatch):
    from lib.conversations import coordinator_births
    from lib.project_dispatch.request_budget import account_identity, bind_source_account
    from lib.tools.result_value import tool_result_failure_verdict

    r = run
    next_task, retry_task = deepcopy(r.human), deepcopy(r.human)
    original_account = _bound_request(r)
    assert original_account['id'] == account_identity(1, r.c.request['authorizationSource'])
    first = ready(r)
    control = activate(r)
    assert first['request']['delegation']['policy']['requestBudgetId'] == original_account['id']
    assert control['request']['delegation']['policy']['requestBudgetId'] == original_account['id']
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                      decision='accept', note='Verified')
    assert reviewed['decision'] == 'accept' and reviewed['canonical_delivery'] is True
    done = r.call('finish_project_request', **report(r))
    assert done['reason'] == 'complete' and done['sourceAttemptId'] == 'coordinator-attempt'
    first, control = r.c.repo.read(first['id']), r.c.repo.read(control['id'])
    closed, calls, created, writes = deepcopy(r.c.repo._load()[0]), list(r.calls), len(r.created), r.c.client.puts

    follow_up = {**r.args, 'idempotency_key': 'next-part'}
    queued = []
    with monkeypatch.context() as held:
        held.setattr(coordinator_births, 'spawn', lambda body, name: queued.append(body))
        starting = r.call('delegate_project_work', **follow_up)
        assert starting['status'] == starting['dispatch'] == 'starting'
        [birth] = coordinator_births.unfinished(r.task, [starting['assignment_id']])
        assert birth.result is birth.failure is None and len(queued) == 1
        assert r.c.repo._load()[0] == closed and r.calls == calls and len(r.created) == created
        queued.pop()()
    reason = 'Coordinator request is settled; new work requires a new human request'
    assert birth.done.is_set() and birth.result is None and birth.failure['error'] == reason
    [failed] = r.call('get_project_workers')['starting_workers']
    assert failed['assignment_id'] == starting['assignment_id']
    assert failed['status'] == 'failed_to_start' and failed['error']['error'] == reason
    refused = execute_coordinator_tool(r.task, 'delegate_project_work', follow_up)
    assert tool_result_failure_verdict(refused) == 'error'
    assert json.loads(refused)['error'] == reason

    end(r, control, delivered=False)
    for _ in range(2):
        end(r, first)
        assert r.wake() == []
        assert continuation.recover_coordinator(r.c.repo, service=r.service, schedule=r.scheduled.append,
                                               sources=[done['sourceAttemptId']]) == []
    assert r.c.repo._load()[0] == closed and r.c.client.puts == writes
    assert r.calls == calls and len(r.created) == created and controls(r) == [control]
    assert not r.scheduled and not r.reports

    r.c.now[0] += 10
    retry_task.update(id='same-input-retry-task', _turnId='same-input-retry-turn',
                      _attemptId='same-input-retry-attempt', status='running', usage={}, abort_event=threading.Event())
    r.turns[retry_task['_turnId']] = {'conversationId': 'coordinator', 'turnId': retry_task['_turnId'],
        'actor': 'assistant', 'parentTurnId': 'human-turn',
        'currentAttemptId': retry_task['_attemptId'], 'projectionRevision': 1}
    r.attempts[retry_task['_attemptId']] = {'conversationId': 'coordinator', 'turnId': retry_task['_turnId'],
        'attemptId': retry_task['_attemptId'], 'taskId': retry_task['id'], 'status': 'running',
        'createdAt': int(r.c.now[0] * 1000), 'config': deepcopy(retry_task['config'])}
    r.task = retry_task
    retry_account = bind_source_account(r.task, repository=r.c.repo)
    assert retry_account['id'] == original_account['id']
    assert retry_account['authorizationSource'] == original_account['authorizationSource']
    assert [account['attemptId'] for account in retry_account['accounts']] == [
        'coordinator-attempt', 'same-input-retry-attempt']
    assert retry_task['_coordinatorRequestBudget'].origin_attempt == done['sourceAttemptId']
    retry_bound, retry_writes = deepcopy(r.c.repo._load()[0]), r.c.client.puts
    refused = execute_coordinator_tool(r.task, 'delegate_project_work', follow_up)
    assert tool_result_failure_verdict(refused) == 'error' and json.loads(refused)['error'] == reason
    assert r.c.repo._load()[0] == retry_bound and r.c.client.puts == retry_writes
    assert r.calls == calls and len(r.created) == created and not r.scheduled and not r.reports
    r.attempts[retry_task['_attemptId']]['status'] = 'completed'

    r.c.now[0] += 10
    next_task.update(id='next-coordinator-task', _turnId='next-coordinator-turn', _attemptId='next-coordinator-attempt',
                     status='running', usage={}, abort_event=threading.Event())
    r.turns['next-human-turn'] = {**deepcopy(r.c.turn), 'turnId': 'next-human-turn',
        'projection': {'role': 'user', 'content': 'A distinct synthetic owner request'}}
    assert r.turns['next-human-turn']['actor'] == 'human'
    r.turns[next_task['_turnId']] = {'conversationId': 'coordinator', 'turnId': next_task['_turnId'],
        'actor': 'assistant', 'parentTurnId': 'next-human-turn',
        'currentAttemptId': next_task['_attemptId'], 'projectionRevision': 1}
    r.attempts[next_task['_attemptId']] = {'conversationId': 'coordinator', 'turnId': next_task['_turnId'],
        'attemptId': next_task['_attemptId'], 'taskId': next_task['id'], 'status': 'running',
        'createdAt': int(r.c.now[0] * 1000), 'config': deepcopy(next_task['config'])}
    r.task = next_task
    fresh_account = bind_source_account(r.task, repository=r.c.repo)
    assert fresh_account['id'] != original_account['id']
    assert fresh_account['authorizationSource'] == {
        'conversationId': 'coordinator', 'turnId': 'next-human-turn', 'projectionRevision': 3}
    assert fresh_account['id'] == account_identity(1, fresh_account['authorizationSource'])
    fresh = delegate(r, 'next-part')
    assert fresh['request']['authorizationSource'] == fresh_account['authorizationSource']
    assert fresh['request']['delegation']['policy']['requestBudgetId'] == fresh_account['id']
    assert fresh['request']['delegation']['attemptId'] == next_task['_attemptId']
    assert fresh['request']['delegation']['turnId'] == next_task['_turnId']
    assert fresh['id'] not in {first['id'], starting['assignment_id']}
    assert fresh['request']['recipientId'] not in {first['request']['recipientId'], starting['conversation_id']}
    assert r.attempts[fresh['execution']['attemptId']]['status'] == 'running'
    assert r.c.repo.read(first['id']) == first and controls(r) == [control]
    final = r.c.repo._load()[0]
    assert final['coordinatorSettlements'] == [done]
    assert {account['id']: account for account in final['requestBudgets']} == {
        original_account['id']: retry_account, fresh_account['id']: fresh_account}
    assert r.calls == calls + [fresh['request']['recipientId']] and len(r.created) == created + 1
    assert not r.scheduled and not r.reports


@pytest.mark.parametrize('violation', ['kind', 'origin', 'age', 'id'])
def test_finish_cannot_substitute_an_unrelated_report(run, violation):
    r = run; ready(r); activate(r); args = report(r, 'decision')
    value = r.turns['human-report']
    if violation == 'kind': value['kind'] = 'message'
    if violation == 'origin': value['projection']['origin'] = {'initiator': 'human'}
    if violation == 'age': value['createdAt'] = 1
    if violation == 'id': value['turnId'] = 'wrong'
    assert 'durable report' in r.call('finish_project_request', **args)['error']


def test_cancel_failed_worker_then_finish_and_replay_is_safe(run):
    r = run; failed = ready(r, 'failed'); activate(r)
    for _ in range(2):
        cancelled = r.call('cancel_project_work', assignment_id=failed['id'])
        assert cancelled['revoked'], cancelled
    done = r.call('finish_project_request', **report(r))
    assert done['reason'] == 'complete'


def test_no_deadline_long_running_request_keeps_waking(run):
    r = run; worker = delegate(r); r.attempts['coordinator-attempt']['status'] = 'completed'
    assert worker['request']['budget'] == {}
    end(r, worker)
    r.c.now[0] += 30 * 24 * 3600  # Far beyond the old two-hour request deadline.
    assert r.wake() == ['accepted'] and not r.reports
    assert controls(r)[0]['request']['budget'] == {}


def test_settlement_receipts_are_immutable_and_per_source_unique(run):
    r = run; ready(r); activate(r)
    assert r.call('finish_project_request', **report(r, 'decision'))['reason'] == 'needs_user'
    with pytest.raises(ProjectDispatchError, match='immutable'):
        r.c.repo._change(lambda value: value['coordinatorSettlements'].clear())
    with pytest.raises(ProjectDispatchError):
        r.c.repo._change(lambda value: value['coordinatorSettlements'].append(deepcopy(value['coordinatorSettlements'][0])))


def test_human_api_cannot_mint_internal_control_profile(run):
    r = run; ready(r)
    with pytest.raises(ProjectDispatchError, match='issued'):
        r.c.repo.authorize(controls(r)[0]['request'], r.c.principal, 'forged-control')


def test_simultaneous_wakes_share_one_grant_and_birth(run):
    from concurrent.futures import ThreadPoolExecutor
    r = run; worker = delegate(r); end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    gate = threading.Barrier(4)
    def wake():
        gate.wait(3)
        return r.wake()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: wake(), range(4)))
    assert len(controls(r)) == 1 and r.calls.count('coordinator') == 1, outcomes
    assert r.wake() == []


def test_finish_rechecks_new_worker_inside_same_grant_cas(run, monkeypatch):
    r = run; worker = ready(r); activate(r)
    assert 'error' not in r.call('review_project_work', assignment_id=worker['id'], result_revision=1, decision='accept', note='Verified')
    change = r.c.repo._change
    inserted = []
    def race(update, **options):
        if not inserted:
            inserted.append(True)
            delegate(r, 'parallel-work')
        return change(update, **options)
    monkeypatch.setattr(r.c.repo, '_change', race)
    assert 'Unfinished/unreviewed' in r.call('finish_project_request', **report(r))['error']
    assert not r.c.repo._load()[0].get('coordinatorSettlements')


def test_unconfirmed_worker_usage_does_not_stop_the_request(run, monkeypatch):
    """Regression for coordinator-1a390889: unconfirmed usage exhausted the request."""
    from tests.project_mod.test_project_dispatch_repository import model_attempt
    r = run
    monkeypatch.setattr('runtime_guards.task_concurrency_hard_ceiling', lambda: 10)
    workers = [delegate(r, str(i)) for i in range(8)]
    r.attempts['coordinator-attempt']['status'] = 'completed'
    for row in workers:
        r.c.repo.mutate(row['id'], lambda value: value['modelAttempts'].append(model_attempt(value['id'])))
        end(r, row, 'interrupted', delivered=False)
    assert r.wake() == ['accepted'] and not r.reports
    activate(r)
    assert delegate(r, 'retry-after-failures')['request']['budget'] == {}


def test_cancel_active_worker_withdraws_grant_and_aborts_exact_attempt(run):
    r = run; done = delegate(r); active = delegate(r, 'active')
    end(r, done); r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']; activate(r)
    cancelled = r.call('cancel_project_work', assignment_id=active['id'])
    assert cancelled['revoked'] and r.attempts[active['execution']['attemptId']]['status'] == 'interrupted'
    # The Worker's stop names who revoked it, so its error is traceable.
    assert 'cancel_project_work' in r.cancel_provenance and active['id'] in r.cancel_provenance
    assert f"Coordinator conversation {r.task['convId']}" in r.cancel_provenance
    assert r.attempts[controls(r)[0]['execution']['attemptId']]['status'] == 'running'
    assert 'error' in r.call('cancel_project_work', assignment_id=controls(r)[0]['id'])


def test_failed_settlement_only_wakes_coordinator_and_leaves_human_queue_alone(monkeypatch):
    from lib import turn_lifecycle, message_queue
    from lib.project_dispatch import recovery
    calls = []
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(threading, 'Thread', Thread)
    def forbidden(*args, **kwargs): pytest.fail('Failed turn must not drain ordinary queues')
    monkeypatch.setattr(message_queue, 'get_queue_depth', forbidden)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued', forbidden)
    monkeypatch.setattr(message_queue, 'dispatch_completed_background_batch', forbidden)
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', lambda *a, **kw: calls.append((a, kw)))
    turn_lifecycle._drain_queue_after_settlement({'_userId': 7}, 'failed-worker', 'failed')
    assert calls == [(('failed-worker',), {'user_id': 7, 'coordinator_only': True})]


def test_failed_lead_settlement_wakes_it_for_messages_its_turn_never_read(monkeypatch):
    """A project message to a lead whose Turn failed is never left pending without a reader.

    Its pending originals are offered at every round of the lead's next Turn,
    but a failed Turn started none: the drain wakes the idle lead once per
    pending frontier (``wake_idle_lead``), exactly as a completed one does.
    """
    from lib import turn_lifecycle
    from lib.project_dispatch import recovery
    woken = []
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(threading, 'Thread', Thread)
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', lambda *a, **kw: None)
    monkeypatch.setattr('lib.conversations.project_messages.wake_idle_lead',
                        lambda owner, recipient: woken.append((owner, recipient)) or 'started')
    turn_lifecycle._drain_queue_after_settlement({'_userId': 7}, 'coordinator-lead', 'failed')
    assert woken == [(7, 'coordinator-lead')]


@pytest.mark.parametrize('abort_reason,stopping', [
    (None, False),
    ('server_shutdown', True),    # the stopping process's own quiesce
    # NDP 2026-11-11 (e11ce7a7): a serving process's durable worker settled the
    # Attempt an earlier process left, still stamped server_shutdown.
    ('server_shutdown', False),
    ('restart_recovery', False),
])
def test_only_a_stopping_process_leaves_successors_to_startup_recovery(
        monkeypatch, abort_reason, stopping):
    from lib import turn_lifecycle, message_queue
    from lib.project_dispatch import recovery
    calls = []
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(threading, 'Thread', Thread)
    monkeypatch.setattr('lib.tasks_pkg.manager.task_admission_closed', lambda: stopping)
    def record(name, value):
        def call(*args, **kwargs):
            if stopping:
                pytest.fail('A stopping server must not start successor work: ' + name)
            calls.append(name)
            return value
        return call
    monkeypatch.setattr(message_queue, 'get_queue_depth', record('queue_depth', 0))
    monkeypatch.setattr(message_queue, 'dispatch_completed_background_batch', record('background', None))
    monkeypatch.setattr(recovery, 'recover_conversation_dispatch', record('dispatch_recovery', None))
    task = {'_userId': 7}
    if abort_reason:
        task.update(aborted=True, _abort_reason=abort_reason)
    turn_lifecycle._drain_queue_after_settlement(task, 'worker', 'interrupted')
    assert calls == ([] if stopping else ['queue_depth', 'background', 'dispatch_recovery'])


def _lane_lister(r):
    """``list_turns`` over the fake Turns: one lane after one ordinal, oldest first."""
    def list_turns(conversation_id, *, user_id, lane_id=None, after_ordinal=None, light=False, **kwargs):
        rows = [deepcopy(turn) for turn in r.turns.values()
                if turn.get('conversationId') == conversation_id and 'ordinal' in turn
                and (lane_id is None or turn.get('laneId', 'main') == lane_id)
                and (after_ordinal is None or turn['ordinal'] > after_ordinal)]
        return {'turns': sorted(rows, key=lambda turn: turn['ordinal'])}
    return list_turns


def _restart_pause(r, turn_id, attempt_id):
    """What restart settlement leaves: the exact Attempt and its Turn interrupted by the restart."""
    r.attempts[attempt_id]['status'] = 'interrupted'
    r.turns[turn_id].update(status='interrupted', currentAttemptId=attempt_id, projectionRevision=4,
        settlement={'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue', 'anchor': {}}]})
    r.turns[turn_id].setdefault('ordinal', 1)
    r.turns[turn_id].setdefault('laneId', 'main')
    r.c.repo.turn_lister = _lane_lister(r)


def test_a_restart_paused_coordinator_wake_continues_in_place_instead_of_a_new_wake(run, monkeypatch):
    """A restart interrupted the Coordinator's continuation Turn: it resumes from its
    checkpoint in the same Turn under the same grant, like a restart-paused Worker,
    instead of a fresh 'Continue the original project request' Turn that re-reads every
    Worker (2026-11-08 16:55: both Coordinators got a new wake)."""
    r = run
    ready(r)
    [row] = controls(r)
    turn_id, attempt_id = row['execution']['turnId'], row['execution']['attemptId']
    _restart_pause(r, turn_id, attempt_id)
    resumed = []

    def resume_turn(conversation, turn, owner, body, **kwargs):
        successor = 'continued-' + attempt_id
        r.attempts[successor] = {'attemptId': successor, 'conversationId': conversation, 'turnId': turn,
                                 'taskId': r.attempts[attempt_id]['taskId'], 'status': 'running',
                                 'createdAt': 1_003_000}
        r.turns[turn].update(status='running', currentAttemptId=successor)
        kwargs['trusted_dispatch'].continue_execution(turn, successor)
        resumed.append((conversation, turn, owner, deepcopy(body)))
        return SimpleNamespace(value={})
    monkeypatch.setattr(r.service.commands, 'resume_turn', resume_turn, raising=False)
    assert r.wake() == ['accepted']
    assert len(controls(r)) == 1, 'no second wake Turn'
    [(conversation, turn, owner, body)] = resumed
    assert (conversation, turn, owner) == ('coordinator', turn_id, 7)
    assert body['operation'] == 'continue' and body['expectedProjectionRevision'] == 4
    assert body['commandId'] == f'project-dispatch-resume:{row["id"]}:{attempt_id}'
    assert controls(r)[0]['execution']['attemptId'] == 'continued-' + attempt_id
    # The continued Attempt owns follow-through; a repeated pass starts nothing.
    assert r.wake() == [] and len(resumed) == 1


def test_a_restart_paused_human_request_continues_in_place_instead_of_a_new_wake(run, monkeypatch):
    """The Coordinator's own human request Turn was interrupted before any wake: it
    continues in place through the ordinary resume command (the one the restart resume
    pass uses, so whichever runs first wins and the other replays). No wake is minted
    while that continued Attempt runs; once it settles, Worker results wake the request."""
    from lib.conversations import restart_resume
    r = run
    worker = delegate(r)
    end(r, worker)
    _restart_pause(r, 'coordinator-turn', 'coordinator-attempt')
    resumed = []

    def resume_conversation(owner, conversation_id, *, turn_id=None, attempt_id=None):
        resumed.append((owner, conversation_id))
        r.attempts['resumed-attempt'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'resumed-attempt',
                                         'status': 'running', 'createdAt': 1_003_000}
        r.turns['coordinator-turn'].update(status='running', currentAttemptId='resumed-attempt')
        return {'outcome': 'resumed'}
    monkeypatch.setattr(restart_resume, 'resume_conversation', resume_conversation)
    assert r.wake() == ['resumed'] and controls(r) == []
    assert resumed == [(7, 'coordinator')]
    # The continued Attempt owns follow-through: no wake, no second resume.
    assert r.wake() == [] and controls(r) == [] and len(resumed) == 1
    r.attempts['resumed-attempt']['status'] = 'completed'
    r.turns['coordinator-turn'].update(status='completed')
    assert r.wake() == ['accepted'] and len(controls(r)) == 1
    assert len(resumed) == 1


def test_a_request_turn_the_owner_continued_owns_follow_through_until_it_settles(run):
    """Any in-place continuation of the request Turn (here the owner's Continue)
    keeps the Turn's current Attempt in charge: no wake races it."""
    r = run
    worker = delegate(r)
    end(r, worker)
    r.attempts['coordinator-attempt']['status'] = 'interrupted'
    r.attempts['continued-by-owner'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'continued-by-owner',
                                        'status': 'running', 'createdAt': 1_003_000}
    r.turns['coordinator-turn'].update(status='running', currentAttemptId='continued-by-owner')
    assert r.wake() == [] and controls(r) == []


@pytest.mark.parametrize('source', ['human', 'continuation'])
def test_superseded_input_does_not_restart_old_request(run, source):
    r = run
    if source == 'human':
        worker = delegate(r); end(r, worker)
        r.attempts['coordinator-attempt']['status'] = 'superseded'
    else:
        ready(r); end(r, controls(r)[-1], 'superseded', delivered=False)
    before = len(controls(r))
    assert r.wake() == [] and len(controls(r)) == before


def test_idle_callbacks_create_no_execution_threads_or_durable_growth(run):
    import time
    r = run; ready(r)
    before = json.dumps(r.c.repo._load()[0], ensure_ascii=False, separators=(',', ':')).encode()
    births, writes = len(r.calls), r.c.client.puts
    threads = {thread.ident for thread in threading.enumerate()}
    started = time.perf_counter()
    for _ in range(50):
        assert r.wake() == []
    elapsed = time.perf_counter() - started
    after = json.dumps(r.c.repo._load()[0], ensure_ascii=False, separators=(',', ':')).encode()
    assert before == after and len(r.calls) == births and r.c.client.puts == writes
    assert {thread.ident for thread in threading.enumerate()} <= threads
    print(f'Coordinator idle recovery: 50 callbacks, {elapsed:.3f}s, {len(after)} retained bytes, 0 births/writes/new threads')


@pytest.mark.parametrize('narrowed', [False, True])
def test_continuation_holds_its_role_catalog_whatever_its_record_froze(run, narrowed):
    """A continuation's catalog is its request's, the role's (docs/ROUTER.md R15), never the
    names its policy recorded: a record narrower than the request still offers messaging,
    and nothing beyond the role is ever offered."""
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble, names, wire
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_DIRECT_TOOLS, COORDINATOR_TOOLS

    r = run
    if narrowed:
        controls = set(CONTRACT['continuationTools']) - {'send_project_message', 'consider_project_message'}
        capture_worker_policy(r.task, r.task['config'], [
            {'function': {'name': name}} for name in controls | {'run_command', 'read_files'}], 'model-a',
            worker_model='model-a')
    ready(r)
    row = activate(r)
    assert ('send_project_message' in row['request']['profile']['tools']) is not narrowed
    tools = assemble(r.task)
    catalog = names(r.task['_executable_tool_catalog'])
    assert {'send_project_message', 'consider_project_message', 'list_project_leads',
            'search_memories', 'read_tool_artifact'} <= catalog <= COORDINATOR_TOOLS
    assert set(r.task['_requiredDirectToolNames']) == COORDINATOR_DIRECT_TOOLS & catalog
    for round_num in (1, 2):
        body = wire(r.task, tools, round_num=round_num)
        assert set(r.task['_requiredDirectToolNames']) <= names(body['tools'])
        assert names(body['tools']) <= catalog | {'search_tools', 'execute_tools'}
        assert body['_programmatic_tool_calling'] == 'off'
    assert r.task['_principalContext']['kind'] == 'system' and not r.task['_attended']


def test_a_continuation_frozen_before_poll_waits_on_its_workers_through_poll(run):
    """A request issued before poll absorbed await_project_workers (2026-11-12) froze
    that name. Its continuations keep their grant unchanged and wait on the same
    Workers through poll, the name every current instruction gives."""
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble, names
    from lib.llm.llm_errors import ToolAdmissionRefused
    from lib.project_dispatch.scope import admit_tool, granted_tools
    r = run
    current = CONTRACT['continuationTools']
    CONTRACT['continuationTools'] = [
        'await_project_workers' if name == 'poll' else name for name in current]
    try:
        capture_worker_policy(r.task, r.task['config'], [
            {'function': {'name': name}} for name in set(CONTRACT['continuationTools']) | {
                'run_command', 'read_files'}], 'model-a', worker_model='model-a')
    finally:
        CONTRACT['continuationTools'] = current
    ready(r)
    row = activate(r)
    frozen = row['request']['profile']['tools']
    assert 'await_project_workers' in frozen and 'poll' not in frozen
    assert 'poll' in granted_tools(row['request']['profile'], assignment=row)
    tools = assemble(r.task)
    assert 'poll' in names(tools) and 'await_project_workers' not in names(tools)
    assert admit_tool(r.task, 'poll', {'ids': [], 'wait_s': 0}) is None
    with pytest.raises(ToolAdmissionRefused, match='original continuation policy'):
        admit_tool(r.task, 'kill', {'id': 'bg_x'})


def test_continuation_admitted_before_a_tool_flag_retirement_keeps_its_grant(run):
    """Incident 2026-11-05: a build retired ``toolSearch: auto``. After the restart
    that loaded it, every Coordinator continuation admitted earlier stopped at
    tool assembly with dispatch_authority_lost: assembly re-normalized the role's
    tool settings over the ones its grant had fixed, so the grant fence refused."""
    from dataclasses import replace
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble

    r = run
    issued = r.task['_coordinatorWorkerPolicy']
    r.task['_coordinatorWorkerPolicy'] = replace(issued, config={
        **issued.config, 'tools': {**issued.config['tools'], 'toolSearch': 'auto'}})
    ready(r)
    row = activate(r)
    assert row['request']['delegation']['policy']['config']['tools']['toolSearch'] == 'auto'
    assemble(r.task)
    assert not r.task['abort_event'].is_set()
    admission.validate_task(r.task)
    assert r.task['config']['tools']['programmaticCalling'] == 'off'


def _decision_report(r, turn_id='decision-report', ordinal=10):
    r.turns[turn_id] = {'turnId': turn_id, 'conversationId': 'coordinator', 'actor': 'assistant',
        'kind': 'coordinator_report.decision', 'createdAt': 1_002_000, 'ordinal': ordinal,
        'projection': {'content': 'Option A or B? I recommend A.', 'origin': {'initiator': 'brain'}}}
    return turn_id


def _turn_lister(r, human_after=()):
    def list_turns(conversation_id, *, user_id, lane_id, after_ordinal=None, light=False):
        assert lane_id == 'main', 'the reply is read on the decision report\'s own lane'
        return {'turns': [{'actor': 'human', 'ordinal': ordinal} for ordinal in human_after
                          if after_ordinal is None or ordinal > after_ordinal]}
    r.c.repo.turn_lister = list_turns


def _bound_request(r):
    from lib.project_dispatch.request_budget import bind_source_account
    r.task.update(status='running', usage={}, abort_event=threading.Event())
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    return bind_source_account(r.task, repository=r.c.repo)


def _pending(r):
    value, _ = r.c.repo._load()
    return value['requestBudgets'][0].get('pendingDecision')


def test_unanswered_decision_resumes_with_the_recommendation(run, monkeypatch):
    r = run; _bound_request(r); _turn_lister(r)
    timers = []
    monkeypatch.setattr(continuation, 'schedule_wake', lambda owner, project, due, **kw: timers.append(due))
    active = delegate(r)
    report_id = _decision_report(r)
    result = r.call('request_user_decision', report_turn_id=report_id,
                    recommendation='Use option A', wait_seconds=600)
    assert 'error' not in result, result
    due = result['pending_decision']['dueAt']
    assert timers == [due] and due == pytest.approx(r.c.now[0] + 600)
    assert r.call('request_user_decision', report_turn_id=report_id,
                  recommendation='Use option A', wait_seconds=60)['pending_decision']['dueAt'] == due
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == [] and r.scheduled == [due], 'an active Worker keeps the request open until the decision is due'
    r.c.now[0] = due + 1
    assert r.wake() == ['accepted']
    control = controls(r)[-1]['request']
    assert control['coordinatorWake']['reason'] == 'decision_default'
    assert 'Use option A' in control['brief'] and report_id in control['brief']
    assert _pending(r)['resolved'] == 'default_applied'
    assert r.attempts[active['execution']['attemptId']]['status'] == 'running'
    assert r.wake() == [] and len(controls(r)) == 1


def test_human_reply_cancels_the_decision_default(run, monkeypatch):
    r = run; _bound_request(r); _turn_lister(r, human_after=[11])
    monkeypatch.setattr(continuation, 'schedule_wake', lambda *a, **kw: None)
    delegate(r)
    assert 'error' not in r.call('request_user_decision', report_turn_id=_decision_report(r), recommendation='Use A')
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.c.now[0] += 10_000
    assert r.wake() == [] and not controls(r)


def test_decision_requires_a_published_decision_report(run):
    r = run; _bound_request(r)
    delegate(r)
    report_id = _decision_report(r)
    r.turns[report_id]['kind'] = 'coordinator_report.progress'
    assert 'decision' in r.call('request_user_decision', report_turn_id=report_id, recommendation='Use A')['error']



@pytest.mark.parametrize('asks', [True, False])
def test_continuation_brief_names_only_tools_its_grant_holds(run, asks):
    """The brief told every continuation to call request_user_decision after that tool
    left the Coordinator ceiling, so a continuation asking the human was refused.
    A grant issued before ask_human was frozen into continuations cannot ask at all."""
    import re
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    r = run
    if not asks:
        names = CONTRACT['continuationTools'] + ['run_command', 'read_files', 'write_file']
        capture_worker_policy(r.task, r.task['config'],
                              [{'function': {'name': name}} for name in names if name != 'ask_human'],
                              'model-a', worker_model='model-a')
    ready(r)
    request = controls(r)[0]['request']
    held = set(request['profile']['tools'])
    assert ('ask_human' in held) is asks
    known = COORDINATOR_TOOLS | set(CONTRACT['workerDeniedTools']) | set(CONTRACT['continuationTools'])
    named = {name for name in known if re.search(rf'\b{re.escape(name)}\b', request['brief'])}
    assert named <= held, sorted(named - held)
    assert ('ask_human' in named) is asks

def test_timed_wake_keeps_one_timer_per_project_and_the_earliest_due_time(monkeypatch):
    fired = []
    monkeypatch.setattr(continuation, 'recover_coordinator', lambda repository: fired.append(repository))
    class Repo:
        def __init__(self, owner, project): self.owner, self.project = owner, project
    monkeypatch.setattr('lib.project_dispatch.repository.ProjectDispatchRepository', Repo)
    now = [1000.0]
    continuation.schedule_wake(7, '/p', 5000, clock=lambda: now[0])
    first = continuation._TIMED_WAKES[(7, '/p')][1]
    continuation.schedule_wake(7, '/p', 9000, clock=lambda: now[0])
    assert continuation._TIMED_WAKES[(7, '/p')][1] is first
    continuation.schedule_wake(7, '/p', now[0] - 5, clock=lambda: now[0])
    timer = continuation._TIMED_WAKES[(7, '/p')][1]
    assert timer is not first and not first.is_alive()
    timer.join(3)
    assert len(fired) == 1 and (7, '/p') not in continuation._TIMED_WAKES
