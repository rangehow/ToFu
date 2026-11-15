"""Human steering preserves work; physical resumes retain one immutable request."""
from copy import deepcopy
import threading

import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, runtime, base_runtime, context, delegate, end, activate, controls, report,
)
from tests.project_mod.test_coordinator_request_budget import bind
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.request_budget import bind_source_account

pytestmark = pytest.mark.unit


def next_human_attempt(r, *, new_input=False):
    previous = r.task
    r.attempts[previous['_attemptId']]['status'] = 'completed'
    parent = r.turns[previous['_turnId']]['parentTurnId']
    if new_input:
        r.turns['steering-human'] = {**deepcopy(r.turns[parent]), 'turnId': 'steering-human'}
        parent = 'steering-human'
    r.turns['resumed-turn'] = {**r.turns[previous['_turnId']], 'turnId': 'resumed-turn', 'parentTurnId': parent}
    r.attempts['resumed-attempt'] = {**r.attempts[previous['_attemptId']], 'attemptId': 'resumed-attempt',
        'turnId': 'resumed-turn', 'taskId': 'resumed-task', 'status': 'running'}
    r.task = {key: value for key, value in previous.items()
              if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    r.task.update(id='resumed-task', _turnId='resumed-turn', _attemptId='resumed-attempt',
                  usage={}, abort_event=threading.Event())
    if previous.get('_coordinatorRequestBudget') is not None:
        bind_source_account(r.task, repository=r.c.repo)
    return previous


def test_same_human_resume_recovers_replays_reviews_and_finishes_original_workers(run):
    r = run
    bind(r)
    first = delegate(r)
    next_human_attempt(r)
    # Changing physical attempt defaults cannot rewrite the first issued policy.
    capture_worker_policy(r.task, r.task['config'], [{'function': {'name': 'new_tool'}}], 'new-model',
                          worker_model='new-model')
    page = r.call('get_project_workers')
    assert [item['assignment_id'] for item in page['workers']] == [first['id']], page
    assert page['source_attempt_id'] == 'coordinator-attempt'
    assert delegate(r)['id'] == first['id'] and len(r.created) == 1
    second = delegate(r, 'second')
    assert second['request']['delegation'] == first['request']['delegation']
    assert second['request']['model'] == first['request']['model']
    assert second['request']['profile']['tools'] == first['request']['profile']['tools']
    end(r, first)
    assert r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    args = report(r)
    r.turns['human-report']['createdAt'] = int(r.c.now[0] * 1000) + 1
    assert 'Unfinished' in r.call('finish_project_request', **args)['error']
    assert r.call('cancel_project_work', assignment_id=second['id'])['revoked']
    assert r.call('finish_project_request', **args)['reason'] == 'complete'
    r.attempts['resumed-attempt']['status'] = 'completed'
    assert r.wake() == [] and not controls(r)


def test_resume_before_first_worker_keeps_original_account_identity(run):
    r = run; bind(r)
    next_human_attempt(r)
    worker = delegate(r)
    assert worker['request']['delegation']['attemptId'] == 'coordinator-attempt'
    assert r.call('get_project_workers')['workers'][0]['assignment_id'] == worker['id']


@pytest.mark.parametrize('withdraw', ['capability', 'stop'])
def test_resumed_source_withdrawal_does_not_create_an_empty_worker(run, withdraw):
    r = run; bind(r); first = delegate(r)
    next_human_attempt(r)
    if withdraw == 'capability':
        r.task.pop('_coordinatorWorkerPolicy')
    else:
        from lib.project_dispatch.repository import record_stop
        record_stop(7, 'coordinator', r.c.now[0] + 1, client=r.c.repo.client)
    assert 'error' in r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'must-not-create'})
    assert len(r.created) == len(r.calls) == 1


def test_new_human_steering_can_find_review_and_cancel_prior_work(run):
    r = run; first = delegate(r); second = delegate(r, 'second')
    end(r, first)
    next_human_attempt(r, new_input=True)
    assert r.call('get_project_workers')['workers'] == []
    page = r.call('get_project_workers', scope='project')
    assert {item['assignment_id'] for item in page['workers']} == {first['id'], second['id']}
    assert all(not item['current_request'] and item['source_turn_id'] == 'human-turn' for item in page['workers'])
    assert r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                  decision='accept', note='Checked earlier delivery')['decision'] == 'accept'
    assert r.call('cancel_project_work', assignment_id=second['id'])['revoked']
    assert r.attempts[second['execution']['attemptId']]['status'] == 'interrupted'
    assert len(r.created) == 2


