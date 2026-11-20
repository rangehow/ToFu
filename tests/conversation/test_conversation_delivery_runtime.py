"""Conversation delivery shares one lifecycle watch and scoped replay hints."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_scoped_hint_and_epoch_gap_wake_the_correct_subscriptions():
    from lib.conversation_sync.broker import ConversationWakeBroker
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource

    async def run():
        broker = ConversationWakeBroker()
        one, two = broker.subscribe(41, 'same'), broker.subscribe(42, 'same')
        source = ConversationWakeSource(None)
        before = {'epoch': 'one', 'change': 0}
        after = {'epoch': 'one', 'change': 1}
        source._observe(before, {'cursor': after, 'conversations': [
            {'userId': 41, 'conversationId': 'same'}]}, broker.invalidate)
        assert await one.wait(1)
        assert not await two.wait(0)
        source._observe(after, {'cursor': after, 'conversations': None}, broker.invalidate)
        assert not await one.wait(0)
        source._observe(after, {'cursor': {'epoch': 'two', 'change': 0},
                               'conversations': []}, broker.invalidate)
        assert await one.wait(1)
        assert await two.wait(1)
        one.close()
        two.close()
        assert broker.snapshot()['active'] == 0

    asyncio.run(run())


def test_delivery_lifecycle_starts_once_and_joins_before_removal(monkeypatch):
    from lib.conversation_sync import delivery_runtime as runtime
    calls = []
    class Source:
        def __init__(self, factory, **kwargs):
            calls.append('construct')
        def start(self, hint):
            calls.append('start')
        def close(self):
            assert app.extensions
            calls.append('join')
    app = SimpleNamespace(extensions={})
    monkeypatch.setattr(runtime, 'ConversationWakeSource', Source)
    runtime.ensure_started(app)
    runtime.ensure_started(app)
    runtime.stop(app)
    runtime.stop(app)
    assert calls == ['construct', 'start', 'join']
    assert app.extensions == {}


@pytest.mark.parametrize('response', [
    {},
    {'format': 'tofu.conversation-control/v1', 'cursor': {'epoch': 'a', 'change': 0},
     'conversations': [{'userId': True, 'conversationId': 'foreign'}]},
    {'format': 'tofu.conversation-control/v1', 'cursor': {'epoch': 'a', 'change': -1},
     'conversations': None},
])
def test_malformed_hints_are_refused(response):
    from lib.storage.errors import StorageError
    from lib.conversation_sync.delivery_runtime import ConversationWakeSource
    with pytest.raises(StorageError, match='Invalid conversation'):
        ConversationWakeSource(None)._validate(response)


def test_stop_after_publication_skips_client_factory(monkeypatch):
    from lib import storage
    from lib.conversation_sync import delivery_runtime as runtime

    published, interrupted, resume = (threading.Event() for _ in range(3))
    calls = []
    app = SimpleNamespace(extensions={})
    source_type = runtime.ConversationWakeSource

    class PausedSource(source_type):
        def start(self, hint):
            published.set()
            assert resume.wait(5)
            return super().start(hint)

        def interrupt(self):
            super().interrupt()
            interrupted.set()

    def factory(**kwargs):
        calls.append('factory')
        raise AssertionError('cancelled watch entered the client factory')

    monkeypatch.setattr(storage, 'get_storage_client', factory)
    monkeypatch.setattr(runtime, 'ConversationWakeSource', PausedSource)
    with ThreadPoolExecutor(max_workers=2) as executor:
        startup = executor.submit(runtime.ensure_started, app)
        try:
            assert published.wait(5)
            assert app.extensions
            stopped = executor.submit(runtime.stop, app)
            assert interrupted.wait(5)
            assert not stopped.done()
        finally:
            resume.set()
        stopped.result(2)
        startup.result(2)
    assert calls == []
    assert not app.extensions


def test_stop_interrupts_client_acquisition_during_restart(monkeypatch):
    from lib.conversation_sync import delivery_runtime as delivery
    from lib.storage import service
    from lib.storage.runtime import StorageRuntime

    supervisor = SimpleNamespace(ready=False, add_crash_callback=lambda _: None)
    runtime = StorageRuntime(supervisor)
    restart = SimpleNamespace(is_alive=lambda: True)
    runtime._restart_thread = restart
    monkeypatch.setattr(service, '_runtime', runtime)
    waiting = threading.Event()
    wait = runtime._ready_condition.wait

    def parked(_timeout):
        waiting.set()
        # Require a notification, not the runtime's normal restart poll.
        return wait(5)

    monkeypatch.setattr(runtime._ready_condition, 'wait', parked)
    app = SimpleNamespace(extensions={})
    with ThreadPoolExecutor(max_workers=2) as executor:
        startup = executor.submit(delivery.ensure_started, app)
        try:
            assert waiting.wait(5)
            stopped = executor.submit(delivery.stop, app)
            stopped.result(2)
            assert not app.extensions
            startup.result(2)
            assert runtime._restart_thread is restart
        finally:
            with runtime._ready_condition:
                runtime._restart_thread = None
                runtime._ready_condition.notify_all()


def test_retry_at_failure_publication_gets_fresh_owner(monkeypatch):
    from lib.conversation_sync import delivery_runtime as runtime

    publishing, retrying = (threading.Event() for _ in range(2))
    app = SimpleNamespace(extensions={})
    owners = []

    class Source:
        def __init__(self, factory, **kwargs):
            owners.append(self)

        def start(self, hint):
            if self is owners[0]:
                raise RuntimeError('first enrollment failed')

        def close(self):
            pass

    class ObservedFuture(Future):
        def set_exception(self, error):
            super().set_exception(error)
            publishing.set()
            assert retrying.wait(5)
            assert not app.extensions, 'failed owner was still discoverable at failure publication'

    def retry():
        assert publishing.wait(5)
        retrying.set()
        runtime.ensure_started(app)

    monkeypatch.setattr(runtime, 'ConversationWakeSource', Source)
    monkeypatch.setattr(runtime, 'Future', ObservedFuture)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(retry)
        first = executor.submit(runtime.ensure_started, app)
        with pytest.raises(RuntimeError, match='first enrollment failed'):
            first.result(5)
        pending.result(5)
    assert len(owners) == 2
    runtime.stop(app)
    assert not app.extensions
