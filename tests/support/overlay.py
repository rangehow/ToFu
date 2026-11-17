"""Core tests that still exercise the YourProvider overlay's data (docs/PUBLIC_CORE.md increment 9b).

``requires_yourprovider_overlay`` skips such a test on a public core without the
overlay bundle. Each marked test is debt: it moves to bundles/yourprovider/tests or
switches to a synthetic fixture (tests/support/gateway_profiles.py).
"""
from __future__ import annotations

import pytest

import tofu_extensions


def yourprovider_overlay_installed() -> bool:
    return any(root.name == 'yourprovider' or (root / 'tofu_yourprovider').is_dir()
               for root in tofu_extensions.bundle_roots())


requires_yourprovider_overlay = pytest.mark.skipif(
    not yourprovider_overlay_installed(), reason='uses YourProvider overlay data (docs/PUBLIC_CORE.md increment 9b)')