def test_automatic_continuation_can_read_but_cannot_control_another_human_request(run):
    r = run; first = delegate(r); end(r, first)
    original = next_human_attempt(r, new_input=True)
    other = delegate(r, 'other-request')
    r.attempts['resumed-attempt']['status'] = 'completed'
    r.task = original
    assert r.wake() == ['accepted']
    activate(r)
    history = r.call('get_project_workers', scope='project')
    assert other['id'] in {item['assignment_id'] for item in history['workers']}
    assert 'only its original request' in r.call('cancel_project_work', assignment_id=other['id'])['error']
    end(r, other)
    assert 'only its original request' in r.call('review_project_work', assignment_id=other['id'],
        result_revision=1, decision='accept', note='Not my request')['error']
    assert not r.c.repo.read(other['id'])['grant']['revoked']
    assert r.c.repo.read(other['id'])['review'] is None


OWNER_WORDS = '现在进展如何？之前那个没用的 Worker 停掉。'
ROUTER, ROUTER_TURN = 'router', 'router-turn'


def _later_request_continuation(r, worker):
    """The second request's automatic continuation, with an earlier request's Worker left behind.

    ``superseded``: the owner's next message replaced request one while its Worker ran on.
    ``revoked_stranded``: that Worker's grant was revoked, but its attempt was never stopped,
    so the row stays live and every restart reports it (parallel 1be49eb0, Tofu 3d589822).
    """
    earlier = delegate(r)
    next_human_attempt(r, new_input=True)
    current = delegate(r, 'current-request')
    end(r, current)
    r.attempts['resumed-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    if worker == 'revoked_stranded':
        r.c.repo.revoke(earlier['id'], expected_revision=earlier['grant']['revision'])
    row = activate(r)
    assert row['request']['delegation']['attemptId'] == 'resumed-attempt'
    r.turns[r.task['_turnId']]['currentAttemptId'] = r.task['_attemptId']
    return earlier


def _steer_into_reply(r, monkeypatch, message, *, state='delivered', router_words=OWNER_WORDS):
    """An input steered into the continuation's running reply, as turn.steer.commit stores it.

    ``owner``: the owner's own message. ``forwarded``: the Global Router's forward of the
    owner's message (R2), verified against the Router's own Turn, whose words are
    ``router_words``. ``nonhuman``: a commitment-due notice, which records no author.
    """
    command = {'owner': 'ui-steer-1', 'forwarded': 'router-forward:1', 'nonhuman': 'commitment-due:1'}[message]
    record = {'blockId': 'injection:user-steer:' + command, 'commandId': command, 'round': 2, 'count': 1,
              'previews': [{'text': OWNER_WORDS}], 'deliveryState': 'pending'}
    if message == 'owner':
        record['author'] = {'actor': 'human', 'text': OWNER_WORDS}
    elif message == 'forwarded':
        record['author'] = {'actor': 'human', 'text': OWNER_WORDS, 'origin': {
            'initiator': 'human', 'forwardedFrom': {'conversationId': ROUTER, 'turnId': ROUTER_TURN}}}
        monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False, client=None: {
            'availability': 'available', 'conversationId': ROUTER, 'projectPath': '/router-home',
            'revision': 1})
        monkeypatch.setattr('lib.router.forwarding._router_turn', lambda owner, conversation, turn_id: {
            'conversationId': ROUTER, 'turnId': ROUTER_TURN, 'actor': 'human',
            'projection': {'role': 'user', 'content': router_words}})
    reply = r.turns[r.task['_turnId']]
    reply['projectionRevision'] += 1
    reply['projection'] = {**reply['projection'], '_userSteerInjects': [dict(record)]}
    # This executor's model received it: the steer's local delivery record.
    r.task['_userSteerInjects'] = [{**record, 'deliveryState': state}]


