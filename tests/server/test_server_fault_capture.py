"""Early fatal capture keeps its own thread and bounded runtime provenance."""

from pathlib import Path

import pytest

from lib.server import server_fault_watchdog as faults

pytestmark = pytest.mark.unit


def test_fatal_capture_never_walks_other_mutating_thread_frames(monkeypatch, tmp_path):
    import lib.log
    calls = []
    monkeypatch.setattr(faults, '_SHM_DIR', str(tmp_path))
    monkeypatch.setattr(lib.log, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(faults.faulthandler, 'enable', lambda **kwargs: calls.append(kwargs))
    sinks = faults.arm_faulthandler_sinks(
        pid=123, default_log_dir=str(tmp_path), data_root=str(tmp_path / 'data'))
    try:
        assert calls and all(call['all_threads'] is False for call in calls)
        assert calls[0]['file'] is sinks.shm_log
        sinks.shm_log.flush()
        captured = Path(sinks.shm_path).read_text()
        assert 'native crash provenance' in captured
        assert 'faulting-thread-only' in captured
        assert len(captured.encode()) < 16 * 1024
    finally:
        for sink in (sinks.shm_log, sinks.durable_log):
            if sink is not None:
                sink.close()
