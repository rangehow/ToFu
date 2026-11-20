"""Ordinary accepted conversations have one durable execution admission."""
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

import pytest

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def accepted(semantic_authority, monkeypatch):
    from lib import storage, turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: factory(**kwargs))
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: factory(**kwargs))
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *args, **kwargs: pytest.fail('API entered physical execution'))
    principal = PrincipalContext.user(subject_id='accepted-owner', owner_user_id=owner,
                                      tenant_id='accepted-tenant')
    identity = uuid.uuid4().hex
    pair = turns.create_turn_pair(identity, command_id=identity,
        input_projection={'content': 'accepted work'},
        config=capture_execution_identity({'model': 'accepted-model'}, owner, principal), user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Durable admission', 'settings': {}})
    service = ConversationTurnCommandService(build_user_message=lambda *args: None,
                                            was_aborted_after=lambda *args: False)
    def dispatch():
        service._start_accepted_attempt(pair, owner, {'model': 'forged-replay'}, {},
                                        abort_after_ts=None)
    return turns, factory, owner, pair, principal, dispatch


def test_default_dispatch_is_atomic_idempotent_and_preserves_accepted_owner(accepted):
    turns, factory, owner, pair, principal, dispatch = accepted
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: dispatch(), range(16)))
    attempt = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)
    assert attempt['taskId'] == 'conversation-attempt:' + attempt['attemptId']
    assert attempt['status'] == 'pending'
    job = factory().query('worker_job.get', {'task_id': attempt['taskId'], 'user_id': owner})
    assert job['status'] == 'queued'
    assert job['payload']['principal'] == principal.to_payload()
    assert job['payload']['attemptId'] == attempt['attemptId']
    assert job['payload']['conversationId'] == pair['turn']['conversationId']
    assert 'model' not in job['payload']


def test_lost_dispatch_ack_keeps_the_single_bound_job(accepted, monkeypatch):
    turns, factory, owner, pair, _, dispatch = accepted
    lost = []
    class Client:
        def __getattr__(self, name):
            return getattr(factory(write=True), name)
        def command(self, operation, *args, **kwargs):
            result = factory(write=True).command(operation, *args, **kwargs)
            if operation == 'turn.attempt.dispatch_worker' and not lost:
                lost.append(result['job']['taskId'])
                raise StorageError('database_timeout', 'Committed dispatch lost its ACK', True)
            return result
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: Client())
    with pytest.raises(StorageError, match='ACK'):
        dispatch()
    dispatch()
    attempt = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)
    assert attempt['taskId'] == lost[0]
    assert attempt['status'] == 'pending'


def test_stop_before_dispatch_never_creates_work(accepted):
    turns, factory, owner, pair, _, dispatch = accepted
    identity = pair['attempt']['attemptId']
    turns.abort_attempt(identity, user_id=owner)
    dispatch()
    assert turns.get_attempt(identity, user_id=owner)['status'] == 'interrupted'
    assert factory().query('worker_job.get', {
        'task_id': 'conversation-attempt:' + identity, 'user_id': owner}) is None


def test_stop_racing_dispatch_cancels_the_newly_bound_job(accepted, monkeypatch):
    """Stop read the attempt unbound, then the turn command bound it to a worker."""
    turns, factory, owner, pair, _, dispatch = accepted
    identity = pair['attempt']['attemptId']
    raced = []
    class Client:
        def __init__(self, write=False):
            self._client = factory(write=write)
        def __getattr__(self, name):
            return getattr(self._client, name)
        def query(self, operation, payload, *args, **kwargs):
            result = self._client.query(operation, payload, *args, **kwargs)
            if (operation == 'turn.attempt.get' and payload.get('resolve_executor')
                    and not raced):
                raced.append(result['taskId'])
                dispatch()
            return result
    monkeypatch.setattr(turns, '_turn_client', lambda write=False, **kwargs: Client(write))
    outcome = turns.abort_attempt(identity, user_id=owner)
    assert raced == ['']
    assert outcome['status'] == 'abort_signaled'
    job = factory().query('worker_job.get', {
        'task_id': 'conversation-attempt:' + identity, 'user_id': owner})
    assert job['cancelRequestedAtMs'] > 0


