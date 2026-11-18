"""Several processes write the same rotating log files.

The API host and the resident execution host each install the application log
handlers on one ``logs/`` directory (docs/LOGGING.md, "Shared rotating
files"). Two handler instances on one path stand for the two processes: each
opens the file itself, exactly as separate processes do.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib.incident_journal import _PrivateRotatingFileHandler as IncidentSink
from lib.server.server_logging import (
    _PrivateRotatingFileHandler,
    _SizeAndTimeRotatingFileHandler,
)

pytestmark = pytest.mark.unit

FAMILIES = {
    'app': lambda path: _SizeAndTimeRotatingFileHandler(
        path, when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=200, total_budget_bytes=0),
    'error': lambda path: _PrivateRotatingFileHandler(
        path, maxBytes=200, backupCount=5, encoding='utf-8'),
    'incident': lambda path: IncidentSink(
        path, maxBytes=200, backupCount=5, encoding='utf-8', delay=True),
}


def _record(text, name='lib.shared_rotation_test'):
    return logging.LogRecord(name, logging.INFO, __file__, 1, text, (), None)


def _writers(path, family):
    handlers = [FAMILIES[family](str(path)) for _ in range(2)]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(message)s'))
    return handlers


def _lines(path):
    return path.read_text(encoding='utf-8').splitlines()


def _rotated(directory, base):
    return sorted(path for path in directory.iterdir() if path != base)


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_record_after_a_peer_rollover_goes_to_the_current_file(tmp_path, family):
    base = tmp_path / 'shared.log'
    first, second = _writers(base, family)
    try:
        first.emit(_record('a' * 150))
        second.emit(_record('b' * 10))
        first.emit(_record('c' * 60))  # crosses the ceiling: first rolls the shared file over
        second.emit(_record('after-peer-rollover'))
    finally:
        first.close()
        second.close()
    assert [_lines(path) for path in _rotated(tmp_path, base)] == [['a' * 150, 'b' * 10]]
    assert _lines(base) == ['c' * 60, 'after-peer-rollover']


def test_a_peer_time_rollover_is_adopted_not_repeated(tmp_path):
    base = tmp_path / 'app.log'
    first, second = (_SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=1_000_000, total_budget_bytes=0) for _ in range(2))
    for handler in (first, second):
        handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        first.emit(_record('first-before'))
        second.emit(_record('second-before'))
        first.rolloverAt = second.rolloverAt = int(time.time()) - 1
        first.emit(_record('first-after'))
        second.emit(_record('second-after'))
    finally:
        first.close()
        second.close()
    assert [_lines(path) for path in _rotated(tmp_path, base)] == [['first-before', 'second-before']]
    assert _lines(base) == ['first-after', 'second-after']


_WRITER = '''
import logging, os, sys, time
from lib.server.server_logging import _SizeAndTimeRotatingFileHandler
path, writer, count, ready, go = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
pause = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
handler = _SizeAndTimeRotatingFileHandler(
    path, when='midnight', backupCount=10_000, encoding='utf-8',
    max_bytes=4096, total_budget_bytes=0)
handler.setFormatter(logging.Formatter('%(message)s'))
failures = []
handler.handleError = failures.append
open(ready, 'w').close()
deadline = time.monotonic() + 60
while not os.path.exists(go):
    if time.monotonic() > deadline:
        sys.exit(4)
    time.sleep(0.001)
for seq in range(count):
    text = f'W{writer}-{seq:04d} ' + 'x' * (20 + (seq * 37) % 280)
    handler.emit(logging.LogRecord('lib.shared_rotation_test', logging.INFO, 'writer', 1, text, (), None))
    if pause:
        time.sleep(pause)
handler.close()
sys.exit(3 if failures else 0)
'''


def test_concurrent_processes_keep_every_record_exactly_once(tmp_path):
    """Separately launched interpreters, like the API and execution hosts."""
    logs = tmp_path / 'logs'
    logs.mkdir()
    go = tmp_path / 'go'
    ready = [tmp_path / f'ready-{writer}' for writer in range(2)]
    workers = [subprocess.Popen(
        [sys.executable, '-c', _WRITER, str(logs / 'app.log'), str(writer), '400',
         str(ready[writer]), str(go)],
        cwd=Path(__file__).resolve().parents[2]) for writer in range(2)]
    try:
        deadline = time.monotonic() + 120
        while not all(path.exists() for path in ready) and time.monotonic() < deadline:
            if any(worker.poll() is not None for worker in workers):
                break
            time.sleep(0.01)
        go.touch()
        codes = [worker.wait(timeout=120) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
    assert codes == [0, 0]
    keys = []
    for path in logs.iterdir():
        data = path.read_bytes()
        assert b'\x00' not in data, path.name
        keys += [line.split(' ', 1)[0] for line in data.decode('utf-8').splitlines()]
    assert sorted(keys) == sorted(f'W{w}-{s:04d}' for w in range(2) for s in range(400))


_FUSE_UNLINKED_FSTAT = '''
import errno, os
_real_fstat = os.fstat
def _fuse_fstat(descriptor):
    # The BeeGFS FUSE mount answers fstat on a descriptor whose file was
    # unlinked with ENOENT, where a local filesystem reports st_nlink == 0.
    result = _real_fstat(descriptor)
    if result.st_nlink == 0:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
    return result
os.fstat = _fuse_fstat
'''

_PRUNER = '''
import fcntl, os, shutil, sys, time
logs, archive, stop = sys.argv[1], sys.argv[2], sys.argv[3]
while True:
    finished = os.path.exists(stop)
    for name in sorted(os.listdir(logs)):
        if name == 'app.log':
            continue
        path = os.path.join(logs, name)
        try:
            source = open(path, 'rb')
        except FileNotFoundError:
            continue
        with source:
            # Like retention under budget pressure: the chunk is removed while
            # a peer may still hold a descriptor on it.
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            with open(os.path.join(archive, name), 'ab') as target:
                shutil.copyfileobj(source, target)
            os.unlink(path)
    if finished:
        break
    time.sleep(0.002)
'''


def test_a_peer_removing_the_file_behind_a_descriptor_loses_no_record(tmp_path):
    """Retention in one process unlinks a chunk another writer still holds open."""
    base = tmp_path / 'app.log'
    first, second = (_SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=200, total_budget_bytes=0) for _ in range(2))
    failures = []
    for handler in (first, second):
        handler.setFormatter(logging.Formatter('%(message)s'))
        handler.handleError = failures.append
    real_fstat = os.fstat

    def fuse_fstat(descriptor):
        result = real_fstat(descriptor)
        if result.st_nlink == 0:
            raise FileNotFoundError(2, 'No such file or directory')
        return result

    try:
        second.emit(_record('second-first'))
        first.emit(_record('a' * 190))
        first.emit(_record('b' * 20))  # first rolls the file second still holds
        for path in _rotated(tmp_path, base):
            path.unlink()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, 'fstat', fuse_fstat)
            second.emit(_record('after-peer-removed-file'))
    finally:
        first.close()
        second.close()
    assert failures == []
    assert _lines(base) == ['b' * 20, 'after-peer-removed-file']


def test_concurrent_processes_keep_every_record_while_chunks_are_removed(tmp_path):
    """Writers on a FUSE-like mount while a peer removes rotated chunks."""
    logs = tmp_path / 'logs'
    logs.mkdir()
    archive = tmp_path / 'archive'
    archive.mkdir()
    go = tmp_path / 'go'
    stop = tmp_path / 'stop'
    ready = [tmp_path / f'ready-{writer}' for writer in range(3)]
    root = Path(__file__).resolve().parents[2]
    workers = [subprocess.Popen(
        [sys.executable, '-c', _FUSE_UNLINKED_FSTAT + _WRITER, str(logs / 'app.log'),
         str(writer), '300', str(ready[writer]), str(go),
         # Writer 0 stays idle between records, so peers roll its file away
         # and the pruner removes it before its next record.
         '0.005' if writer == 0 else '0'],
        cwd=root) for writer in range(3)]
    pruner = subprocess.Popen(
        [sys.executable, '-c', _PRUNER, str(logs), str(archive), str(stop)], cwd=root)
    try:
        deadline = time.monotonic() + 120
        while not all(path.exists() for path in ready) and time.monotonic() < deadline:
            if any(worker.poll() is not None for worker in workers):
                break
            time.sleep(0.01)
        go.touch()
        codes = [worker.wait(timeout=180) for worker in workers]
        stop.touch()
        assert pruner.wait(timeout=60) == 0
    finally:
        for process in (*workers, pruner):
            if process.poll() is None:
                process.kill()
    assert codes == [0, 0, 0], 'a writer reported a handler error (dropped record)'
    keys = []
    for path in (*logs.iterdir(), *archive.iterdir()):
        keys += [line.split(' ', 1)[0]
                 for line in path.read_bytes().decode('utf-8').splitlines()]
    assert sorted(keys) == sorted(f'W{w}-{s:04d}' for w in range(3) for s in range(300))


_FORK_SERVER = '''
import logging, os, sys
from lib.server.server_logging import _SizeAndTimeRotatingFileHandler
path, children, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
# The shard fork server opens the log files at ``import server``; its shards
# are forked from that image and write from the descriptors they inherit.
handler = _SizeAndTimeRotatingFileHandler(
    path, when='midnight', backupCount=10_000, encoding='utf-8',
    max_bytes=4096, total_budget_bytes=0)
handler.setFormatter(logging.Formatter('%(message)s'))
handler.emit(logging.LogRecord('t', logging.INFO, 'f', 1, 'IMAGE-0000 fork server', (), None))
pids = []
for writer in range(children):
    pid = os.fork()
    if pid == 0:
        failures = []
        handler.handleError = failures.append
        for seq in range(count):
            text = f'S{writer}-{seq:04d} ' + 'x' * (20 + (seq * 37) % 280)
            handler.emit(logging.LogRecord('t', logging.INFO, 'f', 1, text, (), None))
        os._exit(3 if failures else 0)
    pids.append(pid)
codes = [os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in pids]
print(codes, flush=True)
sys.stdin.readline()  # the fork server outlives its shards' rollovers
'''


def test_a_forked_writer_never_leaves_a_rolled_file_locked(tmp_path):
    """The 2026-11-18 06:08 wedge: a shard's rollover locked app.log for good.

    flock belongs to the open file description. A shard that closed its
    inherited copy of the fork server's description after a rollover left the
    outgoing file locked while the fork server lived, and the API host, still
    writing to that file through its own description, blocked on its next
    record until the restart.
    """
    logs = tmp_path / 'logs'
    logs.mkdir()
    base = logs / 'app.log'
    api = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=10_000, encoding='utf-8',
        max_bytes=1_000_000, total_budget_bytes=0)
    api.setFormatter(logging.Formatter('%(message)s'))
    api.emit(_record('API-0000 before'))
    fork_server = subprocess.Popen(
        [sys.executable, '-c', _FORK_SERVER, str(base), '3', '200'],
        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True)
    try:
        assert fork_server.stdout.readline().strip() == '[0, 0, 0]'
        assert len(list(logs.iterdir())) > 1, 'the shards never rolled the file over'
        emitted = threading.Event()

        def emit_after_rollovers():
            api.emit(_record('API-0001 after'))
            emitted.set()

        threading.Thread(target=emit_after_rollovers, daemon=True).start()
        assert emitted.wait(30), 'the API writer is blocked on a lock nobody releases'
    finally:
        fork_server.communicate('\n', timeout=30)
        api.close()
    keys = []
    for path in logs.iterdir():
        data = path.read_bytes()
        assert b'\x00' not in data, path.name
        keys += [line.split(' ', 1)[0] for line in data.decode('utf-8').splitlines()]
    # Forked writers exclude each other as separate processes do.
    assert sorted(keys) == sorted(
        ['API-0000', 'API-0001', 'IMAGE-0000']
        + [f'S{w}-{s:04d}' for w in range(3) for s in range(200)])


def test_a_record_logged_by_the_rollover_itself_is_appended(tmp_path):
    base = tmp_path / 'app.log'
    old = tmp_path / 'app.log.2000-01-01'
    old.write_text('old\n' * 50, encoding='utf-8')
    # Closed on its own day: rotations of the current day are never pruned.
    closed = time.mktime((2000, 1, 1, 23, 0, 0, 0, 0, -1))
    os.utime(old, (closed, closed))
    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=200, total_budget_bytes=300)
    handler.setFormatter(logging.Formatter('%(message)s'))
    retention = logging.getLogger('lib.logging_pkg.log_retention')
    previous = retention.propagate, retention.level
    retention.addHandler(handler)
    retention.propagate = False
    retention.setLevel(logging.INFO)
    done = threading.Event()

    def emit_all():
        for text in ('a' * 150, 'b' * 10, 'c' * 60):
            handler.handle(_record(text))
        done.set()

    try:
        worker = threading.Thread(target=emit_all, daemon=True)
        worker.start()
        worker.join(30)
        assert done.is_set(), 'a rollover that logs must not wait on its own file lock'
    finally:
        retention.removeHandler(handler)
        retention.propagate, retention.level = previous
        handler.close()
    assert not old.exists()
    current = _lines(base)
    assert current[-1] == 'c' * 60
    assert any('Log retention removed' in line and str(old) in line for line in current)


def test_a_delivery_lane_record_reaches_app_log_promptly_across_a_shard_rollover(tmp_path):
    """The 2026-11-18 M5 shape, through the production queue path end to end.

    A ``tofu-delivery-lane`` thread logs through the API host's QueueHandler;
    the QueueListener writes app.log with the production app handler. A fork
    server's shards roll that app.log over mid-run. Every record, before and
    after the rollover, must be in the logs directory within a second of the
    call: at 06:08 every later lane record waited for the 07:17 shutdown.
    """
    import queue as queue_mod
    from lib.logging_pkg.log_record_values import BoundedFormatter
    from lib.server.server_logging import (
        _BoundedQueueHandler, _BoundedQueueListener, _LOG_DATEFMT, _LOG_FMT,
        _app_log_handler)

    logs = tmp_path / 'logs'
    logs.mkdir()
    sink = _app_log_handler(str(logs), BoundedFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    log_queue = queue_mod.Queue(maxsize=1000)
    producer = _BoundedQueueHandler(log_queue)
    producer.setFormatter(BoundedFormatter('%(message)s'))
    listener = _BoundedQueueListener(log_queue, sink, respect_handler_level=True)
    logger = logging.getLogger('lib.delivery_lane_probe')
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(producer)
    listener.start()
    late = []

    def visible(token):
        return any(token.encode() in path.read_bytes() for path in logs.iterdir())

    def lane(phase, count):
        for seq in range(count):
            token = f'LANE-{phase}-{seq:03d}'
            logged = time.monotonic()
            logger.info('%s deferred wake probe', token)
            while not visible(token):
                if time.monotonic() - logged > 1.0:
                    late.append(token)
                    return
                time.sleep(0.01)

    def run_lane(phase):
        thread = threading.Thread(target=lane, args=(phase, 20),
                                  name='tofu-delivery-lane:knowledge', daemon=True)
        thread.start()
        thread.join(60)
        assert not thread.is_alive()

    fork_server = None
    try:
        run_lane('before')
        fork_server = subprocess.Popen(
            [sys.executable, '-c', _FORK_SERVER, str(logs / 'app.log'), '3', '200'],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True)
        assert fork_server.stdout.readline().strip() == '[0, 0, 0]'
        assert len(list(logs.iterdir())) > 1, 'the shards never rolled app.log over'
        # The fork server stays alive, as it did from 06:08 to 07:17.
        run_lane('after')
        assert late == []
    finally:
        if fork_server is not None:
            fork_server.communicate('\n', timeout=30)
        assert listener.stop(timeout=30)
        logger.removeHandler(producer)
        sink.close()
    lines = [line for path in logs.iterdir()
             for line in path.read_text(encoding='utf-8').splitlines() if 'LANE-' in line]
    assert len(lines) == 40
    assert all('[tofu-delivery-lane:knowledge]' in line for line in lines)
