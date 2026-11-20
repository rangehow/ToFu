"""Offline fixture integrity and copy isolation contracts."""
import hashlib
from types import SimpleNamespace

import pytest

from tests.support import misans_fixture as fixture

pytestmark = pytest.mark.unit


@pytest.fixture
def source(monkeypatch):
    data = b'verified fixture bytes'
    source = SimpleNamespace(weight=400, size=len(data),
                             sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(fixture, 'get_font', lambda _: SimpleNamespace(sources=(source,)))
    return source, data


def test_offline_install_and_copy_are_independent(tmp_path, source):
    pin, data = source
    cached = tmp_path / 'cached'
    cached.mkdir()
    (cached / 'w400.otf').write_bytes(data)
    persistent = tmp_path / 'persistent'
    fixture.install(persistent, cached=cached)
    asset = persistent / f'{pin.sha256}.otf'
    assert asset.stat().st_mode & 0o222 == 0
    isolated = tmp_path / 'isolated'
    fixture.copy_to_cache(isolated, persistent)
    (isolated / 'w400.otf').write_bytes(b'changed')
    assert asset.read_bytes() == data
    assert (cached / 'w400.otf').read_bytes() == data
    fixture.install(persistent, cached=cached)


def test_same_size_corruption_is_rejected_before_copy(tmp_path, source):
    pin, data = source
    (tmp_path / f'{pin.sha256}.otf').write_bytes(b'x' * len(data))
    destination = tmp_path / 'isolated'
    with pytest.raises(ValueError, match='pin mismatch'):
        fixture.copy_to_cache(destination, tmp_path)
    assert not destination.exists()


def test_missing_fixture_never_downloads(tmp_path, source):
    with pytest.raises(FileNotFoundError):
        fixture.copy_to_cache(tmp_path / 'isolated', tmp_path / 'missing')