def _dispatch_started_at(accepted, started_at):
    """Dispatch through the production Stop fence, as the turn command does."""
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync.pending_abort import was_pending_abort_after
    _, _, owner, pair, _, _ = accepted
    service = ConversationTurnCommandService(build_user_message=lambda *args: None,
                                            was_aborted_after=was_pending_abort_after)
    service._start_accepted_attempt(pair, owner, {}, {}, abort_after_ts=started_at)


def test_stop_fence_reaches_a_process_that_did_not_record_it(accepted):
    from lib.conversation_sync.pending_abort import mark_pending_abort, was_pending_abort_after
    from lib.runtime.runtime_state_store import reset_for_test
    _, _, owner, pair, _, _ = accepted
    conversation = pair['turn']['conversationId']
    started = time.time()
    stopped_at = mark_pending_abort(conversation, owner)
    reset_for_test()  # The execution host shares storage, never this process's memory.
    assert was_pending_abort_after(conversation, owner, started)
    assert not was_pending_abort_after(conversation, owner, stopped_at + 0.001)
    assert not was_pending_abort_after(conversation, owner + 1, started)


def test_stop_fence_keeps_the_latest_stop_within_its_window(accepted):
    from lib.conversation_sync.pending_abort import mark_pending_abort, was_pending_abort_after
    _, _, owner, pair, _, _ = accepted
    conversation = pair['turn']['conversationId']
    now = time.time()
    mark_pending_abort(conversation, owner, occurred_at=now - 10)
    mark_pending_abort(conversation, owner, occurred_at=now - 20)  # A delayed, older Stop.
    assert was_pending_abort_after(conversation, owner, now - 15)
    expired = 'expired-' + conversation
    mark_pending_abort(expired, owner, occurred_at=now - 301)
    assert not was_pending_abort_after(expired, owner, now - 400)


def test_stop_while_the_turn_persists_settles_instead_of_dispatching(accepted):
    from lib.conversation_sync.pending_abort import mark_pending_abort
    turns, factory, owner, pair, _, _ = accepted
    identity = pair['attempt']['attemptId']
    started = time.time()
    mark_pending_abort(pair['turn']['conversationId'], owner)
    _dispatch_started_at(accepted, started)
    assert turns.get_attempt(identity, user_id=owner)['status'] == 'interrupted'
    assert factory().query('worker_job.get', {
        'task_id': 'conversation-attempt:' + identity, 'user_id': owner}) is None


def test_an_earlier_stop_never_fences_a_later_send(accepted):
    from lib.conversation_sync.pending_abort import mark_pending_abort
    turns, factory, owner, pair, _, _ = accepted
    identity = pair['attempt']['attemptId']
    stopped_at = mark_pending_abort(pair['turn']['conversationId'], owner)
    _dispatch_started_at(accepted, stopped_at + 0.001)
    assert turns.get_attempt(identity, user_id=owner)['status'] == 'pending'
    assert factory().query('worker_job.get', {
        'task_id': 'conversation-attempt:' + identity, 'user_id': owner})['status'] == 'queued'


