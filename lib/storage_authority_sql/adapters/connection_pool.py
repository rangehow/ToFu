"""Connection lease ownership shared by selected storage adapters.

Acquire creates a connection immediately when idle reuse is empty. All borrowed
and connecting leases remain owned through release. Close fences new leases,
joins borrowers/preparers, then closes idle handles. No database paths, SQL,
backend choice or semantic transactions belong here; owners supply a factory.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import queue
import threading
import time

from lib.storage.errors import StorageError

from lib.log import get_logger

logger = get_logger(__name__)


class _IdleSlots:
    """Idle connections, reused newest-first so a surplus ages at the cold end."""

    def __init__(self):
        self._slots: deque[ConnectionSlot] = deque()
        self._lock = threading.Lock()

    def put(self, slot: 'ConnectionSlot') -> None:
        with self._lock:
            self._slots.append(slot)

    def get_nowait(self) -> 'ConnectionSlot':
        with self._lock:
            if not self._slots:
                raise queue.Empty
            return self._slots.pop()

    def qsize(self) -> int:
        with self._lock:
            return len(self._slots)

    def take_expired(self, *, keep: int, idle_lifetime_s: float,
                     now: float) -> list['ConnectionSlot']:
        """Detach cold-end slots idle past their lifetime beyond ``keep``."""
        expired = []
        with self._lock:
            while (len(self._slots) > keep
                   and now - self._slots[0].last_used_at >= idle_lifetime_s):
                expired.append(self._slots.popleft())
        return expired


@dataclass(slots=True)
class ConnectionSlot:
    connection: object
    created_at: float
    last_used_at: float
    leased: bool = False
    owner: object | None = None
    releasing: bool = False


class DemandConnectionPool:
    def __init__(self, connect, *, idle_reuse_hint, max_lifetime_s=float('inf'),
                 idle_lifetime_s=float('inf'), classify_error=None, driver_errors=(), reset=None):
        self.connect = connect
        self.idle_reuse_hint = max(1, int(idle_reuse_hint))
        self.max_lifetime_s = max_lifetime_s
        self.idle_lifetime_s = idle_lifetime_s
        self.classify_error = classify_error
        self.driver_errors = driver_errors
        self.reset = reset
        self.idle = _IdleSlots()
        self.condition = threading.Condition()
        self.active = self.peak = self.rotations = self.connections_created = 0
        self.closed = False
        self._close_lock = threading.RLock()

    def _raise_error(self, error, deadline_at):
        if isinstance(error, self.driver_errors) and self.classify_error is not None:
            raise self.classify_error(error, deadline_at) from error
        raise error

    def acquire(self, deadline_at):
        with self.condition:
            if self.closed:
                raise StorageError('database_unavailable', 'Storage connection is not available')
            if time.monotonic() >= deadline_at:
                raise StorageError('database_timeout', 'Storage connection deadline expired', True, 25)
            try:
                slot = self.idle.get_nowait()
            except queue.Empty:
                slot = None
            if slot is not None and (slot.owner not in (None, self) or slot.leased or slot.releasing):
                raise RuntimeError('Storage idle connection belongs to another lease owner')
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            now = time.monotonic()
            if slot is None:
                slot = ConnectionSlot(self.connect(deadline_at), now, now)
                with self.condition:
                    self.connections_created += 1
            elif (now - slot.created_at >= self.max_lifetime_s
                    or now - slot.last_used_at >= self.idle_lifetime_s):
                slot.connection.close()
                slot = ConnectionSlot(self.connect(deadline_at), now, now)
                with self.condition:
                    self.connections_created += 1
                    self.rotations += 1
            if time.monotonic() >= deadline_at:
                raise StorageError('database_timeout', 'Storage connection deadline expired', True, 25)
            slot.owner = self
            slot.leased = True
            return slot
        except BaseException as error:
            try:
                if slot is not None:
                    slot.connection.close()
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()
            self._raise_error(error, deadline_at)

    def release(self, slot, *, retire=False):
        with self.condition:
            if slot.owner is not self or not slot.leased or slot.releasing:
                raise RuntimeError('Storage connection lease is not owned or was already released')
            slot.releasing = True
        cleanup_error = None
        try:
            if not retire:
                if self.reset is not None:
                    self.reset(slot.connection)
                slot.last_used_at = time.monotonic()
        except BaseException as error:
            logger.debug('release failure handled: %s', type(error).__name__)
            retire = True
            cleanup_error = error
        expired = []
        with self.condition:
            if retire:
                self.rotations += 1
            if not slot.leased:
                raise RuntimeError('Storage connection lease was already released')
            if not retire and not self.closed:
                # Demand created this connection; closing it because the idle
                # count passed the reuse hint only makes the next acquire pay
                # for a new one (SQLite re-parses the whole schema per
                # connection under its global allocator mutex: a burst above
                # the hint turned into a churn storm). A surplus beyond the
                # hint is evicted once it idles past ``idle_lifetime_s``.
                slot.leased = False
                slot.releasing = False
                self.idle.put(slot)
                expired = self.idle.take_expired(
                    keep=self.idle_reuse_hint,
                    idle_lifetime_s=self.idle_lifetime_s,
                    now=time.monotonic())
                self.rotations += len(expired)
                if not expired:
                    self.active -= 1
                    self.condition.notify_all()
                    return
        if expired:
            # This lease stays active until its evictions close, so pool close
            # still joins every connection it hands out or retires.
            try:
                for expired_slot in expired:
                    try:
                        expired_slot.connection.close()
                    except self.driver_errors as error:
                        logger.debug('idle connection close failed: %s', type(error).__name__)
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()
            return
        try:
            slot.connection.close()
        finally:
            with self.condition:
                slot.leased = False
                slot.releasing = False
                self.active -= 1
                self.condition.notify_all()
        if cleanup_error is not None:
            self._raise_error(cleanup_error, float('inf'))

    def begin_close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def close(self):
        with self._close_lock:
            self.begin_close()
            with self.condition:
                while self.active:
                    self.condition.wait()
            while True:
                try:
                    slot = self.idle.get_nowait()
                except queue.Empty:
                    break
                slot.connection.close()

    def snapshot(self):
        with self.condition:
            return {'idle': self.idle.qsize(), 'active': self.active, 'peak': self.peak,
                    'idle_reuse_hint': self.idle_reuse_hint, 'capacity': 0,
                    'rotations': self.rotations, 'connections_created': self.connections_created}
