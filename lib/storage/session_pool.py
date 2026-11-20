"""Demand-created exclusive loopback sessions with bounded idle reuse.

Responsibility: one pool owner shared by the storage.v1 (SQL Sidecar) and
storage.v2 (native) clients, so both transports reuse authenticated sockets
under the same lifecycle: idle size bounds retention only, never admission;
an idle session is reused only inside its transport's freshness cap and after
its liveness probe; broken sessions are never reused.
Entry point: ``SessionPool``. Dependencies: ``lib.log``, ``lib.storage.errors``.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)


class SessionPool:
    """Demand-created exclusive sessions with a bounded idle reuse cache.

    Size controls retained idle sockets, never active requests. Each caller
    immediately claims an idle session or a new creation reservation, then
    probes/negotiates outside the lock under its own attempt deadline. Close
    and deadline fences are checked before returning the owned session.
    Closing sockets remain observable until teardown finishes; they cannot
    block unrelated callers. Broken sessions are never reused.
    """

    def __init__(self, factory, size: int, *, idle_cap_s: float):
        if size < 1:
            raise ValueError('idle session target must be positive')
        self._factory = factory
        self._size = size
        self._idle_cap_s = float(idle_cap_s)
        self._idle: list = []
        self._live = 0
        self._retiring = 0
        self._closed = False
        self._completed = 0
        self._condition = threading.Condition()

    @property
    def size(self) -> int:
        return self._size

    def set_idle_cap_s(self, idle_cap_s: float) -> None:
        """Adopt the peer's advertised freshness bound for later reuse."""
        self._idle_cap_s = float(idle_cap_s)

    def resize(self, size: int) -> None:
        """Resize only idle retention; socket teardown runs outside the lock."""
        if size < 1:
            return
        retired = []
        with self._condition:
            self._size = size
            while len(self._idle) > size:
                session, _released_at = self._idle.pop()
                self._retiring += 1
                retired.append(session)
        for session in retired:
            self._retire_detached_session(session)

    def _retire_detached_session(self, session) -> None:
        try:
            self._close_quietly(session)
        finally:
            with self._condition:
                self._live -= 1
                self._retiring -= 1

    def _discard_reserved_session(self, session) -> None:
        with self._condition:
            self._retiring += 1
        self._retire_detached_session(session)

    def resource_snapshot(self) -> dict[str, int]:
        with self._condition:
            return {'capacity': self._size, 'active_capacity': 0,
                    'idle_reuse_capacity': self._size,
                    'used': self._live - len(self._idle), 'idle': len(self._idle),
                    'retiring': self._retiring, 'waiting': 0,
                    'completed': self._completed}

    @staticmethod
    def _close_quietly(session) -> None:
        closer = getattr(session, 'close', None)
        if closer is None:
            return
        try:
            closer()
        except Exception as _e:
            logger.debug('close quietly failed: %s', _e)
            pass

    def _reusable(self, session, released_at: float) -> bool:
        """One idle session may be reused only while it is fresh and the
        peer has not already closed it (a FIN/RST queued on an idle socket
        is the daemon's idle reaping or a restart — both are invisible until
        probed). Sessions without a probe (test doubles) rely on the
        freshness cap alone."""
        if time.monotonic() - released_at > self._idle_cap_s:
            return False
        probe = getattr(session, 'probe_liveness', None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception as _e:
            logger.debug('check reusable failed: %s', _e)
            return False

    def acquire(self, timeout: float, *, cancellation=None):
        """Claim immediately, then validate the exclusive socket unlocked."""
        deadline = time.monotonic() + max(0.001, timeout)
        while True:
            if cancellation is not None and cancellation.cancelled:
                raise OSError('Transport request was cancelled before dispatch')
            session = None
            released_at = None
            with self._condition:
                if self._closed:
                    raise StorageError('database_unavailable',
                        'storage client is closed', True, 100,
                        request_not_dispatched=True)
                if self._idle:
                    session, released_at = self._idle.pop()
                else:
                    self._live += 1
            try:
                if released_at is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StorageError('database_timeout',
                            'storage connection budget elapsed', True, 25,
                            request_not_dispatched=True)
                    session = (self._factory(remaining) if cancellation is None else
                               self._factory(remaining, cancellation=cancellation))
                    reusable = True
                else:
                    reusable = self._reusable(session, released_at)
                with self._condition:
                    if self._closed:
                        raise StorageError('database_unavailable',
                            'storage client is closed', True, 100,
                            request_not_dispatched=True)
                    if time.monotonic() >= deadline:
                        raise StorageError('database_timeout',
                            'storage connection budget elapsed', True, 25,
                            request_not_dispatched=True)
            except BaseException:
                self._discard_reserved_session(session)
                raise
            if reusable:
                return session
            self._discard_reserved_session(session)

    def release(self, session, *, broken: bool) -> None:
        with self._condition:
            self._completed += 1
            retire = broken or self._closed or len(self._idle) >= self._size
            if retire:
                self._retiring += 1
            else:
                self._idle.append((session, time.monotonic()))
        if retire:
            self._retire_detached_session(session)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._retiring += len(idle)
        for session, _released_at in idle:
            self._retire_detached_session(session)
