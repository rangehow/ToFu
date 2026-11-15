"""Owner interruption of in-flight synchronous HTTP requests (docs/NETWORK.md).

Responsibility: while an owner's ``RequestInterruption`` is bound on a thread
(``lib.http_transport.outbound.network_interruption``), every managed
connection that thread's requests use joins it. ``interrupt()`` is final: it
shuts those sockets down, so a request still in connection setup or waiting
for its response (a long poll the server holds) fails at once instead of at
its timeout, and a later request under the handle is refused before it is
sent. Requests run on the binding thread and its thread-local pool, so the
handle reaches only that owner's connections, never one another request
borrows; leaving the binding forgets them.

Entry points: ``RequestInterruption``, ``RequestInterrupted``,
``bind_interruption`` and ``InterruptibleConnection`` (the managed connection
policy mixin in ``connection_setup``). Dependencies: the standard library.
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class RequestInterrupted(ConnectionAbortedError):
    """The request's owner interrupted it before it reached the network.

    An ``OSError``, so the HTTP stack reports it like any other socket failure.
    """


class _Scope:
    """One binding's connections; identity, not equality, tells bindings apart."""

    __slots__ = ('handle', 'connections')

    def __init__(self, handle: 'RequestInterruption'):
        self.handle = handle
        self.connections: set = set()


class RequestInterruption:
    """One owner's final stop for its in-flight synchronous requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interrupted = False
        self._scopes: list[_Scope] = []

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def interrupt(self) -> None:
        with self._lock:
            self._interrupted = True
            connections = [connection for scope in self._scopes for connection in scope.connections]
        for connection in connections:
            _shut_down(connection)

    def _enter(self, scope: _Scope) -> None:
        with self._lock:
            self._scopes.append(scope)

    def _leave(self, scope: _Scope) -> None:
        with self._lock:
            self._scopes = [entered for entered in self._scopes if entered is not scope]

    def _admit(self, scope: _Scope, connection) -> None:
        with self._lock:
            if self._interrupted:
                raise RequestInterrupted('The request owner interrupted it')
            scope.connections.add(connection)


_bound: ContextVar[_Scope | None] = ContextVar('tofu_request_interruption', default=None)


@contextmanager
def bind_interruption(handle: RequestInterruption) -> Iterator[RequestInterruption]:
    """Join this thread's managed connections to ``handle`` while the block runs."""
    scope = _Scope(handle)
    handle._enter(scope)
    token = _bound.set(scope)
    try:
        yield handle
    finally:
        _bound.reset(token)
        handle._leave(scope)


class InterruptibleConnection:
    """urllib3 connection policy: serve requests the bound owner can interrupt."""

    def connect(self):
        scope = _bound.get()
        if scope is None:
            return super().connect()
        scope.handle._admit(scope, self)
        result = super().connect()
        if scope.handle.interrupted:
            # interrupt() ran while the socket was being created and could not see it.
            _shut_down(self)
        return result

    def request(self, *args, **kwargs):
        scope = _bound.get()
        if scope is not None:
            scope.handle._admit(scope, self)  # a pooled connection skips connect()
        return super().request(*args, **kwargs)


def _shut_down(connection) -> None:
    sock = getattr(connection, 'sock', None)
    # urllib3's SSLTransport (TLS inside a TLS proxy tunnel) carries the socket.
    while sock is not None and not isinstance(sock, socket.socket):
        sock = getattr(sock, 'socket', None)
    if sock is None:
        return
    try:
        # The plain socket method: SSLSocket.shutdown() would also discard the
        # TLS state the blocked reader is still using. The reader sees EOF.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass  # not connected yet, or already closed


__all__ = ['InterruptibleConnection', 'RequestInterrupted', 'RequestInterruption', 'bind_interruption']