@pytest.mark.parametrize('worker', ['superseded', 'revoked_stranded'])
@pytest.mark.parametrize('message', ['owner', 'forwarded'])
def test_an_owner_message_in_a_continuation_controls_every_worker_of_its_project(
        run, monkeypatch, message, worker):
    """INCIDENT 2026-11-14 01:08-01:11 (parallel's lead, continuation 5f4f5020 of request
    c4bbe78a): the Global Router forwarded the owner's 「现在进展如何」 into the running
    continuation, which then twice failed to cancel stranded row 1be49eb0 of an earlier
    request: 'Automatic continuation may control only its original request workers'. A reply
    the owner's message reached carries his request (as for a nonhuman wake), so it controls
    every Worker of this Coordinator binding; the continuation alone still cannot."""
    r = run
    earlier = _later_request_continuation(r, worker)
    attempt = earlier['execution']['attemptId']
    refused = r.call('cancel_project_work', assignment_id=earlier['id'])
    assert 'only its original request' in refused['error'], refused
    assert r.attempts[attempt]['status'] == 'running'
    assert r.c.repo.read(earlier['id'])['grant']['revoked'] is (worker == 'revoked_stranded')

    _steer_into_reply(r, monkeypatch, message)
    cancelled = r.call('cancel_project_work', assignment_id=earlier['id'])
    assert cancelled.get('revoked') is True, cancelled
    assert r.attempts[attempt]['status'] == 'interrupted'
    assert r.c.repo.read(earlier['id'])['grant']['revoked']
    assert 'calling cancel_project_work' in r.cancel_provenance


@pytest.mark.parametrize('case', ['nonhuman', 'forged-forward', 'queued', 'not-yet-delivered'])
def test_a_continuation_without_an_owner_message_still_controls_only_its_request(run, monkeypatch, case):
    """Only an owner message the model received lifts the continuation's scope: a notice steered
    in, a forward whose words are not the owner's, an owner message that missed the reply
    (queued as the next request) or one not yet delivered leaves it controlling only its own
    request's Workers."""
    r = run
    earlier = _later_request_continuation(r, 'superseded')
    if case == 'nonhuman':
        _steer_into_reply(r, monkeypatch, 'nonhuman')
    elif case == 'forged-forward':
        _steer_into_reply(r, monkeypatch, 'forwarded', router_words='只是问问进展。')
    elif case == 'queued':
        _steer_into_reply(r, monkeypatch, 'owner', state='queued')
    else:
        _steer_into_reply(r, monkeypatch, 'owner')
        r.task['_userSteerInjects'] = []
    refused = r.call('cancel_project_work', assignment_id=earlier['id'])
    assert 'only its original request' in refused['error'], refused
    assert r.attempts[earlier['execution']['attemptId']]['status'] == 'running'
    assert not r.c.repo.read(earlier['id'])['grant']['revoked']


def test_project_worker_history_pages_do_not_drop_earlier_requests(run):
    r = run
    identities = set()
    for index in range(34):
        worker = delegate(r, 'part-' + str(index)); identities.add(worker['id'])
        end(r, worker)
    next_human_attempt(r, new_input=True)
    first = r.call('get_project_workers', scope='project')
    assert len(first['workers']) == 32 and first['next_cursor']
    second = r.call('get_project_workers', scope='project', after_id=first['next_cursor'])
    assert len(second['workers']) == 2 and second['next_cursor'] is None
    assert {row['assignment_id'] for row in first['workers'] + second['workers']} == identities


@pytest.mark.parametrize('withdraw', ['tenant', 'epoch', 'synthetic'])
def test_human_steering_cannot_cross_owner_epoch_or_synthetic_authority(run, withdraw):
    r = run; first = delegate(r); next_human_attempt(r, new_input=True)
    if withdraw == 'tenant':
        r.task['_principalContext']['tenant_id'] = 'another-tenant'
    elif withdraw == 'epoch':
        r.c.binding.value['revision'] += 1
    else:
        r.task['_principalContext']['kind'] = 'system'
    assert 'error' in r.call('cancel_project_work', assignment_id=first['id'])
    assert not r.c.repo.read(first['id'])['grant']['revoked']
