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

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from lib.storage_authority_sql.adapters import sqlite_checkpointer
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
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
    # Pin the reader's end mark below the real boundary. This guarantees a
    # partial PASSIVE pass, rather than hoping to sample a short-lived peak.
    def write(keys):
        def op(session):
            for key in keys:
                session.execute('INSERT INTO bulk VALUES (?, ?)', (key, f'{key:03500d}'))
        backend._writer.submit(op, 'user', time.monotonic() + 30, operation_name='test.bulk')

    def initialize(session):
        session.execute('CREATE TABLE bulk(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')

    backend._writer.submit(initialize, 'user', time.monotonic() + 30, operation_name='test.bulk')
    write(range(64))
    status = lambda: backend.metrics()['wal_checkpointer']
    assert status()['wal_autocheckpoint_frames'] == 4096
    assert backend._writer._connection.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert backend._writer._connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert _wal_index(backend)[0] < 4096
    with closing(backend._connect(query_only=True)) as reader:
        reader.execute('BEGIN')
        assert reader.execute('SELECT count(*) FROM bulk').fetchone()[0] == 64
        gate = threading.Barrier(2, timeout=30)

        def producer(start):
            for key in range(start, 4664, 100):
                gate.wait()
                write(range(key, key + 50))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(producer, start) for start in (64, 114)]
            for future in futures:
                future.result()
        _eventually(lambda: status()['passes'] > 0 and status()['frames_backfilled'] > 0)
        pinned = status()
        frames, backfilled = _wal_index(backend)
        assert frames >= 4096 and 0 < backfilled < frames
        assert reader.execute('SELECT count(*) FROM bulk').fetchone()[0] == 64
        reader.rollback()

    # A fresh commit triggers retry; require actual backfill, then a WAL reset
    # on the following commit. Neither probing nor test teardown checkpoints it.
    write(range(4664, 4665))
    def fully_backfilled():
        current, copied = _wal_index(backend)
        return current >= 4096 and current == copied

    _eventually(fully_backfilled)
    write(range(4665, 4666))
    final_index = _wal_index(backend)
    assert final_index[0] < 4096
    final = status()
    assert final['max_wal_frames'] >= 4096
    assert final['failed_passes'] == 0 and final['last_error'] == ''
    with closing(sqlite3.connect(backend.config.sqlite_path)) as reader:
        assert reader.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        rows = reader.execute('SELECT id, payload FROM bulk ORDER BY id').fetchall()
    assert rows == [(key, str(key).zfill(3500)) for key in range(4666)]
    print(json.dumps({'partial_index': [frames, backfilled], 'pinned': pinned,
                      'reset_index': final_index, 'final': final, 'content_rows': len(rows),
                      'content_sha256': hashlib.sha256(''.join(row[1] for row in rows).encode()).hexdigest(),
                      'integrity': 'ok'}))


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
