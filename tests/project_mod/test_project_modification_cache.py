"""Bounded project undo-cache contracts; disk remains the authority."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_undo_cache_evicts_lru_and_reloads_from_authority(
    monkeypatch,
    tmp_path,
):
    from lib.project_mod import modifications

    directories = [tmp_path / name for name in ('a', 'b', 'c')]
    for directory in directories:
        directory.mkdir()
    loads: list[str] = []

    # _read_history is the one authoritative read behind the cache (it
    # replaced _load_from_disk on this path with per-file journal locks).
    def read_history(session_dir):
        loads.append(session_dir)
        return modifications._History(
            [], [{'session': session_dir}], ('identity', session_dir), set(), set(), 0)

    monkeypatch.setattr(modifications, '_MODS_CACHE_CAPACITY', 2)
    monkeypatch.setattr(modifications, '_read_history', read_history)
    monkeypatch.setattr(modifications, '_journal_identity',
                        lambda session_dir: ('identity', session_dir))
    monkeypatch.setattr(modifications, '_sweep_crash_artifacts', lambda *_a, **_k: None)
    from collections import OrderedDict
    monkeypatch.setattr(modifications, '_mods_cache', OrderedDict())
    for name in ('_mods_cache_identity', '_mods_cache_bytes',
                 '_mods_cache_generation', '_mods_cache_oversized'):
        monkeypatch.setattr(modifications, name, {})
    for directory in directories:
        modifications._cache_get(str(directory))

    assert list(modifications._mods_cache) == [
        str(directories[1]), str(directories[2]),
    ]
    assert modifications.modification_cache_snapshot() == {
        'entries': 2,
        'capacity': 2,
        'pendingRecords': 2,
    }

    # The evicted record is reconstructed from disk and becomes newest.
    modifications._cache_get(str(directories[0]))
    assert loads.count(str(directories[0])) == 2
    assert list(modifications._mods_cache) == [
        str(directories[2]), str(directories[0]),
    ]
