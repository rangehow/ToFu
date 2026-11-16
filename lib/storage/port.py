"""Backend-neutral application storage port.

Repositories consume semantic operations from the generated operation catalog.
Adapters own transport, error translation and committed-event delivery; runtime
owns readiness and write fencing. No driver, lifecycle or wire imports belong here.
``query_many`` is the one entry point for several independent reads: the
storage.v1 and storage.v2 adapters answer them in one pipelined exchange.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


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
              deadline: float | None = None) -> Any: ...

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
