"""Copy the SQLite WAL into the database beside the writer, never inside its COMMIT.

Responsibility: SQLite's automatic checkpoint runs inside whichever writer
COMMIT leaves the WAL at ``wal_autocheckpoint`` frames or more, on the
Sidecar's single writer thread, so every queued write waits while it copies
pages (about 0.2 ms per page write on DolphinFS; seconds after a settlement
herd). The writer therefore runs with ``wal_autocheckpoint=0`` and this
checkpointer keeps SQLite's rule on its own connection: after each writer
commit, while the WAL holds ``WAL_AUTOCHECKPOINT_FRAMES`` frames or more and
some are not yet backfilled, it runs one ``PASSIVE`` checkpoint. A PASSIVE
checkpoint takes no write lock, so commits continue while it copies.

SQLite restarts the WAL only at a write transaction that begins with the WAL
fully backfilled, and a writer that commits continuously always begins while
the checkpointer copies its previous commit. The writer therefore finishes
the checkpoint itself between batches once the WAL is due and less than one
checkpoint's worth (``WAL_AUTOCHECKPOINT_FRAMES``) is left: that copies only
what the checkpointer has not reached yet, and its next transaction restarts
the WAL. A larger backlog stays with the checkpointer. On DolphinFS this
bound matters: every wal-index region a growing WAL touches for the first
time costs about a second (docs/STORAGE.md, "Checkpoints run beside the
writer").

Entry point: :class:`WalCheckpointer`. Dependencies: standard library and a
connection opened through the coalescing VFS, whose ``tofu_wal_frames()`` and
``tofu_wal_backfilled()`` read the wal-index this process already maps.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from lib.log import get_logger

# SQLite's wal_autocheckpoint the Sidecar has always used: the WAL size at
# which a checkpoint is due. The checkpointer applies it; it is not new.
WAL_AUTOCHECKPOINT_FRAMES = 4096

logger = get_logger('tofu.storage.sidecar.sqlite')


class WalCheckpointer:
    """One thread and connection that checkpoint the WAL when SQLite would."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect
        self._condition = threading.Condition()
        self._pending = False
        self._stop = False
        self._lock = threading.Lock()
        self._metrics: dict[str, Any] = {
            'passes': 0, 'frames_backfilled': 0, 'failed_passes': 0,
            'writer_passes': 0, 'writer_frames': 0, 'max_writer_pass_ms': 0.0,
            'max_wal_frames': 0, 'last_pass_ms': 0.0, 'max_pass_ms': 0.0,
            'last_error': '',
        }
        self._connection = connect()
        # A first read maps the wal-index so the probe functions see it.
        self._connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        self._thread = threading.Thread(
            target=self._run, name='storage-sqlite-checkpointer', daemon=True)
        self._thread.start()

    def notify_commit(self) -> None:
        """Writer hook: a commit landed; check whether a checkpoint is due."""
        with self._condition:
            self._pending = True
            self._condition.notify()

    def finish_on_writer(self, connection: sqlite3.Connection) -> None:
        """Writer hook between batches: backfill a due WAL's short remainder."""
        frames, backfilled = connection.execute(
            'SELECT tofu_wal_frames(), tofu_wal_backfilled()').fetchone()
        if frames is None or frames < WAL_AUTOCHECKPOINT_FRAMES:
            return
        remainder = frames - backfilled
        if remainder <= 0 or remainder >= WAL_AUTOCHECKPOINT_FRAMES:
            return
        started = time.monotonic()
        connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        with self._lock:
            self._metrics['writer_passes'] += 1
            self._metrics['writer_frames'] += remainder
            self._metrics['max_writer_pass_ms'] = max(
                self._metrics['max_writer_pass_ms'], elapsed_ms)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {'active': True, 'wal_autocheckpoint_frames': WAL_AUTOCHECKPOINT_FRAMES,
                    **self._metrics}

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify()
        self._thread.join()
        self._connection.close()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stop:
                    self._condition.wait()
                if self._stop:
                    return
                self._pending = False
            try:
                self._checkpoint_if_due()
            except sqlite3.Error as exc:
                # The frames stay in the WAL; the next commit retries, as
                # SQLite retries a failed automatic checkpoint.
                with self._lock:
                    self._metrics['failed_passes'] += 1
                    self._metrics['last_error'] = f'{type(exc).__name__}: {exc}'[:200]
                logger.warning('SQLite WAL checkpoint failed: %s', exc)

    def _wal_index(self) -> tuple[int | None, int | None]:
        frames, backfilled = self._connection.execute(
            'SELECT tofu_wal_frames(), tofu_wal_backfilled()').fetchone()
        return frames, backfilled

    def _checkpoint_if_due(self) -> None:
        frames, backfilled = self._wal_index()
        if frames is not None:
            with self._lock:
                self._metrics['max_wal_frames'] = max(self._metrics['max_wal_frames'], frames)
            if frames < WAL_AUTOCHECKPOINT_FRAMES or backfilled >= frames:
                return
        started = time.monotonic()
        _busy, _log, checkpointed = self._connection.execute(
            'PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        with self._lock:
            self._metrics['passes'] += 1
            if backfilled is not None and checkpointed > backfilled:
                self._metrics['frames_backfilled'] += checkpointed - backfilled
            self._metrics['last_pass_ms'] = elapsed_ms
            self._metrics['max_pass_ms'] = max(self._metrics['max_pass_ms'], elapsed_ms)


__all__ = ['WAL_AUTOCHECKPOINT_FRAMES', 'WalCheckpointer']
