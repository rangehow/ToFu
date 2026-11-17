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


def _audited(monkeypatch):
    from lib import log
    audited = []
    monkeypatch.setattr(log, 'audit_log', lambda event, **fields: audited.append((event, fields)))
    return audited


def test_automatic_continuation_reviews_and_cancels_another_requests_workers_of_its_binding(
        run, monkeypatch):
    """2026-11-16 (coordinator-4950501b, Turn 35df2aa7): the continuation of the first request was
    refused review of a Worker the same Coordinator dispatched in a later owner-forwarded request
    ('Automatic continuation may control only its original request workers'), so finished work
    waited for that request's own wake. Review and cancel are scoped by the Coordinator binding,
    like session control, and each call is audited."""
    r = run; first = delegate(r); end(r, first)
    original = next_human_attempt(r, new_input=True)
    other, stray = delegate(r, 'other-request'), delegate(r, 'stray')
    r.attempts['resumed-attempt']['status'] = 'completed'
    r.task = original
    assert r.wake() == ['accepted']
    activate(r)
    history = r.call('get_project_workers', scope='project')
    assert other['id'] in {item['assignment_id'] for item in history['workers']}
    audited = _audited(monkeypatch)
    end(r, other)
    reviewed = r.call('review_project_work', assignment_id=other['id'], result_revision=1,
                      decision='accept', note='Checked the later request\u2019s delivery')
    assert reviewed.get('decision') == 'accept', reviewed
    assert r.c.repo.read(other['id'])['review']['decision'] == 'accept'
    cancelled = r.call('cancel_project_work', assignment_id=stray['id'])
    assert cancelled.get('revoked') is True, cancelled
    assert r.attempts[stray['execution']['attemptId']]['status'] == 'interrupted'
    records = [fields for event, fields in audited if event == 'coordinator_session_control']
    assert [(f['action'], f['assignment_id'], f['outcome']) for f in records] == [
        ('review_project_work', other['id'], 'accepted'), ('cancel_project_work', stray['id'], 'accepted')]
    assert all(f['principal_kind'] == 'system' and f['coordinator_conversation_id'] == 'coordinator'
               and f['coordinator_turn_id'] == r.task['_turnId'] for f in records)


def _as_another_coordinators(r, monkeypatch, assignment_id):
    """Reads of ``assignment_id`` return it as another Coordinator's Worker."""
    for name in ('read', 'read_point'):
        def read(identity, *args, _original=getattr(r.c.repo, name), **kwargs):
            row = _original(identity, *args, **kwargs)
            if identity == assignment_id:
                row = {**row, 'request': {**row['request'], 'coordinatorId': 'other-coordinator'}}
            return row
        monkeypatch.setattr(r.c.repo, name, read)


