"""A stop signal never runs the stop request inside the signal handler.

A Python signal handler runs on the main thread between bytecodes, even while
that thread holds a lock. The resident host's handler used to set a
``threading.Event`` directly; a shard receiving several SIGTERMs in a row
(``ShardPool.join`` signals twice, ``ResidentService.stop`` once more)
re-entered ``Event.set`` with the Event's lock already held and hung forever,
holding up its primary's drain. ``SignalStop`` only writes a byte; a watcher
thread runs the request.
"""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from serverctl_pkg.execution_host import SignalStop

pytestmark = pytest.mark.unit


def test_the_handler_returns_while_the_stop_request_lock_is_held():
    lock = threading.Lock()
    requested = threading.Event()

    def request_stop():
        with lock:
            requested.set()

    stop = SignalStop(request_stop)
    try:
        with lock:  # The main thread holds the lock, as an interrupted Event.set does.
            started = time.monotonic()
            stop.handler(signal.SIGTERM, None)
            stop.handler(signal.SIGTERM, None)
            assert time.monotonic() - started < 1.0
            assert not requested.is_set()
        assert requested.wait(10)
    finally:
        stop.close()


def test_a_burst_of_real_signals_stops_once_and_the_watcher_ends_on_close():
    script = textwrap.dedent('''
        import os, signal, sys, threading
        from serverctl_pkg.execution_host import SignalStop
        stopped = threading.Event()
        stop = SignalStop(stopped.set)
        previous = signal.signal(signal.SIGTERM, stop.handler)
        for _ in range(500):
            os.kill(os.getpid(), signal.SIGTERM)
        assert stopped.wait(20), 'stop request never ran'
        signal.signal(signal.SIGTERM, previous)
        stop.close()
        assert threading.active_count() == 1, threading.enumerate()
        print('ok')
    ''')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True,
                            text=True, timeout=60, env={**os.environ, 'PYTHONPATH': str(root)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
