"""Regression guard: the netpath prober must never start in a pytest process.

``tests/conftest.py`` pins ``TOFU_NETPATH=off`` BEFORE importing ``server``
(whose module-level code calls ``start_prober()``). Without that pin, every
test process — the controller plus every xdist worker — would spawn a daemon
thread that starts firing real network probes (through the env proxy) ten
seconds later and writes into the production ``logs/app.log``.

These tests intentionally live OUTSIDE tests/test_netpath.py: that module's
autouse fixture forces the switch back on to exercise the mechanism itself.
"""
from __future__ import annotations

import os

import pytest

import lib.netpath as netpath


@pytest.mark.unit
def test_conftest_pins_netpath_off():
    # If this fails, the conftest pin was removed (or the whole suite is
    # deliberately being run with TOFU_NETPATH overridden in the env).
    assert os.environ.get('TOFU_NETPATH') == 'off'


@pytest.mark.unit
def test_server_import_did_not_start_prober():
    # conftest imports server at collection time; with the pin in place the
    # module-level start_prober() call must have been a no-op. (Tolerates a
    # stopped thread object left by an earlier test in the same worker.)
    t = netpath._prober_thread
    assert t is None or not t.is_alive()


@pytest.mark.unit
def test_start_prober_refuses_in_pinned_env():
    assert netpath.start_prober(interval=60) is False
    t = netpath._prober_thread
    assert t is None or not t.is_alive()
