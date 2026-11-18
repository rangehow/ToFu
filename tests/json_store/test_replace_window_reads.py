"""A read racing an atomic replace decides absence under the writers' lock.

``tests.support.replace_window`` reproduces the FUSE ENOENT window.
"""

import os
import threading

import pytest

from lib.json_store import (
    locked_path, read_json, read_text, update_json_atomic, write_json_atomic,
    write_text_atomic)
from tests.support.replace_window import DEADLOCK_GUARD_S, ReplaceWindow

pytestmark = pytest.mark.unit


def test_read_json_during_replace_sees_the_published_store(tmp_path, monkeypatch):
    path = str(tmp_path / 'grants.json')
    update_json_atomic(path, lambda _cur: {'grant': 'old'}, default={})
    window = ReplaceWindow(monkeypatch, path)
    window.run_writer(lambda: write_json_atomic(path, {'grant': 'new'}))

    seen = window.read_during_gap(lambda: read_json(path, default={}, strict=True))

    assert seen == {'grant': 'new'}


def test_read_text_during_replace_sees_the_published_text(tmp_path, monkeypatch):
    path = str(tmp_path / 'journal.md')
    with locked_path(path):
        write_text_atomic(path, 'old\n')
    window = ReplaceWindow(monkeypatch, path)
    window.run_writer(lambda: write_text_atomic(path, 'new\n'))

    seen = window.read_during_gap(lambda: read_text(path, default=''))

    assert seen == 'new\n'


def _without_deadlock(action):
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault('value', action()))
    worker.start()
    worker.join(DEADLOCK_GUARD_S)
    assert not worker.is_alive(), 'read of a held path deadlocked'
    return result['value']


def test_missing_store_read_inside_its_own_mutator_returns_default(tmp_path):
    path = str(tmp_path / 'state.json')

    def mutate(current):
        assert read_json(path, default={'inner': True}) == {'inner': True}
        return {'written': current}

    assert _without_deadlock(
        lambda: update_json_atomic(path, mutate, default='none')) == {
            'written': 'none'}


def test_missing_store_read_inside_locked_path_returns_default(tmp_path):
    path = str(tmp_path / 'approvals.json')

    def locked_read():
        with locked_path(path):
            return read_json(path, default='absent'), read_text(path, 'absent')

    assert _without_deadlock(locked_read) == ('absent', 'absent')


def test_absent_store_read_creates_no_lock_file_or_directory(tmp_path):
    missing_dir = tmp_path / 'never-created'
    assert read_json(str(missing_dir / 'store.json'), default=1) == 1
    assert not missing_dir.exists()

    plain = tmp_path / 'plain.json'
    assert read_json(str(plain), default=2) == 2
    assert read_text(str(plain), default='x') == 'x'
    assert sorted(os.listdir(tmp_path)) == []
