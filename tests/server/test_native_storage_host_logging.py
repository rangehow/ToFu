"""The native storage host's logs: shared app sinks, never stalling storage.

In a manager deployment the tofu-db engine is a child of
``serverctl_pkg.native_storage_host``, so that process runs the engine's stderr
drainer (``lib/storage/supervisor.py``). Its records must reach app.log,
error.log and the incident index like the API and execution hosts' records,
its console (the manager's logs/server-console.log) keeps its lines, and a
stalled log volume must never stall the drain: a full stderr pipe blocks the
next engine thread that logs, possibly while it holds the authority
(docs/LOGGING.md, "The native storage host's logs").
"""

from __future__ import annotations

import io
import json
import logging
from logging.handlers import QueueHandler
import os
import re
import sys
import threading

import pytest

import lib.runtime.runtime_paths as runtime_paths
from serverctl_pkg import native_storage_host


pytestmark = pytest.mark.unit

_ENGINE_PID = 4321
_EMITTED_AT = '2026-11-09T18:06:39.091+00:00'
_CONSOLE_RECORD = re.compile(
    r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (?P<level>[A-Z]+) (?P<message>.*)')


def _engine_line(level, target, message):
    # engine_log.rs serialization: serde_json, sorted keys, compact.
    return json.dumps({
        'level': level, 'message': message, 'source': 'tofu-db',
        'target': target, 'timestamp': _EMITTED_AT}, separators=(',', ':')) + '\n'


class _EngineChild:
    """The engine child as its supervisor sees it: a pid and a stderr pipe."""

    def __init__(self):
        self.pid = _ENGINE_PID
        read_fd, self._write_fd = os.pipe()
        self.stderr = os.fdopen(read_fd, 'r')

    def write(self, text):
        view = memoryview(text.encode('utf-8'))
        while view:
            view = view[os.write(self._write_fd, view):]

    def exit(self):
        os.close(self._write_fd)


@pytest.fixture
def host_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    monkeypatch.setattr(runtime_paths, 'logs_root', lambda: str(log_dir))
    return log_dir


def _read(path):
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_engine_warn_and_error_reach_app_log_error_log_and_incidents(
        host_log_dir, monkeypatch):
    console = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', console)
    retry = _engine_line('warn', 'search_retry', (
        'failure_kind=TimedOut consecutive_failures=1 retry_after_ms=250 '
        'error=WAL barrier wait exceeded its deadline'))
    backup = _engine_line('error', 'online_backup', 'backup failed')

    stop_logging = native_storage_host.install_logging()
    try:
        supervisor = native_storage_host._new_supervisor()
        child = _EngineChild()
        supervisor._start_stderr_drainer(child)
        child.write(retry + backup)
        child.exit()
        supervisor._stderr_drainer.join(timeout=30)
        assert not supervisor._stderr_drainer.is_alive()
    finally:
        stop_logging()

    app = _read(host_log_dir / 'app.log')
    error = _read(host_log_dir / 'error.log')
    mirrored_retry = (f'tofu-db engine pid={_ENGINE_PID} at={_EMITTED_AT} '
                      '[search_retry] failure_kind=TimedOut')
    mirrored_backup = (f'tofu-db engine pid={_ENGINE_PID} at={_EMITTED_AT} '
                       '[online_backup] backup failed')
    assert [line for line in app.splitlines() if mirrored_retry in line
            and '[WARNING] tofu.storage.engine ' in line], app
    assert [line for line in error.splitlines() if mirrored_backup in line
            and '[ERROR] tofu.storage.engine ' in line], error
    incidents = [json.loads(line) for line in
                 _read(host_log_dir / 'incident.jsonl').splitlines()]
    assert {incident['level'] for incident in incidents
            if incident['logger'] == 'tofu.storage.engine'} == {'WARNING', 'ERROR'}

    # The console keeps its lines: each engine line as the engine wrote it,
    # followed by the host's formatted record of it.
    lines = console.getvalue().splitlines()
    assert lines[0] == retry.rstrip('\n')
    assert _CONSOLE_RECORD.fullmatch(lines[1]).group('level') == 'WARNING'
    assert lines[1].endswith(mirrored_retry + ' consecutive_failures=1 '
                             'retry_after_ms=250 error=WAL barrier wait '
                             'exceeded its deadline')
    assert lines[2] == backup.rstrip('\n')
    assert _CONSOLE_RECORD.fullmatch(lines[3]).group('level') == 'ERROR'
    assert lines[3].endswith(mirrored_backup)
    assert len(lines) == 4


def test_a_stalled_log_sink_never_stalls_the_engine_drain(
        host_log_dir, monkeypatch):
    """A stalled log volume costs log lines, counted, never storage progress."""
    # The shared async queue at its contract floor, so a few thousand lines
    # overflow it.
    monkeypatch.setenv('TOFU_LOG_QUEUE_MAX', '1000')
    # The host's stderr becomes a pipe nobody reads: a write blocks once its
    # buffer is full, as a write to a stalled FUSE volume does.
    stall_read, stall_write = os.pipe()
    stalled_console = os.fdopen(stall_write, 'w')
    saved_stderr_fd = os.dup(2)
    os.dup2(stall_write, 2)
    monkeypatch.setattr(sys, 'stderr', stalled_console)
    line_count = 3000
    lines = [_engine_line('warn', 'search_retry', f'failure {n:05d} ' + 'x' * 150)
             for n in range(line_count)]
    child = _EngineChild()

    def engine_writes():
        child.write(''.join(lines))
        child.exit()

    writer = threading.Thread(target=engine_writes, name='test-engine-stderr',
                              daemon=True)
    console_chunks = []
    console_reader = threading.Thread(
        target=lambda: console_chunks.extend(iter(
            lambda: os.read(stall_read, 1 << 16), b'')),
        name='test-console-reader', daemon=True)
    stop_logging = native_storage_host.install_logging()
    try:
        supervisor = native_storage_host._new_supervisor()
        supervisor._start_stderr_drainer(child)
        writer.start()
        # Hang detector only: a drain that keeps up finishes in milliseconds.
        writer.join(timeout=30)
        engine_finished_while_stalled = not writer.is_alive()
    finally:
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stderr_fd)
        console_reader.start()
        writer.join(timeout=30)
        supervisor._stderr_drainer.join(timeout=30)
        # Once the queue has drained, the next record carries the shed count.
        for handler in logging.getLogger().handlers:
            if isinstance(handler, QueueHandler):
                handler.queue.join()
        logging.getLogger('tofu.storage.host').warning('log volume recovered')
        stop_logging()
        stalled_console.close()
        console_reader.join(timeout=30)
        os.close(stall_read)

    assert engine_finished_while_stalled, (
        'the engine blocked on its stderr pipe while the log sink was stalled')
    console = b''.join(console_chunks).decode('utf-8').splitlines()
    raw = [line for line in console if line.startswith('{"level":"warn"')]
    mirrored = [line for line in console
                if f'WARNING tofu-db engine pid={_ENGINE_PID} ' in line]
    shed = sum(int(match.group(1)) for line in console
               for match in [re.search(r'shed (\d+) physical record', line)]
               if match)
    assert shed > 0
    # Every engine line became a raw console record and a mirrored record;
    # each of those was either written or shed and counted.
    assert len(raw) + len(mirrored) + shed == 2 * line_count
    app = _read(host_log_dir / 'app.log')
    assert 'Async log queue recovered; shed' in app
    assert 'log volume recovered' in app
