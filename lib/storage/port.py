"""Backend-neutral application storage port.

Repositories consume semantic operations from the generated operation catalog.
Adapters own transport, error translation and committed-event delivery; runtime
owns readiness and write fencing. No driver, lifecycle or wire imports belong here.
``query_many`` is the one entry point for several independent reads: the
storage.v1 and storage.v2 adapters answer them in one pipelined exchange.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from lib.log import get_logger

logger = get_logger(__name__)


@runtime_checkable
class StoragePort(Protocol):
    """Shared contract implemented by storage.v1 and native storage.v2 adapters.

    Deadlines are relative seconds. Owners travel in semantic payloads and must
    match authenticated scope. Commands retain their caller's idempotency key;
    ambiguous writes must never be silently replayed or sent to another backend.
    All adapter failures use StorageError. Maintenance may mutate durable state.
    """

    @property
    def endpoint(self) -> tuple[str, int]: ...

    def query(self, operation: str, payload: Mapping[str, Any] | None = None,
              deadline: float | None = None, *, cancellation=None) -> Any:
        """``cancellation`` (a ``RequestCancellation``) ends the read early.

        Cancelling interrupts the in-flight exchange where the transport can,
        and the call raises instead of replaying it.
        """

    def command(self, operation: str, payload: Mapping[str, Any] | None,
                command_id: str | None, priority: str = 'user',
                deadline: float | None = None) -> Any: ...

    def health(self, deadline: float | None = 2.0) -> Any: ...

    def metrics(self, deadline: float | None = 2.0) -> Any: ...

    def maintenance(self, operation: str, payload: Mapping[str, Any] | None = None,
                    deadline: float | None = 30.0) -> Any: ...


def query_many(client: Any, requests: Sequence[tuple[str, Mapping[str, Any] | None]],
               deadline: float | None = None) -> list[Any]:
    """Independent reads; element ``i`` is ``client.query(*requests[i])`` or its StorageError.

    The selected adapters (storage.v1, native storage.v2 and embedded) answer
    them in one pipelined exchange. A single read has no exchange to share and
    goes straight through ``query``. A client without ``query_many`` (a test
    port) reads them in turn, with the same per-read outcomes.
    """
    # A single read is one query, never a batch that immediately falls back.
    pipelined = getattr(client, 'query_many', None) if len(requests) > 1 else None
    if pipelined is not None:
        return pipelined(requests, deadline=deadline)
    from lib.storage.errors import StorageError
    outcomes: list[Any] = []
    for operation, payload in requests:
        try:
            outcomes.append(client.query(operation, payload, deadline=deadline)
                            if deadline is not None else client.query(operation, payload))
        except StorageError as error:
            outcomes.append(error)
    return outcomes


def _request_key(operation: str, payload: Mapping[str, Any] | None) -> str:
    import json
    return operation + '\0' + json.dumps(payload or {}, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False, default=repr)


class PrefetchedReads:
    """Independent reads of several owners, sent together in one ``query_many``.

    The first ``query`` for any listed (operation, payload) pair sends the
    whole exchange on its own thread, or reads the answers of the exchange
    ``send_now`` already put on the wire (no hand-off to another thread, which
    in a busy interpreter waits for the lock); later ones wait for it if it is
    still in flight. Each answer serves as many reads as the request was
    listed (two owners that read one row list it twice); any other read, a
    read past those, and every read after a failed exchange go to
    ``client.query``. An answered StorageError is raised as a lone ``query``
    would raise it. Nothing is cached beyond the object.
    """

    def __init__(self, client: Any, requests: Sequence[tuple[str, Mapping[str, Any] | None]]) -> None:
        import threading
        self._client = client
        unique: dict[str, tuple[str, Mapping[str, Any] | None]] = {}
        self._remaining: dict[str, int] = {}
        for operation, payload in requests:
            key = _request_key(operation, payload)
            unique.setdefault(key, (operation, payload))
            self._remaining[key] = self._remaining.get(key, 0) + 1
        self._requests = list(unique.values())
        self._index = {key: index for index, key in enumerate(unique)}
        self._lock = threading.Lock()
        self._exchange_lock = threading.Lock()
        self._answers: list[Any] | None = None
        self._pending: Any = None

    def _exchange(self) -> list[Any] | None:
        with self._exchange_lock:
            if self._answers is None:
                pending, self._pending = self._pending, None
                try:
                    self._answers = (pending.answers() if pending is not None
                                     else query_many(self._client, self._requests))
                except Exception as error:
                    logger.debug('[StoragePort] round exchange of %d read(s) failed; each read goes through '
                                 'query instead: %s', len(self._requests), error, exc_info=True)
                    self._answers = []
            return self._answers

    def send_now(self) -> None:
        """Put the exchange on the wire now from this thread; readers take its answers.

        The authority answers while the caller works on; the first reader
        only reads the answers. A client that cannot split an exchange
        (``send_query_many`` absent or declining) leaves it to the first
        reader, as before.
        """
        send = getattr(self._client, 'send_query_many', None)
        if send is None or len(self._requests) < 2:
            return
        with self._exchange_lock:
            if self._answers is None and self._pending is None:
                try:
                    self._pending = send(self._requests)
                except Exception as error:
                    # Nothing usable was sent: the first reader sends the exchange itself.
                    logger.debug('[StoragePort] round exchange not sent early; its first reader sends it: %s', error)
                    self._pending = None

    def close(self) -> None:
        """Drop an exchange sent by ``send_now`` that no reader took, keeping its session."""
        with self._exchange_lock:
            pending, self._pending = self._pending, None
        discard = getattr(pending, 'discard', None)
        if discard is not None:
            discard()

    def _take(self, key: str) -> int | None:
        with self._lock:
            if not self._remaining.get(key):
                return None
            self._remaining[key] -= 1
            return self._index[key]

    def query(self, operation: str, payload: Mapping[str, Any] | None = None,
              deadline: float | None = None, **options) -> Any:
        index = None if options else self._take(_request_key(operation, payload))
        answers = self._exchange() if index is not None else None
        if answers:
            from lib.storage.errors import StorageError
            outcome = answers[index]
            if isinstance(outcome, StorageError):
                raise outcome
            return outcome
        return (self._client.query(operation, payload, deadline=deadline, **options)
                if deadline is not None else self._client.query(operation, payload, **options))

    def __call__(self, *, write: bool = False):
        """A client factory: reads come from here, writes from the real client."""
        if write:
            from lib.storage import get_storage_client
            return get_storage_client(write=True)
        return self
