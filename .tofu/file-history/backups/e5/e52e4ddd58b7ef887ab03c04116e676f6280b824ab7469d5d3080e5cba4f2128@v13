"""Shared pytest fixtures for the Tofu test suite.

Provides the ``flask_client`` fixture consumed by ``tests/test_api_integration.py``
and ``tests/test_conversation_search.py``.

Design:
  * Each test session gets a fresh, isolated SQLite database via ``TOFU_DB_PATH``
    pointing at a temp file — no PostgreSQL required, no cross-test contamination.
  * The Flask ``app`` is imported lazily AFTER env-vars are set so
    ``lib.database._core`` picks the right backend at import time.
  * ``flask_client`` is function-scoped so each test gets a clean test client
    with its own cookie jar.
"""

from __future__ import annotations

import os
import tempfile

import pytest


# ─── Module-load: shim werkzeug.__version__ if missing ────────────────
#
# Werkzeug 3.x no longer exposes ``werkzeug.__version__`` as a module
# attribute, but older Flask checkouts (e.g. an editable install of
# Flask 2.3.0.dev0 pinned by a swebench workspace) still reference it
# from ``flask.testing`` and ``flask.helpers``. When that combination is
# present, ``app.test_client()`` raises ``AttributeError: module
# 'werkzeug' has no attribute '__version__'`` before any test even
# runs.
#
# Populate the attribute from package metadata so the legacy Flask path
# works without modifying the shared environment. No-op on installations
# where Werkzeug already exports it.
def _ensure_werkzeug_version():
    try:
        import werkzeug
    except ImportError:
        return
    if getattr(werkzeug, '__version__', None):
        return
    try:
        from importlib.metadata import version as _pkg_version
        werkzeug.__version__ = _pkg_version('werkzeug')
    except Exception:
        werkzeug.__version__ = '0+unknown'


_ensure_werkzeug_version()


# ─── Session-level: one SQLite DB per pytest run ──────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Set env vars BEFORE importing the Flask app so the DB layer picks
    SQLite and isolates data to a temp file. Trading features are disabled
    to keep the surface area small.
    """
    tmpdir = tempfile.mkdtemp(prefix="tofu-test-")
    db_path = os.path.join(tmpdir, "tofu-test.db")

    os.environ.setdefault("TOFU_DB_BACKEND", "sqlite")
    os.environ.setdefault("TOFU_DB_PATH", db_path)
    os.environ.setdefault("TRADING_ENABLED", "0")
    os.environ.setdefault("PPTX_TRANSLATE_ENABLED", "0")
    # Avoid accidental real LLM calls in CI.
    os.environ.setdefault("LLM_API_KEY", "test-key-placeholder")
    os.environ.setdefault("LLM_API_KEYS", "test-key-placeholder")
    # Lock auth mode to 'private' for the test suite by default. The
    # production default is 'open' (no credential required); tests that
    # exercise the gate need the stricter behavior. Individual tests
    # that need a different mode use the ``auth_mode`` marker:
    #
    #   @pytest.mark.auth_mode("open")
    #   def test_thing(): ...
    #
    # See the ``_auth_mode_override`` autouse fixture below.
    os.environ.setdefault("TOFU_AUTH_MODE", "private")

    yield

    # Best-effort cleanup — don't fail the run if files are still locked.
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


# ─── Session-level: build the Flask app once ──────────────────────────
@pytest.fixture(scope="session")
def flask_app(_configure_test_env):
    """Import and return the Flask app AFTER env-vars are set.

    This runs exactly once per test session so importing 800+ modules
    (server.py's full blueprint stack) doesn't dominate the wall-clock.
    """
    import server  # noqa: F401 — importing triggers app construction
    from server import app

    app.config.update(TESTING=True)
    return app


# ─── Function-level: per-test auth-mode override via marker ──────────
def pytest_configure(config):
    """Register the ``auth_mode`` marker so ``--strict-markers`` is happy."""
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )


@pytest.fixture(autouse=True)
def _auth_mode_override(request):
    """Apply the ``@pytest.mark.auth_mode("...")`` marker if present.

    Without a marker this is a no-op and the session default
    (``TOFU_AUTH_MODE=private`` from ``_configure_test_env``) stands.
    With a marker we set the env var, clear ``lib.auth_mode``'s cache
    so the new value is observed, and restore both on teardown.

    Tests that hit the live ``server.app`` without a Bearer token (the
    paper-migration suite, the daily-report-migration suite, anything
    importing ``server`` and using ``app.test_client()``) should mark
    themselves ``auth_mode("open")``.
    """
    marker = request.node.get_closest_marker('auth_mode')
    if marker is None:
        yield
        return
    mode = marker.args[0] if marker.args else 'open'
    prev = os.environ.get('TOFU_AUTH_MODE')
    os.environ['TOFU_AUTH_MODE'] = mode
    try:
        from lib.auth_mode import reset_for_tests
        reset_for_tests()
    except Exception:
        pass
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = prev
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass


# ─── Function-level: fresh test client per test ───────────────────────
@pytest.fixture()
def flask_client(flask_app):
    """Return a Werkzeug test client with its own cookie jar.

    Used by API integration tests and conversation-search tests.
    """
    with flask_app.test_client() as client:
        yield client
