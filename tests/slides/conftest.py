"""Slide integration tests use pinned bytes in a disposable cache, never live data."""
import pytest

from tests.support.misans_fixture import copy_to_cache


@pytest.fixture(scope='session')
def _misans_cache(tmp_path_factory):
    cache = tmp_path_factory.mktemp('slide-fonts')
    copy_to_cache(cache / 'misans')
    return cache


@pytest.fixture(autouse=True)
def isolated_slide_fonts(monkeypatch, _misans_cache):
    from lib.design_sys import fonts
    monkeypatch.setattr(fonts, '_store_dir', lambda: str(_misans_cache))
