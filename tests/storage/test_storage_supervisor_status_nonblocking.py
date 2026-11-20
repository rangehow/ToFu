"""``StorageSupervisor.status()`` is an in-memory snapshot that never blocks its callers.

The readiness route and the event loop read it. On 2026-11-18 a LoopWatch
stall sampled the event loop waiting on the supervisor lock in ``status()``
(14:28:51, ~5.9 s) while readiness probes failed: every call re-read the
deployment configuration from the shared volume while holding that lock, so
one slow read on any thread stalled every other reader.
"""
from __future__ import annotations

import threading

import pytest

import runtime_guards
from lib.storage.supervisor import StorageSupervisor

pytestmark = pytest.mark.unit


def _ready(supervisor: StorageSupervisor) -> StorageSupervisor:
    supervisor._embedded_runtime = object()
    supervisor._client = object()
    supervisor._ready_backend = 'tofudb'
    return supervisor


def test_ready_status_reads_no_configuration(tmp_path, monkeypatch):
    def read_configuration(**_kwargs):
        raise AssertionError('a ready supervisor answered status() from disk')

    monkeypatch.setattr(runtime_guards, 'load_deployment_configuration', read_configuration)
    snapshot = _ready(StorageSupervisor(project_root=tmp_path)).status()
    assert snapshot['ready'] is True and snapshot['backend'] == 'tofudb'


def test_configuration_read_does_not_hold_the_supervisor_lock(tmp_path, monkeypatch):
    supervisor = StorageSupervisor(project_root=tmp_path)
    other_reader = []

    def read_configuration(**_kwargs):
        def acquire():
            acquired = supervisor._lock.acquire(timeout=5)
            other_reader.append(acquired)
            if acquired:
                supervisor._lock.release()
        thread = threading.Thread(target=acquire)
        thread.start()
        thread.join()
        return type('Configuration', (), {'storage_backend': 'tofudb'})()

    monkeypatch.setattr(runtime_guards, 'load_deployment_configuration', read_configuration)
    snapshot = supervisor.status()
    assert snapshot['ready'] is False and snapshot['backend'] == 'tofudb'
    assert other_reader == [True], 'status() held its lock across a configuration read'
