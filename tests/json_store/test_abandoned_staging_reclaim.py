"""Abandoned atomic-write staging files are reclaimed by the writer itself.

A process killed between ``mkstemp`` and ``os.replace`` leaves its
``.jsonstore-*.tmp`` / ``.binstore-*.tmp`` staging file behind forever:
data/config held 32 of them, some carrying copies of credential stores at 0644.
Staging files are reconstructible transport data, never durable state, so the
first publication a process makes into a directory reclaims every staging file
no live writer holds. A writer holds an exclusive ``flock`` on its staging file
from creation until after publication, which is how a live one is told apart
without any age threshold.
"""
from __future__ import annotations

import fcntl
import os

import pytest

from lib import json_store

pytestmark = pytest.mark.unit


def _abandoned(directory, name, payload=b'{"stale": true}'):
    path = directory / name
    path.write_bytes(payload)
    os.chmod(path, 0o644)
    return path


def test_first_write_reclaims_abandoned_staging_files(tmp_path):
    stale_json = _abandoned(tmp_path, '.jsonstore-abc123.tmp')
    stale_empty = _abandoned(tmp_path, '.jsonstore-def456.tmp', b'')
    stale_binary = _abandoned(tmp_path, '.binstore-ghi789.tmp')
    unrelated = _abandoned(tmp_path, '.output-keep.tmp')
    store_lock = _abandoned(tmp_path, 'store.json.lock', b'')

    json_store.write_json_atomic(str(tmp_path / 'store.json'), {'ok': True})

    assert not stale_json.exists()
    assert not stale_empty.exists()
    assert not stale_binary.exists()
    assert unrelated.exists() and store_lock.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        '.output-keep.tmp', 'store.json', 'store.json.lock']


def test_a_staging_file_held_by_a_live_writer_is_kept(tmp_path):
    live = _abandoned(tmp_path, '.jsonstore-live01.tmp', b'')
    holder = os.open(live, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        json_store.write_json_atomic(str(tmp_path / 'store.json'), {'ok': True})
        assert live.exists()
    finally:
        os.close(holder)


def test_a_writer_whose_staging_file_was_reclaimed_before_it_locked_retries(
        tmp_path, monkeypatch):
    original_mkstemp = json_store.tempfile.mkstemp
    reclaimed = []

    def mkstemp_then_lose_the_race(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        if not reclaimed:
            # Another process's sweep saw the unlocked file and removed it.
            os.unlink(path)
            reclaimed.append(path)
        return fd, path

    monkeypatch.setattr(json_store.tempfile, 'mkstemp', mkstemp_then_lose_the_race)
    destination = tmp_path / 'store.json'
    json_store.write_json_atomic(str(destination), {'ok': True})

    assert reclaimed
    assert destination.read_text() == '{\n  "ok": true\n}\n'
    assert [p.name for p in tmp_path.iterdir()] == ['store.json']


def test_reclaim_runs_once_per_directory_per_process(tmp_path):
    json_store.write_json_atomic(str(tmp_path / 'store.json'), {'n': 1})
    late = _abandoned(tmp_path, '.jsonstore-late01.tmp')
    json_store.write_json_atomic(str(tmp_path / 'store.json'), {'n': 2})
    assert late.exists()


def test_a_symlink_named_like_a_staging_file_is_not_followed(tmp_path):
    target = tmp_path / 'precious.json'
    target.write_text('{}')
    link = tmp_path / '.jsonstore-link01.tmp'
    link.symlink_to(target)
    json_store.write_json_atomic(str(tmp_path / 'store.json'), {'ok': True})
    assert target.read_text() == '{}'
