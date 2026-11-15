"""The required host volume is a deployment declaration with no built-in default."""
from __future__ import annotations

from pathlib import Path

import pytest

from rootless_vm.host_paths import required_host_volume

pytestmark = pytest.mark.unit


def test_no_declaration_means_no_volume_restriction(monkeypatch):
    monkeypatch.delenv('TOFU_REQUIRED_HOST_VOLUME', raising=False)
    assert required_host_volume() is None


def test_declared_volume_is_an_absolute_mount_root(monkeypatch):
    monkeypatch.setenv('TOFU_REQUIRED_HOST_VOLUME', '/mnt/shared')
    assert required_host_volume() == Path('/mnt/shared')


@pytest.mark.parametrize('value', ['relative/path', '/', '/mnt/../etc'])
def test_invalid_declaration_fails_closed(monkeypatch, value):
    monkeypatch.setenv('TOFU_REQUIRED_HOST_VOLUME', value)
    with pytest.raises(ValueError, match='absolute mount root'):
        required_host_volume()
