"""Open the FUSE replace window on a local disk.

On the data root's FUSE mount, ``open()`` during a writer's ``os.replace``
can fail with ENOENT although the store exists before and after (measured
29 of 2000 opens on DolphinFS). Local disks never show that window, so
:class:`ReplaceWindow` opens it inside a locked writer: the writer removes the
store, the reader opens the missing file, and only then does the writer
publish.
"""

import fcntl
import os
import threading

from lib import json_store
from lib.json_store import locked_path

DEADLOCK_GUARD_S = 30


class ReplaceWindow:
    """A writer that holds ``path``'s lock with the store missing.

    ``reader_waiting`` is set when another thread enters the path lock or
    requests a file lock (stdlib-only owners lock ``<path>.lock`` directly),
    i.e. the reader asked the writers whether the store is really absent.
    A writer passed as the reader shows whether it publishes under the lock:
    if it does, its value is the one left after the window closes.
    """

    def __init__(self, monkeypatch, path):
        self.path = path
        self.gap_open = threading.Event()
        self.publish = threading.Event()
        self.reader_waiting = threading.Event()
        real_path_lock = json_store._path_lock
        window = self

        class _ObservedLock:
            def __init__(self, lock):
                self._lock = lock

            def __enter__(self):
                if (window.gap_open.is_set()
                        and threading.current_thread() is not window.writer):
                    window.reader_waiting.set()
                self._lock.acquire()
                return self

            def __exit__(self, *exc):
                self._lock.release()
                return False

        monkeypatch.setattr(
            json_store, '_path_lock',
            lambda p: _ObservedLock(real_path_lock(p)))
        real_flock = fcntl.flock

        def _observed_flock(fd, operation):
            if (not operation & fcntl.LOCK_UN and window.gap_open.is_set()
                    and threading.current_thread() is not window.writer):
                window.reader_waiting.set()
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, 'flock', _observed_flock)
        self.writer = None

    def run_writer(self, publish):
        def _writer():
            with locked_path(self.path):
                os.unlink(self.path)
                self.gap_open.set()
                self.publish.wait(DEADLOCK_GUARD_S)
                publish()
        self.writer = threading.Thread(target=_writer, name='replace-writer')
        self.writer.start()
        assert self.gap_open.wait(DEADLOCK_GUARD_S)

    def read_during_gap(self, read):
        result = {}
        reader = threading.Thread(
            target=lambda: result.setdefault('value', read()),
            name='racing-reader')
        reader.start()
        while reader.is_alive() and not self.reader_waiting.wait(0.01):
            pass
        self.publish.set()
        reader.join(DEADLOCK_GUARD_S)
        self.writer.join(DEADLOCK_GUARD_S)
        assert not reader.is_alive() and not self.writer.is_alive()
        return result['value']
