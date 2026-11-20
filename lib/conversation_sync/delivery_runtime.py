"""One commit watch per API lifecycle feeds the existing conversation broker.

The broker remains a content-free hint owner. Streams always read canonical
changes with their own principal, and reconnect invalidates every local scope.
"""
from concurrent.futures import Future
from dataclasses import dataclass
import threading

from lib.storage.commit_wake import StorageCommitWakeSource
from lib.storage.errors import StorageError
from lib.storage.generated_worker_control import CONVERSATION_FORMAT

_composition = threading.Condition()
_EXTENSION_KEY = 'tofu_conversation_delivery'


class ConversationWakeSource(StorageCommitWakeSource):
    def __init__(self, client_factory, *, cancellable_factory=False):
        from lib.conversation_sync.generated_contract import STREAM_POLICY
        from lib.durable_worker.runtime_policy import runtime_policy
        super().__init__(client_factory, watch_method='conversation_watch',
            interval_ms=STREAM_POLICY['heartbeatIntervalMs'],
            backoff_ms=runtime_policy()['failureBackoffMilliseconds'],
            thread_name='conversation-commit-wake', cancellable_factory=cancellable_factory)

    def _validate(self, response):
        if (not isinstance(response, dict) or set(response) != {'format', 'cursor', 'conversations'}
            or response['format'] != CONVERSATION_FORMAT
            or not isinstance(response['cursor'], dict) or set(response['cursor']) != {'epoch', 'change'}
            or not isinstance(response['cursor']['epoch'], str)
            or type(response['cursor']['change']) is not int or not 0 <= response['cursor']['change'] < 1 << 64
            or (response['conversations'] is not None and (
                not isinstance(response['conversations'], list) or any(
                    not isinstance(scope, dict) or set(scope) != {'userId', 'conversationId'}
                    or type(scope['userId']) is not int or scope['userId'] <= 0
                    or not isinstance(scope['conversationId'], str) or not scope['conversationId']
                    for scope in response['conversations'])))):
            raise StorageError('database_protocol_error', 'Invalid conversation watch response')

    def _observe(self, cursor, response, hint):
        if cursor is None or cursor['epoch'] != response['cursor']['epoch']:
            hint(None)
        elif cursor != response['cursor']:
            hint(response['conversations'])

    def _interrupted(self, hint):
        hint(None)


@dataclass
class _Delivery:
    source: ConversationWakeSource
    enrollment: Future
    stopping: bool = False


def ensure_started(app):
    from lib.storage import get_storage_client
    from lib.conversation_sync.broker import broker
    with _composition:
        shutdown = app.extensions.get('tofu_shutdown_requested')
        if shutdown is not None and shutdown.is_set():
            return
        delivery = app.extensions.get(_EXTENSION_KEY)
        if delivery is None:
            delivery = _Delivery(ConversationWakeSource(get_storage_client, cancellable_factory=True),
                                 Future())
            app.extensions[_EXTENSION_KEY] = delivery
            owns_start = True
        elif delivery.stopping:
            return
        else:
            owns_start = False
    source, enrollment = delivery.source, delivery.enrollment
    if not owns_start:
        enrollment.result()
        return
    # Publish the cancellation owner before the synchronous first watch.
    try:
        source.start(broker.invalidate)
    except BaseException as error:
        with _composition:
            if not delivery.stopping:
                del app.extensions[_EXTENSION_KEY]
            enrollment.set_exception(error)
            _composition.notify_all()
        raise
    else:
        with _composition:
            enrollment.set_result(None)
            _composition.notify_all()


def stop(app):
    with _composition:
        delivery = app.extensions.get(_EXTENSION_KEY)
        if delivery is None:
            return
        if delivery.stopping:
            _composition.wait_for(lambda: app.extensions.get(_EXTENSION_KEY) is not delivery)
            return
        delivery.stopping = True
        delivery.source.close()
        _composition.wait_for(delivery.enrollment.done)
        del app.extensions[_EXTENSION_KEY]
        _composition.notify_all()
