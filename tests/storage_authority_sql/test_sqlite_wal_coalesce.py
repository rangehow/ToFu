"""The WAL coalescing VFS writes stock SQLite's bytes with fewer calls, durably.

Specification for ``lib/storage_authority_sql/adapters/sqlite_wal_coalesce.c``
(docs/STORAGE.md, "The Sidecar coalesces each commit's WAL writes"):

- the WAL and database files it produces equal stock SQLite's byte for byte
  for the same workload and PRNG seed, including spills, frame rewrites,
  savepoint and transaction rollbacks, and checkpoint WAL restarts;
- a commit reaches the WAL in one write call instead of two per frame;
- a checkpoint writes each run of adjacent pages in one call, whether it
  backfills the whole WAL or stops short at a reader (SQLite then records the
  backfill with no later database call, so the last run's write is the last
  page's own write);
- a failed database write during any checkpoint loses no acknowledged commit;
- ``tofu_wal_frames()`` and ``tofu_wal_backfilled()`` report the wal-index;
- after a simulated power loss at any sync or right after any WAL write
  (between a flush and its sync), with no unsynced write surviving, a torn
  prefix surviving, or all surviving, recovery by stock SQLite keeps every
  acknowledged commit and no partial one, exactly as without the shim;
- a failed WAL write fails only its transaction, which SQLite rolls back;
- the Sidecar's connections use it.

Every workload runs in a child (``sqlite_wal_child.py``) because a power loss
exits the process and the fault VFS must load below the shim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from lib.storage_authority_sql.adapters.sqlite_wal_coalesce import VFS_NAME, wal_coalescing

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(os.name != 'posix', reason='the shim is built on POSIX hosts')]

_HERE = Path(__file__).parent
_CHILD = _HERE / 'sqlite_wal_child.py'
_TRANSACTIONS = 60
_POWER_LOSS = 77


@pytest.fixture(scope='module')
def libraries(tmp_path_factory) -> dict[str, str]:
    state = wal_coalescing()
    compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if state.vfs is None:
        if compiler is None:
            pytest.skip(state.reason)
        pytest.fail(f'a C compiler exists but the shim is unavailable: {state.reason}')
    build = tmp_path_factory.mktemp('fault-vfs')
    library = build / 'tofu_fault_vfs.so'
    command = [compiler, '-O2', '-shared', '-fPIC', '-o', str(library),
               str(_HERE / 'sqlite_fault_vfs.c')]
    include = Path(sys.base_prefix) / 'include'
    if (include / 'sqlite3ext.h').is_file():
        command.insert(1, f'-I{include}')
    subprocess.run(command, check=True, capture_output=True)
    return {'fault': str(library), 'coalesce': state.library}


def _run(libraries, database: Path, engine: str, *, environment=None, **spec):
    extensions = [libraries['fault']]
    if engine == 'coalesce':
        extensions.append(libraries['coalesce'])
    payload = {'database': str(database), 'extensions': extensions,
               'vfs': VFS_NAME if engine == 'coalesce' else None,
               'transactions': _TRANSACTIONS, **spec}
    completed = subprocess.run(
        [sys.executable, str(_CHILD), json.dumps(payload)],
        env={**os.environ, **(environment or {})}, capture_output=True, text=True,
        timeout=300)
    assert completed.returncode in (0, _POWER_LOSS), completed.stderr
    acks, failed, stats = [], [], None
    for line in completed.stdout.splitlines():
        kind, _, rest = line.partition(' ')
        if kind == 'ack':
            acks.append(int(rest))
        elif kind == 'failed':
            failed.append(int(rest.split()[0]))
        elif kind == 'stats':
            stats = json.loads(rest)
    return completed.returncode, acks, failed, stats


def _committed_transactions(database: Path) -> list[int]:
    """Recover with stock SQLite and check every committed transaction is whole."""
    connection = sqlite3.connect(database)
    try:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        ledger = connection.execute('SELECT txn, rows FROM ledger ORDER BY txn').fetchall()
        counter = connection.execute('SELECT value FROM counter').fetchone()
        for txn, rows in ledger:
            assert connection.execute(
                'SELECT count(*) FROM t WHERE txn = ?', (txn,)).fetchone()[0] == rows
        assert connection.execute('SELECT count(*) FROM t WHERE txn < 0').fetchone()[0] == 0
        assert counter is None or counter[0] == len(ledger)
        return [txn for txn, _ in ledger]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return []
        raise
    finally:
        connection.close()


def _assert_durable_prefix(database: Path, acks: list[int]) -> None:
    committed = _committed_transactions(database)
    planned = [txn for txn in range(1, _TRANSACTIONS + 1) if txn % 17]
    assert committed == planned[:len(committed)]
    assert committed[:len(acks)] == acks
    # At most the commit in flight at the power loss survives unacknowledged.
    assert len(committed) <= len(acks) + 1


def _spread(total: int, count: int) -> list[int]:
    return sorted({max(1, round(total * (index + 1) / count)) for index in range(count)})


# A 24-page cache evicts spilled pages, so SQLite reads frames back and
# rewrites checksums; at 100 pages a spilled page stays cached and its frame
# is rewritten while the transaction's frames are still buffered.
@pytest.mark.parametrize('cache_pages', [24, 100])
def test_wal_and_database_bytes_equal_stock_sqlite(libraries, tmp_path, cache_pages):
    for engine in ('stock', 'coalesce'):
        status, acks, failed, _ = _run(
            libraries, tmp_path / f'{engine}.db', engine, seed=7, transactions=120,
            cache_pages=cache_pages, snapshot=str(tmp_path / f'{engine}.snapshot'))
        assert (status, failed, len(acks)) == (0, [], 113)
    for suffix in ('', '-wal'):
        stock = (tmp_path / f'stock.snapshot{suffix}').read_bytes()
        coalesced = (tmp_path / f'coalesce.snapshot{suffix}').read_bytes()
        assert len(stock) > 0
        assert stock == coalesced, f'snapshot{suffix} differs from stock SQLite'


def test_a_commit_reaches_the_wal_in_one_write_call(libraries, tmp_path):
    writes = {}
    for engine in ('stock', 'coalesce'):
        _, acks, _, stats = _run(
            libraries, tmp_path / f'{engine}.db', engine, transactions=120,
            cache_pages=4000, checkpoint_pages=4096)
        writes[engine] = stats['wal_writes']
    # Stock issues two calls per frame; small commits here are 2-5 frames.
    assert writes['stock'] > 4 * len(acks)
    # One call per commit: the acknowledged ones and six setup statements; the
    # twelve 150-row commits (about 330 KiB each) take three unix-VFS-sized
    # calls each.
    assert writes['coalesce'] <= len(acks) + 6 + 12 * 3
    assert writes['coalesce'] * 10 < writes['stock']


@pytest.mark.parametrize('checkpoint', ['auto', 'full'])
def test_a_checkpoint_writes_each_run_of_adjacent_pages_in_one_call(
        libraries, tmp_path, checkpoint):
    stats = {}
    for engine in ('stock', 'coalesce'):
        status, acks, failed, stats[engine] = _run(
            libraries, tmp_path / f'{engine}.db', engine, transactions=120, cache_pages=4000,
            checkpoint=checkpoint)
        assert (status, failed, len(acks)) == (0, [], 113)
    # Stock copies one page per call; appended rows fill adjacent pages.
    assert stats['stock']['db_writes'] > 500
    assert stats['coalesce']['db_writes'] * 4 < stats['stock']['db_writes']
    assert stats['coalesce']['syncs'] == stats['stock']['syncs']


def test_a_checkpoint_stopped_short_by_a_reader_also_writes_runs(libraries, tmp_path):
    stats = {}
    for engine in ('stock', 'coalesce'):
        status, acks, failed, stats[engine] = _run(
            libraries, tmp_path / f'{engine}.db', engine, transactions=120,
            cache_pages=4000, pinned_reader=True, checkpoint='full')
        assert (status, failed, len(acks)) == (0, [], 113)
        assert _committed_transactions(tmp_path / f'{engine}.db') == acks
    assert stats['stock']['db_writes'] > 100
    assert stats['coalesce']['db_writes'] * 2 < stats['stock']['db_writes']


# 'pinned': a reader stops checkpoints short, so SQLite records each backfill
# with no database call after the copy; 'concurrent': PASSIVE checkpoints on
# another connection race the commits.
@pytest.mark.parametrize('checkpoint', ['auto', 'full', 'pinned', 'concurrent'])
@pytest.mark.parametrize('engine', ['stock', 'coalesce'])
def test_a_failed_checkpoint_write_loses_no_commit(libraries, tmp_path, engine, checkpoint):
    spec = {'checkpoint': 'full' if checkpoint == 'pinned' else checkpoint,
            'pinned_reader': checkpoint == 'pinned'}
    _, _, _, stats = _run(libraries, tmp_path / 'dry.db', engine, **spec)
    for at in _spread(stats['db_writes'], 8):
        database = tmp_path / f'fail-db-{at}.db'
        status, acks, failed, _ = _run(libraries, database, engine, **spec,
                                       environment={'TOFU_FAULT_FAIL_DB_WRITE': str(at)})
        # A checkpoint's failure fails no commit: every commit is
        # acknowledged, and the frames stay in the WAL until a later
        # checkpoint copies them.
        assert (status, failed) == (0, [])
        assert _committed_transactions(database) == acks


@pytest.mark.parametrize('engine', ['stock', 'coalesce'])
def test_power_loss_keeps_every_acknowledged_commit(libraries, tmp_path, engine):
    # FULL checkpoints, so the shim's coalesced page runs are among the cut points.
    _, _, _, stats = _run(libraries, tmp_path / 'dry.db', engine, checkpoint='full')
    points = [('SYNC', at) for at in _spread(stats['syncs'], 10)]
    points += [('WRITE', at) for at in _spread(stats['wal_writes'], 10)]
    losses = 0
    for trigger, at in points:
        for mode in ('drop', 'torn', 'kill'):
            database = tmp_path / f'{trigger}-{at}-{mode}.db'
            status, acks, _, _ = _run(libraries, database, engine, checkpoint='full', environment={
                f'TOFU_FAULT_POWER_LOSS_AT_{trigger}': str(at),
                'TOFU_FAULT_MODE': mode, 'TOFU_FAULT_SEED': str(at)})
            losses += status == _POWER_LOSS
            _assert_durable_prefix(database, acks)
    assert losses == len(points) * 3


@pytest.mark.parametrize('engine', ['stock', 'coalesce'])
def test_a_failed_wal_write_fails_only_its_transaction(libraries, tmp_path, engine):
    _, _, _, stats = _run(libraries, tmp_path / 'dry.db', engine)
    for at in _spread(stats['wal_writes'], 6):
        database = tmp_path / f'fail-{at}.db'
        status, acks, failed, _ = _run(libraries, database, engine, environment={
            'TOFU_FAULT_FAIL_WAL_WRITE': str(at)})
        assert status == 0
        assert len(failed) == 1
        assert failed[0] % 17 and failed[0] not in acks
        assert _committed_transactions(database) == acks
        assert acks[-1] in (_TRANSACTIONS, _TRANSACTIONS - 1)


def test_sidecar_connections_use_the_coalescing_vfs(libraries, tmp_path):
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from tests.storage_authority_sql.test_sqlite_read_pool import _config

    backend = SQLiteBackend(_config(tmp_path))
    try:
        backend.start()
        assert backend.metrics()['wal_coalescing'] == {
            'active': True, 'vfs': VFS_NAME, 'reason': 'loaded',
            'library': libraries['coalesce']}
    finally:
        backend.close()
