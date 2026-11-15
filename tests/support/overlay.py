"""Core tests that still exercise the Meituan overlay's data (docs/PUBLIC_CORE.md increment 9b).

``requires_private_overlay`` skips such a test on a public core without the
overlay bundle. Each marked test is debt: it moves to bundles/meituan/tests or
switches to a synthetic fixture (tests/support/gateway_profiles.py).
"""
from __future__ import annotations

import pytest

import tofu_extensions


def private_overlay_installed() -> bool:
    return any(root.name == 'meituan' or (root / 'tofu_meituan').is_dir()
               for root in tofu_extensions.bundle_roots())


requires_private_overlay = pytest.mark.skipif(
    not private_overlay_installed(), reason='uses Meituan overlay data (docs/PUBLIC_CORE.md increment 9b)')
