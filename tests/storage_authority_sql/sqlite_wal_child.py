"""Child process for the WAL coalescing specs: one deterministic SQLite workload.

Run as ``python sqlite_wal_child.py <json>``. The JSON names the database, the
extensions to load in order (the fault VFS ``sqlite_fault_vfs.c`` first, which
is required, so the coalescing shim loads above it), whether to use the coalescing VFS, and an optional PRNG seed that
makes SQLite's WAL salts reproducible. It prints ``ack <txn>`` after each
COMMIT returns, ``failed <txn>`` for a transaction SQLite refused, and finally
``stats <json>``. With ``snapshot`` set, it copies the database and WAL before
closing, while the WAL still holds the workload's frames.

Standard library only; it never imports Tofu code.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

_SQLITE_TESTCTRL_PRNG_SEED = 28


def _seed_prng(seed: int) -> None:
    for line in Path('/proc/self/maps').read_text().splitlines():
        if 'libsqlite3' in line:
            library = ctypes.CDLL(line.split()[-1])
            library.sqlite3_test_control.restype = ctypes.c_int
            if library.sqlite3_test_control(
                    _SQLITE_TESTCTRL_PRNG_SEED, ctypes.c_int(seed), ctypes.c_void_p(None)) != 0:
                raise SystemExit('sqlite3_test_control(PRNG_SEED) failed')
            return
    raise SystemExit('libsqlite3 is not a shared library in this process')


def _payload(txn: int, row: int) -> bytes:
    size = 200 + (txn * 131 + row * 977) % 3500
    seed = hashlib.sha256(f'{txn}:{row}'.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def main() -> None:
    spec = json.loads(sys.argv[1])
    sqlite3.connect(':memory:').close()
    if spec.get('seed') is not None:
        _seed_prng(spec['seed'])
    loader = sqlite3.connect(':memory:')
    loader.enable_load_extension(True)
    for library in spec.get('extensions', []):
        loader.load_extension(library)
    path = spec['database']
    if spec.get('vfs'):
        connection = sqlite3.connect(
            f"{Path(path).absolute().as_uri()}?vfs={spec['vfs']}", uri=True, isolation_level=None)
    else:
        connection = sqlite3.connect(path, isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    # A small page cache makes large transactions spill frames mid-transaction
    # and rewrite them; frequent checkpoints restart the WAL from offset 0.
    connection.execute(f"PRAGMA cache_size={spec.get('cache_pages', 24)}")
    connection.execute(f"PRAGMA wal_autocheckpoint={spec.get('checkpoint_pages', 64)}")
    connection.execute('CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, txn INTEGER, payload BLOB)')
    connection.execute('CREATE INDEX IF NOT EXISTS t_txn ON t(txn)')
    connection.execute('CREATE TABLE IF NOT EXISTS ledger(txn INTEGER PRIMARY KEY, rows INTEGER)')
    connection.execute('CREATE TABLE IF NOT EXISTS counter(id INTEGER PRIMARY KEY, value INTEGER)')
    connection.execute('INSERT OR IGNORE INTO counter VALUES (1, 0)')
    for txn in range(1, spec['transactions'] + 1):
        rows = 150 if txn % 10 == 0 else 1 + txn % 7
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE counter SET value = value + 1 WHERE id = 1')
            for row in range(rows):
                connection.execute('INSERT INTO t(txn, payload) VALUES (?, ?)', (txn, _payload(txn, row)))
                # Keeps one hot page dirty, so a spill writes its frame and a
                # later spill or the commit rewrites it in place.
                connection.execute('UPDATE counter SET value = value WHERE id = 1')
            if txn % 13 == 0:
                connection.execute('SAVEPOINT partial')
                connection.execute('INSERT INTO t(txn, payload) VALUES (?, ?)', (-txn, b'x' * 3000))
                connection.execute('ROLLBACK TO partial')
                connection.execute('RELEASE partial')
            if txn % 10 == 0:
                # Re-dirty pages that were spilled earlier in this transaction.
                connection.execute('UPDATE t SET payload = payload || x\'00\' WHERE txn = ?', (txn,))
            connection.execute('INSERT INTO ledger VALUES (?, ?)', (txn, rows))
            if txn % 17 == 0:
                connection.execute('ROLLBACK')
                continue
            connection.execute('COMMIT')
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            print(f'failed {txn} {exc}', flush=True)
            continue
        print(f'ack {txn}', flush=True)
    if spec.get('snapshot'):
        for suffix in ('', '-wal'):
            shutil.copyfile(path + suffix, spec['snapshot'] + suffix)
    wal_writes, syncs = loader.execute(
        'SELECT tofu_fault_wal_writes(), tofu_fault_syncs()').fetchone()
    connection.close()
    print('stats ' + json.dumps({'wal_writes': wal_writes, 'syncs': syncs}), flush=True)


if __name__ == '__main__':
    main()
