"""Independent API processes observe committed conversation invalidations."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_turn_birth_fences_settlement_without_changing_command_replay(semantic_authority, monkeypatch):
    from lib import turn_lifecycle as turns
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    conversation = uuid.uuid4().hex
    parent = turns.create_turn_pair(conversation, command_id='observer', config={}, user_id=owner,
        input_projection={'content': 'Observe results'},
        conversation_defaults={'allowCreate': True, 'title': 'Wake fence', 'settings': {}})
    attempt_id = parent['attempt']['attemptId']
    turns.bind_task(attempt_id, 'observer-task', user_id=owner)
    turns.mark_task_started(attempt_id, 'observer-task', user_id=owner)
    before = turns.get_conversation_revision(conversation, user_id=owner)
    factory(write=True).command('turn.event.record', {
        'user_id': owner, 'attempt_id': attempt_id, 'task_id': 'observer-task', 'terminal': True,
        'status': 'completed', 'projection': {'content': 'Observed'},
        'settlement': {'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}}, uuid.uuid4().hex)
    kwargs = {'command_id': 'wake', 'config': {}, 'user_id': owner, 'input_actor': 'virtual_user',
              'input_projection': {'content': 'Wake'}, 'require_lane_idle': True}
    with pytest.raises(turns.LifecycleConflict) as stale:
        turns.create_turn_pair(conversation, expected_conversation_revision=before, **kwargs)
    assert stale.value.code == 'lane_advanced'
    assert len(turns.list_turns(conversation, user_id=owner)['turns']) == 2
    current = turns.get_conversation_revision(conversation, user_id=owner)
    born = turns.create_turn_pair(conversation, expected_conversation_revision=current, **kwargs)
    replay = turns.create_turn_pair(conversation, expected_conversation_revision=before, **kwargs)
    assert replay['idempotentReplay'] and replay['attempt']['attemptId'] == born['attempt']['attemptId']


def test_retired_generation_recovers_legacy_start_marker_not_live_claim(semantic_authority, monkeypatch):
    from lib import turn_lifecycle as turns
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    conv = uuid.uuid4().hex
    accepted = turns.create_turn_pair(conv, command_id='old-project-pair', config={}, user_id=owner,
        input_projection={'content': 'Accepted before retirement'},
        conversation_defaults={'allowCreate': True, 'title': 'Old claim', 'settings': {}})
    ident = accepted['attempt']['attemptId']
    assert turns.claim_attempt_start(ident, user_id=owner)
    marker = turns.get_attempt(ident, user_id=owner)['taskId']
    assert marker.startswith('@dispatching:')
    created = accepted['attempt']['createdAt']
    # A live generation is outside boot's authoritative cutoff/exclusion.
    assert turns.recover_running_attempts(created_before_ms=created) == 0
    assert turns.recover_running_attempts(created_before_ms=created + 1, exclude_task_ids=[marker]) == 0
    # The existing retired-generation sweep sees pre-bind markers even though
    # ordinary accepted-start discovery refuses to adopt them.
    assert turns.recover_running_attempts(created_before_ms=created + 1) == 1
    assert turns.get_attempt(ident, user_id=owner)['status'] == 'interrupted'
    assert turns.recover_running_attempts(created_before_ms=created + 1) == 0


def test_conversation_watch_tracks_commit_without_worker_queue_noise(semantic_authority):
    factory, owner, _engine = semantic_authority
    client = factory()
    before = client.conversation_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))
    conversation = 'watch-' + uuid.uuid4().hex
    from lib import turn_lifecycle as turns
    from unittest.mock import patch
    with patch.object(turns, '_turn_client', lambda **kw: factory(**kw)):
        turns.create_turn_pair(conversation, command_id=conversation,
            input_projection={'content': 'watch'}, config={}, user_id=owner,
            conversation_defaults={'allowCreate': True, 'title': 'Watch', 'settings': {}})
    after = client.conversation_watch(cursor=before['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert after['cursor'] != before['cursor']
    assert after['conversations'] is None  # Commit in the gap still invalidates.
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource
    observed = threading.Event()
    source = ConversationWakeSource(factory)
    source.start(lambda scopes: observed.set())
    try:
        with patch.object(turns, '_turn_client', lambda **kw: factory(**kw)):
            turns.create_turn_pair(conversation + '-live', command_id=conversation + '-live',
                input_projection={'content': 'live'}, config={}, user_id=owner,
                conversation_defaults={'allowCreate': True, 'title': 'Watch', 'settings': {}})
        assert observed.wait(5)
    finally:
        source.close()
    assert not source._thread.is_alive()



@pytest.mark.unit
def test_conversation_wait_coalesces_scopes_and_ignores_worker_changes():
    from lib.storage_authority_sql.worker_control import WorkerControl
    control = WorkerControl()
    conversation = control.conversations
    before = conversation.watch({'cursor': None}, time.monotonic())
    control.publish(True, {'worker'})
    assert conversation.watch({'cursor': before['cursor']}, time.monotonic()) == {
        **before, 'conversations': [],
    }
    waiting = threading.Event()
    # Synchronize enrollment rather than relying on the scheduler.
    condition = conversation._condition
    original_wait = condition.wait
    def enrolled(timeout=None):
        waiting.set()
        return original_wait(timeout)
    condition.wait = enrolled
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(conversation.watch, {'cursor': before['cursor']}, time.monotonic() + 5)
        assert waiting.wait(5)
        control.publish(False, set(), {(41, 'one'), (42, 'one'), (41, 'one')})
        result = pending.result(5)
    assert result['conversations'] == [
        {'userId': 41, 'conversationId': 'one'},
        {'userId': 42, 'conversationId': 'one'},
    ]
    assert conversation._waiters == {}
    control.close()


def test_conversation_watch_close_leaves_other_requests_live(semantic_authority):
    from tofu_db._request_cancellation import RequestCancellation
    factory, _owner, _engine = semantic_authority
    client = factory()
    cursor = client.conversation_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))['cursor']
    cancellation = RequestCancellation()
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.conversation_watch, cursor=cursor,
            deadline_unix_ms=int((time.time() + 60) * 1000), cancellation=cancellation)
        cancellation.cancel()
        with pytest.raises(Exception):
            pending.result(5)
    assert factory().worker_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))['cursor']
