"""``tracked.json`` is read consistently while another process replaces it.

The index pins each path's current backup: a read that saw it missing let
blob cleanup delete the backup of the current version.
``tests.support.replace_window`` reproduces the FUSE ENOENT window.
"""

import json

import pytest

from lib.file_history import store
from lib.json_store import write_json_atomic
from tests.support.replace_window import ReplaceWindow

pytestmark = pytest.mark.unit

INDEX = {'notes.md': {'latest_version': 3, 'deleted': False}}


def test_tracked_index_read_during_replace_sees_the_published_index(tmp_path, monkeypatch):
    base = str(tmp_path)
    store.ensure_store(base)
    with store.tracked_index_lock(base):
        store.save_tracked(base, INDEX)
    path = store.tracked_path(base)
    window = ReplaceWindow(monkeypatch, path)
    window.run_writer(lambda: write_json_atomic(path, INDEX))

    assert window.read_during_gap(lambda: store.load_tracked(base)) == INDEX


def test_a_backup_updates_the_index_under_its_lock(tmp_path, monkeypatch):
    base = str(tmp_path)
    (tmp_path / 'notes.md').write_text('v1\n', encoding='utf-8')
    store.ensure_store(base)
    with store.tracked_index_lock(base):
        store.save_tracked(base, {})
    path = store.tracked_path(base)
    window = ReplaceWindow(monkeypatch, path)
    window.run_writer(lambda: write_json_atomic(path, {'other.md': {'latest_version': 1}}))

    version = window.read_during_gap(lambda: store.stage_backup(base, 'notes.md'))

    with open(path, encoding='utf-8') as handle:
        index = json.load(handle)
    assert version == 1
    assert index['notes.md']['latest_version'] == 1
    assert index['other.md'] == {'latest_version': 1}


def test_missing_and_malformed_indexes_still_read_empty(tmp_path):
    base = str(tmp_path)
    assert store.load_tracked(base) == {}
    store.ensure_store(base)
    with open(store.tracked_path(base), 'w', encoding='utf-8') as handle:
        handle.write('[1]')
    assert store.load_tracked(base) == {}
