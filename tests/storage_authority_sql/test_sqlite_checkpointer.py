"""The Sidecar checkpoints its WAL beside the writer, never inside a writer COMMIT.

Specification for ``lib/storage_authority_sql/adapters/sqlite_checkpointer.py``
(docs/STORAGE.md, "Checkpoints run beside the writer"):

- the writer connection runs no automatic checkpoint (``wal_autocheckpoint=0``);
- once the WAL holds SQLite's 4,096-frame checkpoint size, the checkpointer
  backfills it on its own connection, and the writer, even when it commits
  back to back, finishes the remainder and restarts the WAL, so the WAL stays
  near the checkpoint size as it did with the inline checkpoint;
- a writer commit completes while a checkpoint pass is still running;
- ``tofu_wal_frames()`` is NULL on a connection that bypasses the shim.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from lib.storage_authority_sql.adapters import sqlite_checkpointer
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.adapters.sqlite_checkpointer import WAL_AUTOCHECKPOINT_FRAMES
from lib.storage_authority_sql.adapters.sqlite_wal_coalesce import wal_coalescing
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytestmark = pytest.mark.unit


@pytest.fixture
def backend(tmp_path):
    if wal_coalescing().vfs is None:
        pytest.skip(wal_coalescing().reason)
    instance = SQLiteBackend(_config(tmp_path))
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


def _write(backend, rows: int) -> None:
    def op(session):
        session.execute('CREATE TABLE IF NOT EXISTS bulk(payload BLOB)')
        for _ in range(rows):
            session.execute('INSERT INTO bulk VALUES (randomblob(3500))')
    backend._writer.submit(op, 'user', time.monotonic() + 30, operation_name='test.bulk')


def _wal_index(backend) -> tuple[int, int]:
    probe = backend._connect(query_only=True)
    try:
        probe.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return tuple(probe.execute('SELECT tofu_wal_frames(), tofu_wal_backfilled()').fetchone())
    finally:
        probe.close()


def _eventually(predicate, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, 'condition not reached'
        time.sleep(0.02)


def test_the_writer_runs_no_automatic_checkpoint(backend):
    autocheckpoint = backend._writer._connection.execute('PRAGMA wal_autocheckpoint').fetchone()[0]
    assert autocheckpoint == 0
    assert backend.metrics()['wal_checkpointer']['active'] is True


def test_continuous_writes_restart_the_wal(backend):
    # Back-to-back commits from several threads keep the writer busy, so it
    # never begins a transaction while the checkpointer is idle; the writer
    # finishes a due checkpoint and the WAL restarts while they continue.
    stop = threading.Event()

    def keep_writing():
        while not stop.is_set():
            _write(backend, 50)

    threads = [threading.Thread(target=keep_writing) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        peak = 0

        def restarted() -> bool:
            nonlocal peak
            frames, _ = _wal_index(backend)
            peak = max(peak, frames)
            return peak >= WAL_AUTOCHECKPOINT_FRAMES and frames < peak
        _eventually(restarted, timeout_s=120)
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    status = backend.metrics()['wal_checkpointer']
    assert status['passes'] + status['writer_passes'] > 0


def test_a_writer_commit_does_not_wait_for_a_checkpoint_pass(backend, monkeypatch):
    in_pass, release = threading.Event(), threading.Event()
    checkpoint = sqlite_checkpointer.WalCheckpointer._checkpoint_if_due

    def slow_pass(self):
        in_pass.set()
        release.wait(30)
        checkpoint(self)

    monkeypatch.setattr(sqlite_checkpointer.WalCheckpointer, '_checkpoint_if_due', slow_pass)
    try:
        _write(backend, 1)
        assert in_pass.wait(30)
        started = time.monotonic()
        _write(backend, 1)
        assert time.monotonic() - started < 10
    finally:
        release.set()


def test_the_wal_index_functions_need_the_shim(backend, tmp_path):
    plain = sqlite3.connect(tmp_path / 'plain.db')
    try:
        assert plain.execute('SELECT tofu_wal_frames(), tofu_wal_backfilled()').fetchone() == (
            None, None)
    finally:
        plain.close()
