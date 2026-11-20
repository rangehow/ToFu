"""Boundary counterexamples, not an installed normal-park diagnostic consumer."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.project_dispatch import continuation
from lib.project_dispatch.delegation import review_worker, source_for_task
from lib.tasks_pkg.orchestrator._tool_loop_breaker import finish_after_idle_coordinator_wait
from lib.turn_lifecycle import _settlement
from tests.project_mod.test_coordinator_continuation import (
    run, runtime, base_runtime, context, delegate, end,
)
__all__ = ['run']

pytestmark = pytest.mark.unit


def _just_delivered(r):
    parent = delegate(r)
    end(r, parent)
    checkpoint = 'a' * 40
    workspace = r.workspaces[parent['execution']['taskId']]
    workspace['checkpoint_sha'] = checkpoint
    workspace['origin']['delivery']['checkpointSha'] = checkpoint
    request = deepcopy(parent['request'])
    request['dependencies'] = [{'assignmentId': parent['id'], 'grantRevision': 1,
                                'resultRevision': 1, 'checkpointSha': checkpoint}]
    child = r.c.repo.authorize_from_coordinator(request, task=r.task, command_id='park-child')
    assert r.c.repo.dependency_states(child)[0]['status'] == 'waiting_review'
    review_worker(r.c.repo, r.task, {'assignment_id': parent['id'], 'decision': 'accept',
        'note': 'Synthetic exact result', 'result_revision': 1, 'checkpoint_sha': checkpoint})
    assert r.c.repo.dependency_states(child)[0]['status'] == 'ready'
    assert child['execution'] is None
    return child


def _candidate(task):
    state = SimpleNamespace(model='model-a', exit_reason='running')
    return finish_after_idle_coordinator_wait(task, state,
        message={'tool_calls': [{'function': {'name': 'poll'}}]}, round_num=0, tid='park-proof')


def test_one_explicit_witness_at_wait_exit_does_not_install_a_runtime_consumer(run, monkeypatch):
    r = run
    child = _just_delivered(r)
    r.call('poll', ids=[child['id']], wait_s=0)
    assert r.call('poll', ids=[child['id']], wait_s=0)['wait'] == 'timeout'
    assert r.task.get('_coordinatorIdleWaits')
    source_for_task(r.c.repo, r.task, delegated=True)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    reads = []
    witness = continuation.pre_park_witness

    def observed(*args):
        reads.append(args[2])
        return witness(*args)

    with monkeypatch.context() as patch:
        patch.setattr(continuation, 'pre_park_witness', observed)
        assert _candidate(r.task)
        assert reads == []
        # This explicit fixture read is the oracle, not a callback or runtime field.
        shown = continuation.pre_park_witness(r.c.repo, before, 'coordinator-attempt')
        assert shown == {'ready': [child['id']], 'blocked': [], 'unknown': [],
                         'unregistered_intentions': 'unknown', 'request_disposition': 'open'}
        assert reads == ['coordinator-attempt']
        patch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('extra witness'))
        patch.setattr(r.c.repo, 'dependency_states', lambda *a, **kw: pytest.fail('extra graph read'))
        assert not _candidate(r.task)
        for _ in range(2):
            assert r.call('poll', ids=[child['id']], wait_s=0)['wait'] == 'timeout'
            assert _candidate(r.task)
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert r.c.repo.read(child['id'])['execution'] is None
    assert not r.scheduled and not r.reports


@pytest.mark.parametrize('control,status,cause', [
    ('normal', 'completed', 'provider_finished'),
    ('stopped', 'interrupted', 'user_abort'),
    ('revoked', 'failed', 'dispatch_grant_revoked'),
    ('stale', 'failed', 'internal_fence_abort'),
    ('restart', 'interrupted', 'server_restart'),
    ('error', 'failed', 'generation_error'),
])
def test_wait_exit_candidate_is_not_a_terminal_normal_park_verdict(run, monkeypatch, control, status, cause):
    r = run
    child = _just_delivered(r)
    r.call('poll', ids=[child['id']], wait_s=0)
    assert r.call('poll', ids=[child['id']], wait_s=0)['wait'] == 'timeout'
    if control in ('stopped', 'revoked', 'stale', 'restart'):
        reason = {'stopped': 'user_abort', 'revoked': 'dispatch_grant_revoked',
                  'stale': 'turn_attempt_stale_fence', 'restart': 'server_shutdown'}[control]
        r.task.update(aborted=True, _abort_reason=reason)
    elif control == 'error':
        r.task['error'] = 'Synthetic finalization failure'
    monkeypatch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('candidate witness'))
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    assert _candidate(r.task)
    assert r.task['_toolLoopCleanFinish']['reason'] == 'coordinator_idle_wait'
    actual, settlement = _settlement(r.task, {'type': 'done', 'finishReason': 'stop'}, {})
    assert (actual, settlement['cause']) == (status, cause)
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert r.c.repo.read(child['id'])['execution'] is None


def test_completed_and_unobserved_controls_do_not_supply_the_missing_joint_boundary(run, monkeypatch):
    r = run
    child = _just_delivered(r)
    monkeypatch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('unobserved witness'))
    assert not _candidate(r.task)
    source_for_task(r.c.repo, r.task, delegated=True)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    with pytest.raises(PermissionError, match='current owned Coordinator attempt'):
        source_for_task(r.c.repo, r.task, delegated=True)
    # Issued births have an explicit completed-source exception, not a new read policy.
    source_for_task(r.c.repo, r.task, delegated=True, issued=True)
    normal = _settlement(r.task, {'type': 'done', 'finishReason': 'stop'}, {})
    r.task['_coordinatorIdleWaits'] = [1]
    assert _candidate(r.task)
    candidate = _settlement(r.task, {'type': 'done', 'finishReason': 'stop'}, {})
    assert normal == candidate
    assert candidate[0] == 'completed'
    assert r.c.repo.read(child['id'])['execution'] is None
    assert not r.scheduled and not r.reports


@pytest.mark.parametrize('control', ['no_successor', 'queued_successor', 'shutdown', 'autopilot'])
def test_committed_completed_drain_is_successor_arbitration_not_a_park_callback(monkeypatch, control):
    import threading
    from lib.turn_lifecycle import _drain_queue_after_settlement
    calls = []

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr('threading.Thread', InlineThread)
    monkeypatch.setattr('lib.tasks_pkg.manager.task_admission_closed', lambda: control == 'shutdown')
    monkeypatch.setattr('lib.tasks_pkg.manager.task_user_id', lambda task: 7)
    monkeypatch.setattr('lib.message_queue.get_queue_depth', lambda *a, **kw: 1)
    monkeypatch.setattr('lib.message_queue.dispatch_next_queued',
        lambda *a, **kw: 'successor' if control == 'queued_successor' else None)
    monkeypatch.setattr('lib.message_queue.dispatch_completed_background_batch', lambda *a, **kw: None)
    monkeypatch.setattr('lib.project_dispatch.recovery.recover_conversation_dispatch',
        lambda *a, **kw: calls.append(('recover', kw)))
    monkeypatch.setattr('lib.conversations.project_messages.wake_idle_lead',
        lambda *a, **kw: calls.append(('messages', kw)))
    monkeypatch.setattr(continuation, 'pre_park_witness', lambda *a: pytest.fail('drain witness'))
    monkeypatch.setattr(continuation, 'observe_turn_end',
        lambda *args: calls.append(('observed', args)))
    task = {'id': 'physical-task', '_attemptId': 'physical-attempt',
            '_toolLoopCleanFinish': {'reason': 'coordinator_idle_wait'},
            '_coordinatorWorkerReports': {'lock': threading.Lock(), 'reported': {}}}
    observation = ('observed', (7, 'coordinator', 'physical-attempt', {}))
    if control == 'autopilot':
        task['_autopilot_spawned_followup'] = True
    _drain_queue_after_settlement(task, 'coordinator', 'completed')
    if control in ('shutdown', 'autopilot'):
        assert calls == []
    elif control == 'queued_successor':
        assert calls == [observation, ('recover', {'user_id': 7, 'coordinator_only': True})]
    else:
        assert calls == [observation, ('recover', {'user_id': 7}), ('messages', {})]


def _synthetic_question(r, monkeypatch, *, blocking):
    from lib.tasks_pkg.handlers.misc import _human

    row = {'toolCallId': 'synthetic-owner-choice', 'toolName': 'ask_human', 'query': 'ask'}
    r.task.update(toolRounds=[row], messages=[])
    r.turns[r.task['_turnId']]['projection'] = {'toolRounds': r.task['toolRounds']}
    events = []
    monkeypatch.setattr(_human, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr('lib.tasks_pkg.executor._finalize.append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr('lib.router.relay.relay_project_question', lambda *a: None)
    monkeypatch.setattr('lib.router.proactive.questions.record_question', lambda *a: None)
    result = _human._handle_ask_human(
        r.task, None, 'ask_human', row['toolCallId'],
        {'question': 'Synthetic protected choice; no effect is authorized meanwhile.',
         'response_type': 'free_text', 'blocking': blocking},
        1, row, None, None, False)
    turn_id = r.task['_turnId']
    monkeypatch.setattr('lib.router.proactive.questions.open_questions',
                        lambda *a, **kw: [{'question': row['guidanceQuestion'], 'turnId': turn_id}])
    return row, events, result


def test_nonblocking_choice_keeps_ready_work_and_its_result_visible_through_repark(run, monkeypatch):
    """Synthetic lifecycle only: no model judgment, owner answer or protected-effect approval."""
    from tests.project_mod.test_coordinator_continuation import activate, controls, report

    r = run
    ready = _just_delivered(r)
    protected = r.c.repo.authorize_from_coordinator(
        deepcopy(ready['request']), task=r.task, command_id='protected-withdrawn')
    r.c.repo.revoke(protected['id'], expected_revision=1)
    still_running = delegate(r, 'still-running')
    before = deepcopy(r.c.repo._load()[0])
    calls = list(r.calls)
    row, events, result = _synthetic_question(r, monkeypatch, blocking=False)
    assert row['status'] == 'done' and row['guidanceBlocking'] is False
    assert 'without waiting' in result[1]
    assert [event['type'] for event in events] == ['tool_result']
    assert 'guidanceResponse' not in row and '_ownerGuidanceAnswers' not in r.task
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert r.c.repo.read(ready['id'])['execution'] is None
    refused = r.call('dispatch_project_assignment', assignment_id=protected['id'], expected_revision=2)
    assert 'error' in refused and r.c.repo.read(protected['id'])['execution'] is None

    started = r.call('dispatch_project_assignment', assignment_id=ready['id'], expected_revision=1)
    assert started['dispatch'] == 'accepted', started
    running = r.c.repo.read(ready['id'])
    assert r.attempts[running['execution']['attemptId']]['status'] == 'running'
    assert r.call('poll', ids=[ready['id']], wait_s=0)['workers']
    assert r.call('poll', ids=[ready['id']], wait_s=0)['wait'] == 'timeout'
    assert _candidate(r.task)
    assert not r.c.repo._load()[0].get('coordinatorSettlements')
    assert 'Unfinished/unreviewed' in r.call('finish_project_request', **report(r))['error']

    r.attempts['coordinator-attempt']['status'] = 'completed'
    end(r, running)
    assert r.wake() == ['accepted']
    wake = activate(r)
    visible = r.call('get_project_assignment', assignment_id=ready['id'])
    assert visible['execution']['status'] == 'completed'
    assert visible['execution']['resultRevision'] == 1 and visible['review'] is None
    assert r.c.repo.read(protected['id'])['grant'] == next(
        assignment['grant'] for assignment in before['assignments'] if assignment['id'] == protected['id'])
    assert r.c.repo.read(protected['id'])['execution'] is None
    assert row['status'] == 'done' and 'guidanceResponse' not in row
    assert not r.c.repo._load()[0].get('coordinatorSettlements')
    assert r.call('poll', ids=[ready['id']], wait_s=0)['workers']
    # The ordinary observation/repark suppresses repeat delivery, not the request.
    continuation.observe_turn_end(7, 'coordinator', wake['execution']['attemptId'],
                                  r.task['_coordinatorWorkerReports']['reported'])
    end(r, wake, delivered=False)
    assert r.wake() == [] and len(controls(r)) == 1
    assert not r.c.repo._load()[0].get('coordinatorSettlements')
    end(r, still_running)
    assert r.wake() == ['accepted'] and len(controls(r)) == 2


def test_protected_choice_with_no_useful_work_still_enters_existing_blocking_gate(run, monkeypatch):
    from lib.tasks_pkg import human_guidance

    r = run
    held = _just_delivered(r)
    r.c.repo.revoke(held['id'], expected_revision=1)
    before, calls = deepcopy(r.c.repo._load()[0]), list(r.calls)
    waits = []

    def unanswered(guidance_id, *, task, on_registered, abort_check):
        waits.append(guidance_id)
        on_registered()
        return None

    monkeypatch.setattr(human_guidance, 'request_human_guidance', unanswered)
    row, events, _ = _synthetic_question(r, monkeypatch, blocking=True)
    assert waits == [row['guidanceId']]
    assert row['status'] == 'awaiting_human' and 'guidanceBlocking' not in row
    assert [event['type'] for event in events] == ['human_guidance_request']
    assert 'guidanceResponse' not in row and '_ownerGuidanceAnswers' not in r.task
    assert r.c.repo._load()[0] == before and r.calls == calls
    assert r.c.repo.read(held['id'])['execution'] is None
    assert not r.scheduled and not r.reports
