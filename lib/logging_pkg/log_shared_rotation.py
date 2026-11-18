"""Rotating log files that several processes of one deployment write.

Responsibility
--------------
The API host (``server.py``) and the resident execution host
(``serverctl_pkg.execution_host``) each build the application logging runtime,
so each process holds its own handler on the same ``logs/`` files.
``SharedRotationMixin`` keeps those writers consistent across rollovers: no
process writes through a descriptor whose file another process renamed away,
and every writer adopts one rollover instead of repeating it.

Every record and every rollover runs under an exclusive ``flock`` on the file
currently at the handler's path. Holding it, a writer compares its descriptor
with the path and reopens the path when a peer has rotated or removed the
file. A rollover keeps the outgoing file locked until its successor is open,
so a peer waiting on the old file then finds the replacement. On the BeeGFS
FUSE mount that holds ``logs/``, a write through a renamed descriptor shares
its O_APPEND position with the new file at the old path: both files then gain
NUL holes and lose lines (docs/LOGGING.md, "Shared rotating files"). A
descriptor whose file a peer's retention already removed is stale the same way
(that mount fails its ``fstat`` with ENOENT), so the record goes to the path.

A ``flock`` belongs to the open file description, not to the descriptor, and
closing one descriptor of a description another process still holds does not
release it. Every file is therefore unlocked explicitly before its descriptor
is closed, and a forked child drops the descriptions it inherited and opens its
own: the execution shard fork server opens these files at ``import server``,
and its shards used to share that one description. A shard that closed its
copy after a rollover left the outgoing file locked for as long as the fork
server lived, and every other writer still on that file (the API host) blocked
on its next record (2026-11-18 06:08:43 until the 07:16 restart).

Entry point: ``SharedRotationMixin``, listed before a ``BaseRotatingHandler``
subclass. Dependencies: standard library only. Platforms without ``fcntl``
keep the base handler behaviour.
"""

from __future__ import annotations

import logging
import os
import time
import weakref

try:
    import fcntl
except ImportError:
    fcntl = None


_LIVE_SHARED_HANDLERS = weakref.WeakSet()


def _drop_inherited_descriptions():
    # The parent keeps its own reference, so closing this copy releases no lock
    # the parent holds. Records are flushed on every emit; nothing is pending.
    for handler in list(_LIVE_SHARED_HANDLERS):
        stream, handler.stream = handler.stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_drop_inherited_descriptions)


class SharedRotationMixin:
    """Coordinate one rotating file with the other processes that write it."""

    _shared_emit_depth = 0

    def _open(self):
        _LIVE_SHARED_HANDLERS.add(self)
        return super()._open()

    def emit(self, record):
        if fcntl is None:
            super().emit(record)
            return
        if self._shared_emit_depth:
            # Logged while this handler already holds its file (a retention
            # notice raised by the rollover itself): append, never roll over.
            try:
                logging.FileHandler.emit(self, record)
            except Exception:
                self.handleError(record)
            return
        self._shared_emit_depth += 1
        try:
            self._lock_current_file()
            try:
                if self.shouldRollover(record):
                    self._roll_over_locked()
                logging.FileHandler.emit(self, record)
            finally:
                if self.stream is not None:
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            self.handleError(record)
        finally:
            self._shared_emit_depth -= 1

    def _lock_current_file(self):
        while True:
            if self.stream is None:
                self.stream = self._open()
            descriptor = self.stream.fileno()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                opened = os.fstat(descriptor)
            except FileNotFoundError:
                # The BeeGFS FUSE mount answers ENOENT once a peer's retention
                # unlinked the file behind this descriptor; reopen the path.
                opened = None
            try:
                current = os.stat(self.baseFilename)
            except FileNotFoundError:
                current = None
            if opened is not None and current is not None and (
                    current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                # Size decisions count every writer's bytes, not only this
                # descriptor's last write.
                self.stream.seek(0, os.SEEK_END)
                return
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
            if hasattr(self, 'computeRollover'):
                # A peer rolled this period over; adopt it instead of repeating it.
                self.rolloverAt = self.computeRollover(int(time.time()))

    def _roll_over_locked(self):
        guard = os.dup(self.stream.fileno())
        try:
            self.doRollover()
            self._lock_current_file()
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)
            os.close(guard)


__all__ = ['SharedRotationMixin']
