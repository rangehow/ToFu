"""The native storage host's published record state (serverctl_pkg/native_storage_host.py).

A tripped storage breaker still publishes ``failed`` (an explicit restart
replaces a failed host) and adds ``retrying``, because the runtime keeps
retrying the start on its post-trip backoff; a host error never retries.
The trip itself is handed to the out-of-band alert without blocking.
"""
from __future__ import annotations

import pytest

from serverctl_pkg import native_storage_host

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status, errors, published', [
    ({'state': 'ready', 'degraded': False}, [], ('ready', False)),
    ({'state': 'restarting', 'degraded': False}, [], ('restarting', False)),
    ({'state': 'degraded', 'degraded': True}, [], ('failed', True)),
    ({'state': 'degraded', 'degraded': True}, ['RuntimeError: startup failed'], ('failed', False)),
    ({'state': 'ready', 'degraded': False}, ['RuntimeError: startup failed'], ('failed', False)),
])
def test_published_state_marks_only_a_breaker_trip_as_retrying(status, errors, published):
    assert native_storage_host.published_state(status, errors) == published


def test_a_breaker_trip_is_handed_to_the_out_of_band_alert(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path / 'data'))
    monkeypatch.setattr('serverctl_pkg.oob_alert.notify_trip',
                        lambda reason, *, data_dir: calls.append((reason, data_dir)) or True)
    native_storage_host._alert_breaker_trip('6 storage crashes/failed starts within 600s (limit 5)')
    assert calls == [('6 storage crashes/failed starts within 600s (limit 5)', tmp_path / 'data')]
