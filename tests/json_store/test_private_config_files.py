"""Files that can hold credentials or session material are created 0600.

``lib.json_store`` defaults to 0644 and only refuses to *widen* an existing
file, so a brand-new store took the caller's 0644: today's daily reports and a
fresh ``auth_sources.json`` (login cookies) were world-readable. Every
publication under the config tree (``lib.config_dir.CONFIG_DIR``) is now
owner-only whatever mode its caller passes, and the named credential writers
also pass ``mode=0o600`` so they stay private wherever their store lives.
Each case runs under umask 022 so the result cannot come from the host's
restrictive umask.
"""
from __future__ import annotations

import contextlib
import os
import stat

import pytest

from lib import json_store

pytestmark = pytest.mark.unit


@contextlib.contextmanager
def _permissive_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def config_tree(tmp_path, monkeypatch):
    import lib.config_dir as config_dir
    tree = tmp_path / 'config'
    monkeypatch.setattr(config_dir, 'CONFIG_DIR', str(tree))
    return tree


@pytest.mark.parametrize('relative', ['browser_access.json', 'daily_reports/1/2026-11-18.json'])
@pytest.mark.parametrize('publish', [
    lambda path: json_store.write_json_atomic(path, {'k': 'v'}),
    lambda path: json_store.update_json_atomic(path, lambda _: {'k': 'v'}, default={}),
    lambda path: json_store.write_text_atomic(path, 'text'),
    lambda path: json_store.write_bytes_atomic(path, b'bytes'),
])
def test_a_new_file_under_the_config_tree_is_owner_only(config_tree, relative, publish):
    path = config_tree / relative
    with _permissive_umask():
        publish(str(path))
    assert _mode(path) == 0o600


def test_a_world_readable_config_file_is_narrowed_on_its_next_write(config_tree):
    path = config_tree / 'key_stats.json'
    config_tree.mkdir()
    path.write_text('{}')
    os.chmod(path, 0o644)
    with _permissive_umask():
        json_store.write_json_atomic(str(path), {'n': 1})
    assert _mode(path) == 0o600


def test_atomic_output_path_under_the_config_tree_is_owner_only(config_tree):
    path = config_tree / 'export.bin'
    with _permissive_umask(), json_store.atomic_output_path(str(path)) as staging:
        with open(staging, 'wb') as handle:
            handle.write(b'payload')
    assert _mode(path) == 0o600


def test_a_file_outside_the_config_tree_keeps_the_callers_mode(config_tree, tmp_path):
    path = tmp_path / 'public' / 'site.json'
    with _permissive_umask():
        json_store.write_json_atomic(str(path), {'k': 'v'})
    assert _mode(path) == 0o644


def test_a_sibling_whose_name_extends_the_config_tree_is_not_inside_it(config_tree, tmp_path):
    path = tmp_path / 'config-public' / 'site.json'
    with _permissive_umask():
        json_store.write_json_atomic(str(path), {'k': 'v'})
    assert _mode(path) == 0o644


def test_auth_sources_creates_its_cookie_store_owner_only(tmp_path, monkeypatch):
    import lib.auth_sources as auth_sources
    path = tmp_path / 'elsewhere' / 'auth_sources.json'
    monkeypatch.setattr(auth_sources, '_STORE_PATH', str(path))
    auth_sources.invalidate_cache()
    try:
        with _permissive_umask():
            auth_sources.upsert_source(
                'xiaohongshu.com', cookies=[{'name': 'web_session', 'value': 'v'}])
        assert _mode(path) == 0o600
    finally:
        auth_sources.invalidate_cache()


def test_daily_reports_are_created_owner_only(tmp_path, monkeypatch):
    from lib.daily_report import storage
    monkeypatch.setattr(storage, '_REPORTS_DIR', str(tmp_path / 'reports'))
    monkeypatch.setattr(storage, '_invalidate_calendar', lambda _date, *, owner_user_id: None)
    with _permissive_umask():
        storage._save_report('2026-11-18', {'streams': []}, owner_user_id=41)
        storage._save_generated_report('2026-11-17', {'streams': []}, owner_user_id=41)
        storage._update_report('2026-11-16', lambda report: report,
                               owner_user_id=41, default={'tomorrow': []})
    for day in ('2026-11-18', '2026-11-17', '2026-11-16'):
        assert _mode(tmp_path / 'reports' / '41' / f'{day}.json') == 0o600
