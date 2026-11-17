"""A real execution host stopped while its boot still waits for storage exits promptly.

Incident 2026-11-17 22:08: the native storage host had failed, so the
execution host sat in boot phase 3 attaching to a connection file that never
appeared. The restart's SIGTERM ended that attempt, but the boot's retry loop
then started the next one against a fresh storage runtime, and the host
outlived the manager's stop budget and was killed.
"""
import os
from pathlib import Path
import socket
import sys
import time

import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]


def test_stop_during_storage_attach_drains_without_kill(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('TOFU_', 'TOFUDB_')) and key != 'LD_LIBRARY_PATH'}
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    occupied.listen()
    environment.update(PYTHONPATH=str(ROOT), TOFU_PROJECT_PATH=str(ROOT),
        TOFU_DATA_DIR=str(tmp_path / 'data'), TOFU_DEPLOYMENT_MODE='personal',
        TOFU_PERSONAL_STORAGE_BACKEND='sqlite', PORT=str(occupied.getsockname()[1]),
        # The storage owner never publishes its handoff, as after its failed start.
        TOFU_STORAGE_CONNECTION_FILE=str(tmp_path / 'storage/connection.json'))
    (tmp_path / 'storage').mkdir()
    execution = ExecutionService(str(tmp_path), sys.executable)
    console = tmp_path / 'logs/server-console.log'
    try:
        execution.ensure(execution.prepare(environment), environment)
        deadline = time.monotonic() + NATIVE_SERVICE_PREPARATION_SECONDS
        while time.monotonic() < deadline:
            if console.exists() and b'Starting storage sidecar' in console.read_bytes():
                break
            assert execution.record().get('state') == 'starting', execution.record()
            time.sleep(.05)
        else:
            pytest.fail('execution host never reached its storage boot phase')
        record = execution.record()
        started = time.monotonic()
        # No retire_failed_close: a host that needs the kill fails this stop.
        assert execution.stop(NATIVE_SERVICE_STOP_SECONDS) == ''
        elapsed = time.monotonic() - started
        assert not execution.state_path.exists()
        assert not execution.host_matches(record)
        output = console.read_text(errors='replace')
        assert 'sidecar start attempt 2/' not in output, output[-4000:]
        print(f'stop during storage attach took {elapsed:.1f}s')
    finally:
        try:
            if execution.record():
                execution.stop(NATIVE_SERVICE_STOP_SECONDS, retire_failed_close=True)
        finally:
            occupied.close()
