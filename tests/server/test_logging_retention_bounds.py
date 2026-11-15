"""Regression coverage for size + time + total-budget log retention."""

from __future__ import annotations

import logging
import os

import pytest

from server import _SizeAndTimeRotatingFileHandler

pytestmark = pytest.mark.unit


def _record(text):
    return logging.LogRecord(
        'lib.retention_test', logging.INFO, __file__, 1, text, (), None)


def test_log_family_budget_prunes_oldest_rotated_only(tmp_path, caplog):
    base = tmp_path / 'app.log'
    base.write_bytes(b'current')
    old = tmp_path / 'app.log.2026-01-01'
    newer = tmp_path / 'app.log.2026-01-02'
    old.write_bytes(b'o' * 80)
    newer.write_bytes(b'n' * 80)
    os.utime(old, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    caplog.set_level(logging.INFO, logger='lib.logging_pkg.log_retention')

    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=1024, total_budget_bytes=100)
    try:
        assert base.exists(), 'active log must never be retention-pruned'
        assert not old.exists(), 'oldest rotated chunk should be pruned first'
        assert newer.exists(), 'budget fits after deleting only the oldest'
    finally:
        handler.close()
    [record] = [record for record in caplog.records
                if getattr(record, 'tofu_event_name', '')
                == 'logging.retention_removed']
    assert record.levelno == logging.WARNING
    assert record.tofu_event_fields['path'] == str(old)
    assert record.tofu_event_fields['reason'] == 'family_budget'
    assert record.tofu_event_fields['bytes'] == 80
    assert record.tofu_event_fields['budget_bytes'] == 100


def test_daily_family_never_deletes_a_chunk_of_the_current_day(tmp_path):
    """A busy day exceeds the family budget and backup count; nothing of it goes."""
    base = tmp_path / 'app.log'
    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=1, encoding='utf-8',
        max_bytes=40, total_budget_bytes=60)
    handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        for i in range(6):
            handler.emit(_record(f'{i}-' + ('x' * 28)))
        handler.flush()
    finally:
        handler.close()

    chunks = sorted(tmp_path.glob('app.log.*'))
    assert len(chunks) == 5
    assert sorted(path.read_text() for path in chunks) == [
        f'{i}-' + ('x' * 28) + '\n' for i in range(5)]


def _numbered_handler(base, backups):
    from lib.server.server_logging import _PrivateRotatingFileHandler

    handler = _PrivateRotatingFileHandler(
        str(base), maxBytes=40, backupCount=backups, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    return handler


def test_numbered_rotation_never_drops_a_backup_of_the_current_day(tmp_path):
    base = tmp_path / 'error.log'
    handler = _numbered_handler(base, backups=2)
    try:
        for i in range(5):
            handler.emit(_record(f'{i}-' + ('x' * 28)))
    finally:
        handler.close()

    assert sorted(path.name for path in tmp_path.glob('error.log.*')) == [
        'error.log.1', 'error.log.2', 'error.log.3', 'error.log.4']
    assert (tmp_path / 'error.log.4').read_text() == '0-' + 'x' * 28 + '\n'


def test_numbered_rotation_drops_an_older_backup_and_records_it(tmp_path, caplog):
    base = tmp_path / 'error.log'
    base.write_text('current\n')
    (tmp_path / 'error.log.1').write_text('today\n')
    older = tmp_path / 'error.log.2'
    older.write_text('older\n')
    os.utime(older, ns=(1, 1))
    caplog.set_level(logging.INFO, logger='lib.logging_pkg.log_retention')
    handler = _numbered_handler(base, backups=2)
    try:
        handler.doRollover()
    finally:
        handler.close()

    assert (tmp_path / 'error.log.1').read_text() == 'current\n'
    assert (tmp_path / 'error.log.2').read_text() == 'today\n'
    assert not (tmp_path / 'error.log.3').exists()
    [record] = [record for record in caplog.records
                if getattr(record, 'tofu_event_name', '')
                == 'logging.retention_removed']
    assert record.tofu_event_fields['path'] == str(older)
    assert record.tofu_event_fields['reason'] == 'numbered_backup_limit'


def test_multiple_size_rollovers_get_unique_chunks(tmp_path):
    base = tmp_path / 'app.log'
    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=40, total_budget_bytes=10_000)
    handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        for i in range(5):
            handler.emit(_record(f'{i}-' + ('x' * 28)))
        handler.flush()
    finally:
        handler.close()

    chunks = sorted(tmp_path.glob('app.log.*'))
    assert len(chunks) >= 3
    assert len({path.name for path in chunks}) == len(chunks)
    assert base.exists()
