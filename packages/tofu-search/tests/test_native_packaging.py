"""The native wheel is the same distribution as the pure wheel, plus one extension.

``rust/scripts/build_wheel.py`` derives maturin's pyproject from the package's
authoritative ``pyproject.toml``; the published name, version, dependencies,
extras and console scripts therefore cannot drift between the two builds.
"""

import importlib.util
from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _build_wheel_module():
    path = _PACKAGE_DIR / 'rust' / 'scripts' / 'build_wheel.py'
    spec = importlib.util.spec_from_file_location('tofu_search_build_wheel', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_pyproject_carries_the_package_project_table_verbatim():
    native = tomllib.loads(_build_wheel_module().native_pyproject())
    source = tomllib.loads((_PACKAGE_DIR / 'pyproject.toml').read_text(encoding='utf-8'))
    assert native['project'] == source['project']
    assert native['build-system']['build-backend'] == 'maturin'
    assert native['tool']['maturin'] == {'module-name': 'tofu_search._native',
                                         'features': ['python']}


def test_crate_version_tracks_the_distribution():
    crate = tomllib.loads((_PACKAGE_DIR / 'rust' / 'Cargo.toml').read_text(encoding='utf-8'))
    source = tomllib.loads((_PACKAGE_DIR / 'pyproject.toml').read_text(encoding='utf-8'))
    assert crate['package']['version'] == source['project']['version']