@pytest.mark.parametrize('tool', ['review_project_work', 'cancel_project_work'])
@pytest.mark.parametrize('owner', ['another_coordinator', 'another_project'])
def test_review_and_cancel_of_another_coordinators_or_projects_worker_stay_refused_and_audited(
        run, monkeypatch, tool, owner):
    r = run
    earlier = _later_request_continuation(r, 'superseded')
    end(r, earlier)
    before = r.c.repo.read(earlier['id'])
    assignment_id = earlier['id']
    if owner == 'another_coordinator':
        _as_another_coordinators(r, monkeypatch, assignment_id)
    else:
        # The Coordinator's binding names one project; another project's assignment is not in it.
        assignment_id = 'project-worker:assignment-of-another-project'
    audited = _audited(monkeypatch)
    arguments = {'assignment_id': assignment_id}
    if tool == 'review_project_work':
        arguments.update(result_revision=1, decision='accept', note='Not my Worker')
    refused = r.call(tool, **arguments)
    assert 'error' in refused, refused
    if owner == 'another_coordinator':
        assert 'another Coordinator' in refused['error'], refused
    after = r.c.repo.read(earlier['id'])
    assert after['review'] == before['review'] and after['grant'] == before['grant']
    [fields] = [fields for event, fields in audited if event == 'coordinator_session_control']
    assert (fields['action'], fields['assignment_id'], fields['outcome']) == (tool, assignment_id, 'refused')


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
    the owner's message reached controls every Worker of this Coordinator binding (since
    2026-11-16 the continuation alone does too)."""
    r = run
    earlier = _later_request_continuation(r, worker)
    attempt = earlier['execution']['attemptId']
    _steer_into_reply(r, monkeypatch, message)
    cancelled = r.call('cancel_project_work', assignment_id=earlier['id'])
    assert cancelled.get('revoked') is True, cancelled
    assert r.attempts[attempt]['status'] == 'interrupted'
    assert r.c.repo.read(earlier['id'])['grant']['revoked']
    assert 'calling cancel_project_work' in r.cancel_provenance


def _session_control(r, monkeypatch, earlier, action):
    """control_conversation on ``earlier``'s Worker session, its commands and audit records captured."""
    from types import SimpleNamespace
    from lib import log, turn_lifecycle
    from lib.conversation_sync import runtime as sync_runtime
    from lib.project_dispatch.admission import TrustedDispatch
    row = r.c.repo.read(earlier['id'])
    execution = row['execution']
    trusted = TrustedDispatch(r.c.repo.admission(row['id']))
    r.attempts[execution['attemptId']].update(config=trusted.capture(
        {}, 7, trusted.principal, row['request']['recipientId']))
    if action == 'checkpoint_resume':
        r.attempts[execution['attemptId']]['status'] = 'interrupted'
        r.turns[execution['turnId']].update(status='interrupted')
    r.turns[execution['turnId']].update(currentAttemptId=execution['attemptId'], projectionRevision=3)
    monkeypatch.setattr(turn_lifecycle, 'get_turn', r.c.repo.turn_get)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', r.c.repo.attempt_get)
    commands, audited = [], []

    class Commands:
        def abort_attempt(self, attempt_id, owner):
            commands.append(('stop', attempt_id))
            return SimpleNamespace(value={'attemptId': attempt_id, 'aborted': True})

        def create_attempt(self, conversation, turn, owner, body, **kwargs):
            commands.append((body['operation'], turn))
            return SimpleNamespace(value={'turn': r.turns[turn], 'attempt': r.attempts[execution['attemptId']]})
    monkeypatch.setattr(sync_runtime, 'conversation_turn_commands', Commands())
    monkeypatch.setattr(log, 'audit_log', lambda event, **fields: audited.append((event, fields)))
    result = r.call('control_conversation', conversation_id=row['request']['recipientId'],
                    turn_id=execution['turnId'], attempt_id=execution['attemptId'], action=action,
                    expected_revision=3, idempotency_key='restart-recovery-' + action)
    return result, commands, audited


@pytest.mark.parametrize('action', ['checkpoint_resume', 'stop'])
def test_a_continuation_controls_the_sessions_of_its_coordinators_earlier_request_workers(
        run, monkeypatch, action):
    """2026-11-15: after a restart, Workers of an earlier steered request sat idle because the
    Coordinator's automatic continuation was refused 'Automatic continuation may control only
    its original request workers'. Session control is scoped by the Coordinator binding: any
    Turn of the Coordinator resumes or stops any of its own Workers, and every action is audited."""
    r = run
    earlier = _later_request_continuation(r, 'superseded')
    result, commands, audited = _session_control(r, monkeypatch, earlier, action)
    assert 'error' not in result, result
    assert [operation for operation, _ in commands] == [action]
    [(event, fields)] = audited
    assert event == 'coordinator_session_control' and fields['outcome'] == 'accepted'
    assert (fields['action'], fields['coordinator_conversation_id'], fields['principal_kind']) == (
        action, 'coordinator', 'system')


def test_session_control_of_another_coordinators_worker_stays_refused_and_is_audited(run, monkeypatch):
    r = run
    earlier = _later_request_continuation(r, 'superseded')
    from lib.project_dispatch.admission import TrustedDispatch
    checked = TrustedDispatch.checked

    def other_coordinators(self):
        row = checked(self)
        return {**row, 'request': {**row['request'], 'coordinatorId': 'other-coordinator'}}
    monkeypatch.setattr(TrustedDispatch, 'checked', other_coordinators)
    result, commands, audited = _session_control(r, monkeypatch, earlier, 'stop')
    assert 'another Coordinator' in result['error'], result
    assert commands == []
    [(event, fields)] = audited
    assert event == 'coordinator_session_control' and fields['outcome'] == 'refused'
    assert 'another Coordinator' in fields['reason']


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
