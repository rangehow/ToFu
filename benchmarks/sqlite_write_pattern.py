"""Count SQLite's write calls per commit on a measured medium, per variant.

The SQL Sidecar's writer (``lib/storage_authority_sql/adapters/sqlite.py``)
runs WAL, ``synchronous=FULL`` and ``wal_autocheckpoint=4096``. On DolphinFS
every write call is a userspace round trip (0.18 ms for a 4 KiB append
without a sync), so the number of calls, not the bytes or the barriers,
decides a commit's cost there. This probe replays an event-append workload
(insert event rows into a unique (turn, seq) index, update the Turn row)
against one seeded database per variant and reports, from the ``faultfs``
shim's counters, in-scope write/read calls, their time, barriers and commit
latency.

Variants change exactly one thing against ``base``:
  page64k   page_size=65536, SQLite's documented maximum
  mmap      mmap_size covering the whole database (reads through the map)
  group8    eight logical jobs per COMMIT (the writer's group commit)
  nockpt    no checkpoint during the run (isolates the checkpoint's writes)

``--variant frames --frames N`` needs no SQLite and no shim: it writes one
commit of N WAL frames into a preallocated (reused-WAL) region either as
SQLite does, a 24-byte frame header and a page per frame (2N calls), or as
one coalesced call, each followed by ``fdatasync``, alternating, and reports
p50/p99 per commit. That bounds what a write-coalescing VFS could save.

Run (``FAULTFS_ROOT`` must be the measured root):
  cc -O2 -shared -fPIC -o $TMPDIR/faultfs.so benchmarks/agent_storage/faultfs.c -ldl
  LD_PRELOAD="$TMPDIR/faultfs.so $LD_PRELOAD" FAULTFS_ROOT=$ROOT FAULTFS_COUNT_IO=1 \\
    python3 benchmarks/sqlite_write_pattern.py --root $ROOT --variant base
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import statistics
import struct
import tempfile
import time

VARIANTS = ('base', 'page64k', 'mmap', 'group8', 'nockpt', 'frames')
WAL_FRAME_HEADER_BYTES = 24
SQLITE_MAX_PAGE_SIZE = 65536
SIDECAR_AUTOCHECKPOINT_PAGES = 4096
SIDECAR_WRITER_CACHE_KIB = 64 * 1024


def _seed(path: Path, *, page_size: int, turns: int, events: int, rng: random.Random) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(f'PRAGMA page_size={page_size}')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript('''
        CREATE TABLE turns(id TEXT PRIMARY KEY, state TEXT NOT NULL,
                           updated_ms INTEGER NOT NULL, next_seq INTEGER NOT NULL, doc TEXT NOT NULL);
        CREATE INDEX turns_updated ON turns(updated_ms);
        CREATE TABLE events(id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, seq INTEGER NOT NULL,
                            kind TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(turn_id, seq));
    ''')
    connection.execute('BEGIN')
    per_turn = events // turns
    for t in range(turns):
        turn = f'turn-{t:06d}'
        connection.execute('INSERT INTO turns VALUES (?,?,?,?,?)',
                           (turn, 'running', t, per_turn, 'd' * 600))
        connection.executemany(
            'INSERT INTO events(turn_id, seq, kind, payload) VALUES (?,?,?,?)',
            [(turn, s, 'delta', rng.randbytes(450).hex()) for s in range(per_turn)])
    connection.execute('COMMIT')
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    connection.close()


def _stats(path: Path) -> dict:
    values = struct.unpack('10Q', path.read_bytes()[:80])
    keys = ('barriers', 'injected_ns', 'enospc', 'out_of_scope', 'medium_ns',
            'writes', 'write_ns', 'write_bytes', 'reads', 'read_ns')
    return dict(zip(keys, values))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', required=True, type=Path, help='directory on the measured medium')
    parser.add_argument('--variant', choices=VARIANTS, required=True)
    parser.add_argument('--jobs', type=int, default=1500, help='logical jobs to run')
    parser.add_argument('--turns', type=int, default=400)
    parser.add_argument('--events', type=int, default=80_000)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--frames', type=int, default=7, help='WAL frames per commit (frames variant)')
    args = parser.parse_args()
    if args.variant == 'frames':
        print(json.dumps(_frame_calls(args.root, args.frames, args.jobs)))
        return
    if os.environ.get('FAULTFS_ROOT') != str(args.root) or os.environ.get('FAULTFS_COUNT_IO') != '1':
        raise SystemExit('run under faultfs with FAULTFS_ROOT=--root and FAULTFS_COUNT_IO=1')
    rng = random.Random(args.seed)
    page_size = SQLITE_MAX_PAGE_SIZE if args.variant == 'page64k' else 4096
    local = Path(tempfile.mkdtemp(prefix='sqlite-write-pattern-'))
    try:
        seeded = local / 'seed.sqlite3'
        _seed(seeded, page_size=page_size, turns=args.turns, events=args.events, rng=rng)
        args.root.mkdir(parents=True, exist_ok=True)
        database = args.root / f'{args.variant}.sqlite3'
        shutil.copyfile(seeded, database)
        stats_path = local / 'faultfs.stats'
        os.environ['FAULTFS_STATS'] = str(stats_path)
        result = _run(database, args, rng, stats_path)
    finally:
        shutil.rmtree(local, ignore_errors=True)
        for suffix in ('', '-wal', '-shm'):
            Path(f'{args.root}/{args.variant}.sqlite3{suffix}').unlink(missing_ok=True)
    print(json.dumps(result))


def _frame_calls(root: Path, frames: int, commits: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    frame = WAL_FRAME_HEADER_BYTES + 4096
    header, page = b'h' * WAL_FRAME_HEADER_BYTES, os.urandom(4096)
    path = root / 'frame-calls.wal'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.pwrite(descriptor, bytes(frame * frames * commits), 0)
        os.fsync(descriptor)
        latencies = {'split': [], 'coalesced': []}
        for index in range(commits):
            for mode in (('split', 'coalesced') if index % 2 == 0 else ('coalesced', 'split')):
                offset = (index if mode == 'split' else commits - 1 - index) * frame * frames
                begun = time.perf_counter()
                if mode == 'split':
                    for f in range(frames):
                        os.pwrite(descriptor, header, offset + f * frame)
                        os.pwrite(descriptor, page, offset + f * frame + WAL_FRAME_HEADER_BYTES)
                else:
                    os.pwrite(descriptor, (header + page) * frames, offset)
                os.fdatasync(descriptor)
                latencies[mode].append((time.perf_counter() - begun) * 1000)
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)
    result = {'variant': 'frames', 'frames_per_commit': frames, 'commits': commits}
    for mode, values in latencies.items():
        values.sort()
        result[f'{mode}_calls'] = 2 * frames if mode == 'split' else 1
        result[f'{mode}_ms_p50'] = round(statistics.median(values), 2)
        result[f'{mode}_ms_p99'] = round(values[max(0, int(len(values) * 0.99) - 1)], 2)
    return result


def _run(database: Path, args, rng: random.Random, stats_path: Path) -> dict:
    # faultfs maps its counter file when it loads, so the measured writer runs
    # in a child started with FAULTFS_STATS; seeding and copying stay uncounted.
    import subprocess
    import sys
    child = subprocess.run(
        [sys.executable, __file__, '--child', str(database), args.variant, str(args.jobs),
         str(args.turns), str(args.seed)],
        env={**os.environ, 'FAULTFS_STATS': str(stats_path)}, capture_output=True, text=True, check=True)
    timings = json.loads(child.stdout)
    counters = timings['counters']
    commits = timings['commits']
    return {
        'variant': args.variant, 'jobs': args.jobs, 'commits': commits,
        'page_size': timings['page_size'],
        'wall_s': timings['wall_s'],
        'commit_ms_p50': timings['p50'], 'commit_ms_p99': timings['p99'],
        'writes': counters['writes'], 'writes_per_commit': round(counters['writes'] / commits, 1),
        'write_s': round(counters['write_ns'] / 1e9, 2),
        'write_mib': round(counters['write_bytes'] / 2**20, 1),
        'reads': counters['reads'], 'read_s': round(counters['read_ns'] / 1e9, 2),
        'barriers': counters['barriers'], 'barrier_s': round(counters['medium_ns'] / 1e9, 2),
        'wal_frames_at_end': timings['wal_frames_at_end'],
    }


def _child(database: str, variant: str, jobs: int, turns: int, seed: int) -> None:
    rng = random.Random(seed + 1)
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute(f'PRAGMA cache_size=-{SIDECAR_WRITER_CACHE_KIB}')
    connection.execute('PRAGMA wal_autocheckpoint='
                       + ('0' if variant == 'nockpt' else str(SIDECAR_AUTOCHECKPOINT_PAGES)))
    if variant == 'mmap':
        connection.execute(f'PRAGMA mmap_size={os.path.getsize(database) * 2}')
    page_size = connection.execute('PRAGMA page_size').fetchone()[0]
    group = 8 if variant == 'group8' else 1
    latencies = []
    started = time.perf_counter()
    commits = 0
    done = 0
    while done < jobs:
        begun = time.perf_counter()
        connection.execute('BEGIN IMMEDIATE')
        for _ in range(min(group, jobs - done)):
            turn = f'turn-{rng.randrange(turns):06d}'
            connection.execute('SAVEPOINT job')
            seq = connection.execute('SELECT next_seq FROM turns WHERE id=?', (turn,)).fetchone()[0]
            connection.executemany(
                'INSERT INTO events(turn_id, seq, kind, payload) VALUES (?,?,?,?)',
                [(turn, seq + i, 'delta', rng.randbytes(450).hex()) for i in range(3)])
            connection.execute('UPDATE turns SET next_seq=?, updated_ms=? WHERE id=?',
                               (seq + 3, int(time.time() * 1000) + done, turn))
            connection.execute('RELEASE job')
            done += 1
        connection.execute('COMMIT')
        commits += 1
        latencies.append((time.perf_counter() - begun) * 1000)
    wall = time.perf_counter() - started
    counters = _stats(Path(os.environ['FAULTFS_STATS']))
    wal_frames = connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[1]
    connection.close()
    latencies.sort()
    print(json.dumps({
        'commits': commits, 'wall_s': round(wall, 2), 'page_size': page_size,
        'p50': round(statistics.median(latencies), 2),
        'p99': round(latencies[int(len(latencies) * 0.99) - 1], 2),
        'wal_frames_at_end': wal_frames, 'counters': counters,
    }))


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        _child(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]))
    else:
        main()
