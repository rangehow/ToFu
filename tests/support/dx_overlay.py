"""The 大象 overlay's test fakes, for core tests that drive a channel through 大象.

The 大象 adapter and its tests live in the Meituan overlay
(bundles/meituan/tests/channels, docs/PUBLIC_CORE.md). A core test that uses
their fakes calls ``dx_test_module`` at import: with the overlay installed it
returns the overlay's test module; without it the calling module is skipped.
"""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest

DX_TESTS_DIR = Path(__file__).resolve().parents[2] / 'bundles' / 'meituan' / 'tests' / 'channels'


def dx_installed() -> bool:
    from lib.channels.adapters import channel_adapter
    return channel_adapter('dx') is not None


def dx_test_module(name: str):
    """The overlay's ``bundles/meituan/tests/channels/<name>.py`` module, or skip the caller."""
    if not dx_installed() or not (DX_TESTS_DIR / f'{name}.py').is_file():
        pytest.skip('the 大象 overlay (tofu_meituan) is not installed', allow_module_level=True)
    if str(DX_TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(DX_TESTS_DIR))
    return importlib.import_module(name)


#: Loading the adapter registry puts the overlay on ``sys.path`` when it is present.
DX_INSTALLED = dx_installed()
requires_dx = pytest.mark.skipif(not DX_INSTALLED, reason='the 大象 overlay (tofu_meituan) is not installed')