@pytest.mark.parametrize('fault', ['before_commit', 'lost_ack', 'claimed', 'stopped'])
def test_project_bind_refusal_keeps_exact_attempt_recoverable(accepted, monkeypatch, fault):
    from lib.conversation_sync import command_service
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchError

    turns, factory, owner, _, principal, _ = accepted
    conversation = uuid.uuid4().hex
    body = {'commandId': 'project-birth', 'input': 'approved work',
            'conversation': {'allowCreate': True, 'title': 'Project birth'}}
    service = command_service.ConversationTurnCommandService(
        build_user_message=lambda *a: None, was_aborted_after=lambda *a: False)
    binding = admission.TrustedDispatch({})
    monkeypatch.setattr(admission.TrustedDispatch, 'capture', lambda *a: {})
    monkeypatch.setattr(admission.TrustedDispatch, 'validated',
                        lambda self: {'grant': {'createdAt': 0}, 'execution': None, 'request': {}})
    named_attempts = []

    def refuse(turn_id, attempt_id):
        named_attempts.append((turn_id, attempt_id))
        raise ProjectDispatchError('definitive bind refusal')

    monkeypatch.setattr(admission.TrustedDispatch, 'continue_execution', lambda self, *a: refuse(*a))
    monkeypatch.setattr(admission, 'continue_recovered_execution', lambda config, *a: refuse(*a))
    writes = []

    class FaultyClient:
        def __init__(self, write=False):
            self.client = factory(write=write)

        def __getattr__(self, name):
            return getattr(self.client, name)

        def command(self, operation, payload, *args, **kwargs):
            if operation == 'turn.event.record':
                writes.append(operation)
                # After the failing producer has read the Turn, but before
                # its terminal publication reaches the actual authority.
                attempt_id = named_attempts[-1][1]
                if fault == 'claimed' and len(writes) == 1:
                    assert turns.claim_attempt_start(attempt_id, user_id=owner)
                if fault == 'stopped' and len(writes) == 1:
                    turns.abort_attempt(attempt_id, user_id=owner, withdraw_dispatch=False)
                if fault == 'before_commit' and len(writes) <= 2:
                    raise StorageError('database_timeout', 'Terminal write timed out', True)
                if fault == 'lost_ack' and len(writes) == 1:
                    self.client.command(operation, payload, *args, **kwargs)
                    raise StorageError('database_timeout', 'Terminal reply timed out', True)
            return self.client.command(operation, payload, *args, **kwargs)

    monkeypatch.setattr(turns, '_turn_client', FaultyClient)

    def send():
        return service.create_turn(conversation, owner, body, request_started_at=time.time(),
                                   trusted_dispatch=binding, principal=principal).value

    expected_error = StorageError if fault in ('before_commit', 'lost_ack') else ProjectDispatchError
    with pytest.raises(expected_error):
        send()
    turn_id, attempt_id = named_attempts[0]
    row = turns.get_attempt(attempt_id, user_id=owner)
    if fault == 'claimed':
        assert row['status'] == 'pending' and row['taskId'].startswith(f'@dispatching:{attempt_id}:')
        assert service.recover_dispatchable_attempts(created_before_ms=1 << 62)['examined'] == 0
        return  # The existing dispatch owner, not the bind refuser, owns that claim.
    assert row['taskId'] == ''
    assert row['status'] == {'before_commit': 'pending', 'lost_ack': 'failed',
                             'stopped': 'interrupted'}[fault]
    if fault == 'before_commit':
        # Even a second timeout (in recovery) must leave the exact row visible
        # for the next maintenance pass, rather than a synthetic start claim.
        assert service.recover_dispatchable_attempts(created_before_ms=1 << 62) == {
            'examined': 1, 'recovered': 0, 'settledFailed': 0}
        assert turns.get_attempt(attempt_id, user_id=owner)['taskId'] == ''
    scan = service.recover_dispatchable_attempts(created_before_ms=1 << 62)
    recovered_failure = int(fault == 'before_commit')
    assert scan == {'examined': recovered_failure, 'recovered': 0, 'settledFailed': recovered_failure}
    replay = send()
    assert replay['idempotentReplay'] and replay['attempt']['attemptId'] == attempt_id
    assert replay['turn']['turnId'] == turn_id
    assert replay['attempt']['status'] == ('interrupted' if fault == 'stopped' else 'failed')
    if fault == 'stopped':
        assert replay['turn']['settlement']['cause'] == 'user_abort'
    else:
        assert replay['turn']['settlement']['error']['kind'] == 'task_start_failed'
    assert named_attempts == [(turn_id, attempt_id)] * (1 + 2 * recovered_failure)
    events = turns.read_events(attempt_id, user_id=owner)
    terminal = [event for event in events if event['type'] == 'terminal_settlement']
    assert len(terminal) == 1 and '_requireUnboundAttempt' not in terminal[0]['payload']
    assert factory().query('worker_job.get', {
        'task_id': 'conversation-attempt:' + attempt_id, 'user_id': owner}) is None
