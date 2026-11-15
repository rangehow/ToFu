"""The private export toolchain (root ``export.py`` + ``export_pkg/``) for its tests, or a skip.

The toolchain never ships in a shareable export (export_pkg/AGENTS.md), so its
tests skip in a public checkout. ``pytest.importorskip('export')`` is not that
probe: a test that puts ``tests/`` on ``sys.path`` (to import a sibling helper
such as ``_source_scan``) makes ``import export`` resolve to the
``tests/export`` test package, and public CI then ran exporter tests against an
empty namespace and failed with AttributeError instead of skipping.
``require_export_toolchain`` decides by the repository files and always returns
the root ``export.py`` module.

``export_pkg._verify`` treats a module-level ``require_export_toolchain()``
call like ``pytest.importorskip('export')``: nothing after it runs publicly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPORT_FACADE = REPOSITORY_ROOT / 'export.py'
_SKIP_REASON = 'the private export toolchain (export.py, export_pkg/) is not shipped in public source'


def export_toolchain_present() -> bool:
    return EXPORT_FACADE.is_file() and (REPOSITORY_ROOT / 'export_pkg' / '__init__.py').is_file()


requires_export_toolchain = pytest.mark.skipif(not export_toolchain_present(), reason=_SKIP_REASON)


def require_export_toolchain() -> ModuleType:
    """The repository's ``export`` facade; skip the caller (module or test) when it is absent."""
    if not export_toolchain_present():
        pytest.skip(_SKIP_REASON, allow_module_level=True)
    module = sys.modules.get('export')
    module_file = getattr(module, '__file__', None)
    if module_file is None or Path(module_file).resolve() != EXPORT_FACADE:
        spec = importlib.util.spec_from_file_location('export', EXPORT_FACADE)
        module = importlib.util.module_from_spec(spec)
        sys.modules['export'] = module
        spec.loader.exec_module(module)
    return module
