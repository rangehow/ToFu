"""Meituan bundle tests (bundles/meituan/, docs/SITE_PACKS.md).

These tests live outside the default testpaths so the opensource tree never
carries company-host references. Run them explicitly in an internal checkout:

    python3 -m pytest bundles/meituan/tests -q
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )


_AUTH_MODE_BASELINE = os.environ.get('TOFU_AUTH_MODE', 'open')


@pytest.fixture(autouse=True)
def _auth_mode_override(request):
    """Bundle-local mirror of tests/conftest.py's auth-mode isolation (this
    tree sits outside the default testpaths, so the root conftest does not
    apply here)."""
    def _reset():
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass

    marker = request.node.get_closest_marker('auth_mode')
    if marker is not None:
        os.environ['TOFU_AUTH_MODE'] = marker.args[0] if marker.args else 'open'
        _reset()
    try:
        yield
    finally:
        if _AUTH_MODE_BASELINE is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = _AUTH_MODE_BASELINE
        _reset()
