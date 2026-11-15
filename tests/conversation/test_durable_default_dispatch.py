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
