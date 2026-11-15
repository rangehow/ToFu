"""Typed native startup failures survive the child boundary and are reaped."""
import json
import subprocess
import sys

import pytest

from lib.storage.errors import StorageError, StorageStartRefused
from lib.storage.supervisor import StorageSupervisor
import lib.storage.supervisor as supervisor_module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('code, retryable', [('database_integrity', False), ('database_unavailable', True)])
def test_child_startup_verdict_preserves_retryability_and_cleanup(tmp_path, monkeypatch, code, retryable):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    original_popen = subprocess.Popen
    children = []
    envelope = {'type': 'storage.error', 'protocol': 2, 'code': code,
                'retryable': retryable, 'diagnostic': 'search projection startup failed: WAL witness mismatch'}
    def spawn(_command, **options):
        process = original_popen([sys.executable, '-c',
            f'import time; print({json.dumps(envelope)!r}, flush=True); time.sleep(60)'], **options)
        children.append(process)
        return process
    monkeypatch.setattr(supervisor_module.subprocess, 'Popen', spawn)
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite', startup_timeout=5)
    try:
        with pytest.raises(StorageError) as caught:
            supervisor.start()
        assert caught.value.code == code
        assert caught.value.retryable is retryable
        assert 'search projection startup failed' in str(caught.value)
        assert not supervisor.ready
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        supervisor.stop()
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_a_full_volume_start_is_a_refusal_rechecked_instead_of_a_crash(tmp_path, monkeypatch):
    # DISK-RUNWAY-2 (2026-11-12): a start that failed on a full volume or an
    # exhausted quota counted toward the restart breaker, which fenced storage
    # until a process restart even after space returned.
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    original_popen = subprocess.Popen
    children = []
    envelope = {'type': 'storage.error', 'protocol': 2, 'code': 'storage_full', 'retryable': False,
                'diagnostic': 'tofu-db authority volume lacks two WAL windows of writable headroom'}
    def spawn(_command, **options):
        process = original_popen([sys.executable, '-c',
            f'import time; print({json.dumps(envelope)!r}, flush=True); time.sleep(60)'], **options)
        children.append(process)
        return process
    monkeypatch.setattr(supervisor_module.subprocess, 'Popen', spawn)
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite', startup_timeout=5)
    try:
        with pytest.raises(StorageStartRefused) as caught:
            supervisor.start()
        assert caught.value.reason == 'storage_full'
        assert caught.value.code == 'database_unavailable'
        assert 'lacks two WAL windows' in caught.value.detail
        assert 'free space' in caught.value.remedy
        assert not supervisor.ready
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        supervisor.stop()
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
