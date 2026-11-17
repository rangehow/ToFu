"""tests/log/test_audit_log_nonblocking.py — audit_log non-blocking write path.

``audit_log`` must never perform disk I/O on the caller's thread in
production: audit.log lives on a FUSE/NFS mount, and the old synchronous
append (plus per-call ``os.makedirs``) could freeze the event loop when the
mount hung. The caller now only enqueues a serialised JSON line; a dedicated
daemon writer thread does the disk I/O.

Under pytest the write stays synchronous (``_audit_sync_writes``), so the
queue path is exercised here by monkeypatching that predicate off. The
writer reads ``AUDIT_LOG_FILE`` / ``LOG_DIR`` at write time, so redirecting
those module globals works regardless of when the writer thread started.
"""

import json
import queue
import threading
import time

import pytest

import lib.log as log_mod

pytestmark = pytest.mark.unit


def _force_queue_path(monkeypatch, tmp_path):
    monkeypatch.setattr(log_mod, '_audit_sync_writes', lambda: False)
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(log_mod, '_audit_retained', {})
    monkeypatch.setattr(log_mod, '_audit_partial_tails', set())
    monkeypatch.setattr(log_mod, '_audit_failing_paths', set())


def _drain():
    assert log_mod._audit_queue is not None
    log_mod._audit_queue.join()


def test_audit_log_sync_under_pytest_by_default(tmp_path, monkeypatch):
    """Default (pytest) mode: the line is on disk when audit_log returns."""
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.audit_log('sync_event', user='alice')
    lines = (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry['event'] == 'sync_event'
    assert entry['user'] == 'alice'
    assert 'timestamp' in entry


def test_audit_log_queue_path_writes_json_line(tmp_path, monkeypatch):
    """Queue path: the writer thread appends a well-formed JSON line."""
    _force_queue_path(monkeypatch, tmp_path)
    log_mod.audit_log('queued_event', model='opus-5', count=3)
    _drain()
    entry = json.loads(
        (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()[0])
    assert entry['event'] == 'queued_event'
    assert entry['model'] == 'opus-5'
    assert entry['count'] == 3


def test_audit_log_returns_before_write_completes(tmp_path, monkeypatch):
    """The caller must not wait for the disk write — the whole point of the
    queue. Stall the write behind a gate; audit_log must return immediately."""
    _force_queue_path(monkeypatch, tmp_path)
    gate = threading.Event()
    real_append = log_mod._audit_append

    def slow_append(path, payload, *, rotate):
        assert gate.wait(5), 'test gate never released'
        real_append(path, payload, rotate=rotate)

    monkeypatch.setattr(log_mod, '_audit_append', slow_append)
    start = time.monotonic()
    log_mod.audit_log('slow_event')
    assert time.monotonic() - start < 1.0
    gate.set()
    _drain()
    assert 'slow_event' in (tmp_path / 'audit.log').read_text(encoding='utf-8')


def test_audit_log_write_failure_falls_back_without_raising(
        tmp_path, monkeypatch, caplog):
    """A failing write on the queue path must not raise into the caller and
    must surface via the 'audit' logger fallback (from the writer thread)
    without echoing the rejected audit payload into a second log."""
    _force_queue_path(monkeypatch, tmp_path)
    # Point at a path whose parent is a regular file → open() fails.
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a dir')
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(blocker / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(blocker))
    with caplog.at_level('ERROR', logger='audit'):
        log_mod.audit_log('doomed_event')
        _drain()
    assert any('Failed to write audit log record' in r.message
               for r in caplog.records)
    assert 'doomed_event' not in caplog.text


def test_audit_queue_is_bounded_and_keeps_newest(monkeypatch, caplog):
    """A stalled audit sink must not become an unbounded-memory/OOM path."""
    q = queue.Queue(maxsize=2)
    q.put_nowait('oldest\n')
    q.put_nowait('middle\n')
    monkeypatch.setattr(log_mod, '_audit_last_drop_report_mono', float('-inf'))
    monkeypatch.setattr(log_mod, '_audit_dropped', 0)

    with caplog.at_level('WARNING', logger='audit'):
        log_mod._enqueue_audit_line(q, 'newest\n')

    assert q.maxsize == 2
    assert q.get_nowait() == 'middle\n'
    q.task_done()
    assert q.get_nowait() == 'newest\n'
    q.task_done()
    assert q.unfinished_tasks == 0
    assert any('queue saturated' in record.message for record in caplog.records)


def test_audit_queue_capacity_is_clamped(monkeypatch):
    monkeypatch.setenv('TOFU_AUDIT_LOG_QUEUE_MAX', '1')
    assert log_mod._audit_queue_capacity() == 128
    monkeypatch.setenv('TOFU_AUDIT_LOG_QUEUE_MAX', '999999')
    assert log_mod._audit_queue_capacity() == 100_000
    monkeypatch.setenv('TOFU_AUDIT_LOG_QUEUE_MAX', 'invalid')
    assert log_mod._audit_queue_capacity() == 4096


def test_transient_storage_fault_retains_and_later_writes_every_record(
        tmp_path, monkeypatch, caplog):
    """Regression for 2026-11-17 11:15-11:16: dolphinfs answered EDQUOT and
    the writer dropped 21 audit records, one per failed append. Records are
    retained in order and written once the filesystem recovers; a partial
    append resumes at its first unwritten byte, so no line is split or
    duplicated, and one fault episode logs once, not once per record."""
    import errno
    import os
    _force_queue_path(monkeypatch, tmp_path)
    real_write = os.write
    fault = {'on': True, 'partial': True}

    def flaky_write(descriptor, data):
        if fault['on'] and os.fstat(descriptor).st_ino == os.stat(
                tmp_path / 'audit.log').st_ino:
            if fault['partial']:
                fault['partial'] = False
                return real_write(descriptor, bytes(data[:7]))
            raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))
        return real_write(descriptor, data)

    monkeypatch.setattr(os, 'write', flaky_write)
    with caplog.at_level('WARNING', logger='audit'):
        for index in range(5):
            log_mod.audit_log('during_fault', index=index)
        _drain()
        fault['on'] = False
        log_mod.audit_log('after_recovery', index=5)
        _drain()
    entries = [json.loads(line) for line in
               (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()]
    assert [entry['index'] for entry in entries] == [0, 1, 2, 3, 4, 5]
    failures = [r for r in caplog.records
                if 'Failed to write audit log record' in r.getMessage()]
    assert len(failures) == 1
    assert 'during_fault' not in caplog.text
    assert any('recovered' in r.getMessage() for r in caplog.records)


def test_retained_records_are_bounded_by_the_queue_capacity(
        tmp_path, monkeypatch):
    _force_queue_path(monkeypatch, tmp_path)
    monkeypatch.setattr(log_mod, '_audit_queue_capacity', lambda: 3)
    monkeypatch.setattr(log_mod, '_audit_dropped', 0)
    broken = {'on': True}
    real_append = log_mod._audit_append

    def append(path, payload, *, rotate):
        if broken['on']:
            raise OSError(122, 'Disk quota exceeded')
        return real_append(path, payload, rotate=rotate)

    monkeypatch.setattr(log_mod, '_audit_append', append)
    for index in range(6):
        log_mod.audit_log('bounded', index=index)
    _drain()
    broken['on'] = False
    log_mod.audit_log('bounded', index=6)
    _drain()
    kept = [json.loads(line)['index'] for line in
            (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()]
    assert kept == [4, 5, 6]
