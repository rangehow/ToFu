"""The resident storage host's terminal verdict reaches the storage-free health payload.

2026-11-11: the native storage host latched its restart breaker and stayed
fenced for 92 minutes. The API host's attachment keeps retrying and never
latches, so its in-memory status could only say "restarting"; the verdict that
an explicit restart is needed lived in the host's own record. While storage is
not ready, /api/health and /api/ready now carry that verdict as
``storage.host`` (a local record read, never a storage RPC).
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


def _write_host_record(project, *, state: str, error: str = '') -> None:
    from serverctl_pkg.native_storage_service import RECORD_FORMAT, NativeStorageService
    service = NativeStorageService(str(project), 'python3')
    service.state_path.parent.mkdir(parents=True, exist_ok=True)
    record = {'format': RECORD_FORMAT, 'projectPath': str(service.project),
              'connectionFile': str(service.connection_path), 'state': state, 'error': error}
    service.state_path.write_text(json.dumps(record), encoding='utf-8')
    os.chmod(service.state_path, 0o600)


def test_failed_host_verdict_names_the_recorded_cause(tmp_path):
    from serverctl_pkg.native_storage_service import failed_host_verdict
    assert failed_host_verdict(str(tmp_path)) is None
    _write_host_record(tmp_path, state='ready')
    assert failed_host_verdict(str(tmp_path)) is None
    _write_host_record(tmp_path, state='failed',
                       error='6 storage crashes/failed starts within 600s (limit 5)')
    assert failed_host_verdict(str(tmp_path)) == {
        'state': 'failed', 'error': '6 storage crashes/failed starts within 600s (limit 5)'}


def test_unready_attached_storage_status_carries_the_host_verdict(tmp_path, monkeypatch):
    import lib.storage as storage
    from routes.common import _storage_authority_status
    _write_host_record(tmp_path, state='failed', error='restart breaker latched')
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', str(tmp_path / 'data/native-storage-connection.json'))
    monkeypatch.setattr(storage, 'storage_status', lambda: {
        'ready': False, 'state': 'restarting', 'unavailable_since': 1794400484.0})
    status = _storage_authority_status()
    assert status['host'] == {'state': 'failed', 'error': 'restart breaker latched'}
    assert status['unavailable_since'] == 1794400484.0

    # A ready authority needs no verdict, and the record is not read at all.
    monkeypatch.setattr(storage, 'storage_status', lambda: {'ready': True, 'state': 'ready'})
    monkeypatch.setattr('serverctl_pkg.native_storage_service.failed_host_verdict',
                        lambda _project: pytest.fail('a ready authority reads no host record'))
    assert 'host' not in _storage_authority_status()
